"""SQLite log for proactive speakup proposals and commits."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from yeoman_shared.utils.helpers import ensure_dir


class SpeakupLog:
    """Append-oriented speakup log with daily sent counters."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path.expanduser()
        ensure_dir(self.db_path.parent)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._create_schema()

    def _create_schema(self) -> None:
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS speakups (
                    id TEXT PRIMARY KEY,
                    created_at REAL NOT NULL,
                    committed_at REAL,
                    channel TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    action_type TEXT NOT NULL,
                    profile TEXT NOT NULL,
                    message TEXT NOT NULL,
                    status TEXT NOT NULL,
                    trigger TEXT NOT NULL,
                    context_snapshot_json TEXT NOT NULL,
                    outcome TEXT,
                    outcome_classified_at REAL
                )
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_speakups_chat_day
                ON speakups(channel, chat_id, committed_at)
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS taste_distillations (
                    channel TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    sample_fingerprint TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (channel, chat_id, sample_fingerprint)
                )
                """
            )
            self._conn.commit()

    async def record_proposed(
        self,
        *,
        proposal_id: str | None,
        channel: str,
        chat_id: str,
        action_type: str,
        profile: str,
        message: str,
        trigger: str,
        context_snapshot: dict[str, object],
        now: float | None = None,
    ) -> str:
        entry_id = proposal_id or uuid.uuid4().hex
        created_at = float(now if now is not None else time.time())
        self._insert(
            entry_id=entry_id,
            created_at=created_at,
            committed_at=None,
            channel=channel,
            chat_id=chat_id,
            action_type=action_type,
            profile=profile,
            message=message,
            status="proposed",
            trigger=trigger,
            context_snapshot=context_snapshot,
        )
        return entry_id

    async def record_sent(
        self,
        *,
        proposal_id: str,
        channel: str,
        chat_id: str,
        action_type: str,
        profile: str,
        message: str,
        trigger: str,
        context_snapshot: dict[str, object],
        now: float | None = None,
    ) -> None:
        ts = float(now if now is not None else time.time())
        self._insert(
            entry_id=proposal_id,
            created_at=ts,
            committed_at=ts,
            channel=channel,
            chat_id=chat_id,
            action_type=action_type,
            profile=profile,
            message=message,
            status="sent",
            trigger=trigger,
            context_snapshot=context_snapshot,
            replace=True,
        )

    async def mark_sent(self, proposal_id: str, *, now: float | None = None) -> None:
        ts = float(now if now is not None else time.time())
        with self._lock:
            self._conn.execute(
                "UPDATE speakups SET status = ?, committed_at = ? WHERE id = ?",
                ("sent", ts, proposal_id),
            )
            self._conn.commit()

    async def mark_status(
        self,
        proposal_id: str,
        *,
        status: str,
        reason: str | None = None,
    ) -> None:
        with self._lock:
            if reason is None:
                self._conn.execute(
                    "UPDATE speakups SET status = ? WHERE id = ?",
                    (status, proposal_id),
                )
            else:
                self._conn.execute(
                    """
                    UPDATE speakups
                    SET status = ?, context_snapshot_json = json_set(
                        COALESCE(NULLIF(context_snapshot_json, ''), '{}'),
                        '$.status_reason',
                        ?
                    )
                    WHERE id = ?
                    """,
                    (status, reason, proposal_id),
                )
            self._conn.commit()

    async def mark_rejected(self, proposal_id: str, *, reason: str) -> None:
        await self.mark_status(proposal_id, status="rejected", reason=reason)

    async def mark_outcome(
        self,
        proposal_id: str,
        *,
        outcome: str,
        now: float | None = None,
    ) -> None:
        ts = float(now if now is not None else time.time())
        with self._lock:
            self._conn.execute(
                "UPDATE speakups SET outcome = ?, outcome_classified_at = ? WHERE id = ?",
                (outcome, ts, proposal_id),
            )
            self._conn.commit()

    async def record_silent_pass(
        self,
        *,
        channel: str,
        chat_id: str,
        profile: str,
        trigger: str,
        reason: str,
        context_snapshot: dict[str, object] | None = None,
        now: float | None = None,
    ) -> str:
        entry_id = uuid.uuid4().hex
        snapshot = dict(context_snapshot or {})
        snapshot["reason"] = reason
        self._insert(
            entry_id=entry_id,
            created_at=float(now if now is not None else time.time()),
            committed_at=None,
            channel=channel,
            chat_id=chat_id,
            action_type="silent_pass",
            profile=profile,
            message="",
            status="silent_pass",
            trigger=trigger,
            context_snapshot=snapshot,
        )
        return entry_id

    async def count_sent_today(
        self,
        *,
        channel: str,
        chat_id: str,
        now: datetime | None = None,
    ) -> int:
        current = now or datetime.now(UTC)
        if current.tzinfo is None:
            current = current.replace(tzinfo=UTC)
        start = current.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        with self._lock:
            row = self._conn.execute(
                """
                SELECT COUNT(*) AS c
                FROM speakups
                WHERE channel = ?
                  AND chat_id = ?
                  AND status = 'sent'
                  AND committed_at >= ?
                  AND committed_at < ?
                """,
                (channel, chat_id, start.timestamp(), end.timestamp()),
            ).fetchone()
        return int(row["c"] if row else 0)

    async def count_sent_since(
        self,
        *,
        channel: str,
        chat_id: str,
        since: datetime,
    ) -> int:
        current_since = since
        if current_since.tzinfo is None:
            current_since = current_since.replace(tzinfo=UTC)
        with self._lock:
            row = self._conn.execute(
                """
                SELECT COUNT(*) AS c
                FROM speakups
                WHERE channel = ?
                  AND chat_id = ?
                  AND status = 'sent'
                  AND committed_at >= ?
                """,
                (channel, chat_id, current_since.astimezone(UTC).timestamp()),
            ).fetchone()
        return int(row["c"] if row else 0)

    async def last_sent_at(
        self,
        *,
        channel: str,
        chat_id: str,
    ) -> float | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT committed_at
                FROM speakups
                WHERE channel = ?
                  AND chat_id = ?
                  AND status = 'sent'
                  AND committed_at IS NOT NULL
                ORDER BY committed_at DESC
                LIMIT 1
                """,
                (channel, chat_id),
            ).fetchone()
        if row is None or row["committed_at"] is None:
            return None
        return float(row["committed_at"])

    async def history(self, channel: str, chat_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT *
                FROM speakups
                WHERE channel = ? AND chat_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (channel, chat_id, max(1, min(int(limit), 100))),
            ).fetchall()
        return [dict(row) for row in rows]

    async def pending_outcome_rows(self, *, before: float, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT *
                FROM speakups
                WHERE status = 'sent'
                  AND committed_at IS NOT NULL
                  AND committed_at <= ?
                  AND outcome IS NULL
                ORDER BY committed_at ASC
                LIMIT ?
                """,
                (float(before), max(1, min(int(limit), 100))),
            ).fetchall()
        return [dict(row) for row in rows]

    async def outcome_samples(
        self,
        *,
        channel: str,
        chat_id: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT *
                FROM speakups
                WHERE channel = ?
                  AND chat_id = ?
                  AND status = 'sent'
                  AND outcome IS NOT NULL
                ORDER BY committed_at DESC, created_at DESC
                LIMIT ?
                """,
                (channel, chat_id, max(1, min(int(limit), 200))),
            ).fetchall()
        return [dict(row) for row in rows]

    async def outcome_sample_chats(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT channel, chat_id, MAX(COALESCE(committed_at, created_at)) AS latest_at
                FROM speakups
                WHERE status = 'sent'
                  AND outcome IS NOT NULL
                GROUP BY channel, chat_id
                ORDER BY latest_at DESC
                LIMIT ?
                """,
                (max(1, min(int(limit), 200)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def _insert(
        self,
        *,
        entry_id: str,
        created_at: float,
        committed_at: float | None,
        channel: str,
        chat_id: str,
        action_type: str,
        profile: str,
        message: str,
        status: str,
        trigger: str,
        context_snapshot: dict[str, object],
        replace: bool = False,
    ) -> None:
        sql = "INSERT OR REPLACE" if replace else "INSERT"
        with self._lock:
            self._conn.execute(
                f"""
                {sql} INTO speakups (
                    id, created_at, committed_at, channel, chat_id, action_type,
                    profile, message, status, trigger, context_snapshot_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry_id,
                    created_at,
                    committed_at,
                    channel,
                    chat_id,
                    action_type,
                    profile,
                    message,
                    status,
                    trigger,
                    json.dumps(context_snapshot, sort_keys=True),
                ),
            )
            self._conn.commit()

    async def has_taste_distillation(
        self,
        *,
        channel: str,
        chat_id: str,
        sample_fingerprint: str,
    ) -> bool:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT 1
                FROM taste_distillations
                WHERE channel = ? AND chat_id = ? AND sample_fingerprint = ?
                LIMIT 1
                """,
                (channel, chat_id, sample_fingerprint),
            ).fetchone()
        return row is not None

    async def record_taste_distillation(
        self,
        *,
        channel: str,
        chat_id: str,
        sample_fingerprint: str,
        now: float | None = None,
    ) -> None:
        ts = float(now if now is not None else time.time())
        with self._lock:
            self._conn.execute(
                """
                INSERT OR IGNORE INTO taste_distillations (
                    channel, chat_id, sample_fingerprint, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (channel, chat_id, sample_fingerprint, ts),
            )
            self._conn.commit()

    async def claim_taste_distillation(
        self,
        *,
        channel: str,
        chat_id: str,
        sample_fingerprint: str,
        now: float | None = None,
    ) -> bool:
        ts = float(now if now is not None else time.time())
        with self._lock:
            cursor = self._conn.execute(
                """
                INSERT OR IGNORE INTO taste_distillations (
                    channel, chat_id, sample_fingerprint, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (channel, chat_id, sample_fingerprint, ts),
            )
            self._conn.commit()
        return cursor.rowcount == 1

    async def delete_taste_distillation(
        self,
        *,
        channel: str,
        chat_id: str,
        sample_fingerprint: str,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                DELETE FROM taste_distillations
                WHERE channel = ? AND chat_id = ? AND sample_fingerprint = ?
                """,
                (channel, chat_id, sample_fingerprint),
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
