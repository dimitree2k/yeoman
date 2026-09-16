"""Inbound message archive for reply-context lookup."""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from loguru import logger
from yeoman_shared.utils.helpers import ensure_dir, get_operational_data_path

DEFAULT_RETENTION_DAYS = 30
PURGE_INTERVAL_SECONDS = 3600


class InboundArchive:
    """SQLite-backed archive keyed by channel/chat/message_id."""

    def __init__(
        self,
        db_path: Path | None = None,
        retention_days: int | None = DEFAULT_RETENTION_DAYS,
    ) -> None:
        """``retention_days=None`` (or 0) keeps every archived message forever.

        The owner asked for a complete inbound record, so the running gateway uses the
        keep-forever mode and no longer purges on start; the timed mode stays available
        for callers that want it.
        """
        # The canonical location is the operational data directory; deriving it from the
        # home path instead silently creates a second, empty archive.
        self.db_path = db_path or (get_operational_data_path() / "inbound" / "reply_context.db")
        self.retention_days = (
            None if retention_days is None or int(retention_days) <= 0
            else max(1, int(retention_days))
        )
        ensure_dir(self.db_path.parent)

        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._create_schema()
        self._last_purge_at = 0.0

    def _create_schema(self) -> None:
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS inbound_messages (
                    channel TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    participant TEXT,
                    sender_id TEXT,
                    text TEXT NOT NULL,
                    timestamp INTEGER,
                    created_at TEXT NOT NULL,
                    sender_name TEXT,
                    reply_to_message_id TEXT,
                    PRIMARY KEY (channel, chat_id, message_id)
                )
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_inbound_messages_chat_created
                ON inbound_messages (channel, chat_id, created_at)
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_inbound_messages_chat_timestamp
                ON inbound_messages (channel, chat_id, timestamp)
                """
            )
            # Migrate: add sender_name column if missing (existing DBs).
            try:
                self._conn.execute(
                    "ALTER TABLE inbound_messages ADD COLUMN sender_name TEXT"
                )
            except sqlite3.OperationalError:
                pass  # Column already exists
            try:
                self._conn.execute(
                    "ALTER TABLE inbound_messages ADD COLUMN reply_to_message_id TEXT"
                )
            except sqlite3.OperationalError:
                pass  # Column already exists
            self._conn.commit()

    def record_inbound(
        self,
        *,
        channel: str,
        chat_id: str,
        message_id: str,
        participant: str | None,
        sender_id: str | None,
        text: str,
        timestamp: int | None,
        sender_name: str | None = None,
        reply_to_message_id: str | None = None,
    ) -> None:
        """Record one inbound message if it has not been archived yet."""
        if not channel or not chat_id or not message_id or text is None:
            return

        created_at = datetime.now(UTC).isoformat()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO inbound_messages (
                    channel, chat_id, message_id, participant, sender_id, text,
                    timestamp, created_at, sender_name, reply_to_message_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(channel, chat_id, message_id) DO UPDATE SET
                    text = CASE
                        WHEN instr(excluded.text, '[image_description]') > 0
                             AND instr(inbound_messages.text, '[image_description]') = 0
                        THEN excluded.text
                        ELSE inbound_messages.text
                    END,
                    reply_to_message_id = COALESCE(
                        inbound_messages.reply_to_message_id,
                        excluded.reply_to_message_id
                    )
                """,
                (
                    str(channel),
                    str(chat_id),
                    str(message_id),
                    str(participant) if participant else None,
                    str(sender_id) if sender_id else None,
                    str(text),
                    int(timestamp) if isinstance(timestamp, (int, float)) else None,
                    created_at,
                    str(sender_name) if sender_name else None,
                    str(reply_to_message_id) if reply_to_message_id else None,
                ),
            )
            self._conn.commit()
            self._maybe_purge_locked()

    def lookup_message(self, channel: str, chat_id: str, message_id: str) -> dict[str, Any] | None:
        """Find an archived message by unique key."""
        if not channel or not chat_id or not message_id:
            return None
        with self._lock:
            row = self._conn.execute(
                """
                SELECT channel, chat_id, message_id, participant, sender_id, text,
                       timestamp, created_at, sender_name, reply_to_message_id
                FROM inbound_messages
                WHERE channel = ? AND chat_id = ? AND message_id = ?
                LIMIT 1
                """,
                (str(channel), str(chat_id), str(message_id)),
            ).fetchone()
        if row is None:
            return None
        return dict(row)

    def lookup_message_any_chat(
        self,
        channel: str,
        message_id: str,
        *,
        preferred_chat_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Find a message by id within a channel, preferring a specific chat when provided."""
        if not channel or not message_id:
            return None

        preferred = str(preferred_chat_id or "")
        with self._lock:
            row = self._conn.execute(
                """
                SELECT channel, chat_id, message_id, participant, sender_id, text,
                       timestamp, created_at, sender_name, reply_to_message_id
                FROM inbound_messages
                WHERE channel = ? AND message_id = ?
                ORDER BY
                    CASE WHEN chat_id = ? THEN 0 ELSE 1 END,
                    created_at DESC
                LIMIT 1
                """,
                (str(channel), str(message_id), preferred),
            ).fetchone()

        if row is None:
            return None
        return dict(row)

    def lookup_messages_before(
        self,
        channel: str,
        chat_id: str,
        anchor_message_id: str,
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Return up to `limit` messages before one anchor message in the same chat."""
        if not channel or not chat_id or not anchor_message_id:
            return []
        effective_limit = max(1, int(limit))

        with self._lock:
            anchor = self._conn.execute(
                """
                SELECT timestamp, created_at
                FROM inbound_messages
                WHERE channel = ? AND chat_id = ? AND message_id = ?
                LIMIT 1
                """,
                (str(channel), str(chat_id), str(anchor_message_id)),
            ).fetchone()
            if anchor is None:
                return []

            anchor_timestamp = anchor["timestamp"]
            anchor_created_at = str(anchor["created_at"] or "")

            if isinstance(anchor_timestamp, int):
                rows = self._conn.execute(
                    """
                    SELECT channel, chat_id, message_id, participant, sender_id, text,
                           timestamp, created_at, sender_name, reply_to_message_id
                    FROM inbound_messages
                    WHERE channel = ? AND chat_id = ?
                      AND (
                        timestamp < ?
                        OR (timestamp = ? AND created_at < ?)
                      )
                    ORDER BY timestamp DESC, created_at DESC
                    LIMIT ?
                    """,
                    (
                        str(channel),
                        str(chat_id),
                        anchor_timestamp,
                        anchor_timestamp,
                        anchor_created_at,
                        effective_limit,
                    ),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    """
                    SELECT channel, chat_id, message_id, participant, sender_id, text,
                           timestamp, created_at, sender_name, reply_to_message_id
                    FROM inbound_messages
                    WHERE channel = ? AND chat_id = ? AND created_at < ?
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (str(channel), str(chat_id), anchor_created_at, effective_limit),
                ).fetchall()

        return [dict(row) for row in rows]

    def senders_for_messages(
        self, channel: str, chat_id: str, message_ids: tuple[str, ...]
    ) -> dict[str, str]:
        """Sender id for each retained message id in one exact chat.

        Used to revalidate the *originating principals* of an admitted batch: a
        service principal may transport an effect, but it can never stand in for the
        authorization of the participants whose material is being answered.
        """
        wanted = [str(item) for item in message_ids if str(item or "").strip()]
        if not channel or not chat_id or not wanted:
            return {}
        placeholders = ",".join("?" for _ in wanted)
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT message_id, sender_id, participant FROM inbound_messages
                WHERE channel = ? AND chat_id = ? AND message_id IN ({placeholders})
                """,
                (str(channel), str(chat_id), *wanted),
            ).fetchall()
        senders: dict[str, str] = {}
        for row in rows:
            sender = str(row["sender_id"] or row["participant"] or "")
            if sender:
                senders[str(row["message_id"])] = sender
        return senders

    def resolve_source_ids(
        self,
        channel: str,
        chat_id: str,
        source_ids: tuple[str, ...] | None,
    ) -> tuple[str, ...]:
        """Resolve retained human source ids in one exact channel and chat.

        This is deliberately a pure archive lookup. Source sequencing and the
        considered watermark belong to ``SpeakupLog``; retention or a process restart
        must therefore never change the answer's revision or create archive state.
        """
        if not channel or not chat_id:
            return ()
        wanted = {
            str(item).strip()
            for item in (source_ids or ())
            if str(item or "").strip() and not str(item).strip().startswith("observed:")
        }
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT message_id, participant, sender_id
                FROM inbound_messages
                WHERE channel = ? AND chat_id = ?
                ORDER BY created_at ASC, message_id ASC
                """,
                (str(channel), str(chat_id)),
            ).fetchall()
        resolved: list[str] = []
        for row in rows:
            message_id = str(row["message_id"] or "").strip()
            sender = str(row["sender_id"] or row["participant"] or "").strip()
            if not message_id or not sender or message_id.startswith("observed:"):
                continue
            if source_ids is None or message_id in wanted:
                resolved.append(message_id)
        return tuple(resolved)

    def lookup_messages_in_range(
        self,
        channel: str,
        chat_id: str,
        since: datetime,
        until: datetime | None = None,
        *,
        limit: int = 300,
        latest: bool = False,
    ) -> list[dict[str, Any]]:
        """Return messages between two timestamps, oldest first.

        *since* and *until* are UTC datetimes.  *until* defaults to now.
        Hard cap at 300 rows.  When *latest* is true, the newest limited
        slice is selected first and then returned oldest first.
        """
        if not channel or not chat_id:
            return []
        effective_limit = max(1, min(int(limit), 300))
        since_epoch = int(since.timestamp())
        until_dt = until or datetime.now(UTC)
        until_epoch = int(until_dt.timestamp())
        since_iso = since.isoformat()
        until_iso = until_dt.isoformat()

        order_expr = """
            CASE WHEN timestamp IS NOT NULL THEN timestamp
                 ELSE CAST(strftime('%s', created_at) AS INTEGER)
            END
        """
        if latest:
            sql = f"""
                SELECT *
                FROM (
                    SELECT channel, chat_id, message_id, participant, sender_id,
                           text, timestamp, created_at, sender_name, reply_to_message_id,
                           {order_expr} AS sort_ts
                    FROM inbound_messages
                    WHERE channel = ? AND chat_id = ?
                      AND (
                        (timestamp IS NOT NULL AND timestamp >= ? AND timestamp <= ?)
                        OR
                        (timestamp IS NULL AND created_at >= ? AND created_at <= ?)
                      )
                    ORDER BY sort_ts DESC, created_at DESC
                    LIMIT ?
                )
                ORDER BY sort_ts ASC, created_at ASC
                """
        else:
            sql = f"""
                SELECT channel, chat_id, message_id, participant, sender_id,
                       text, timestamp, created_at, sender_name, reply_to_message_id
                FROM inbound_messages
                WHERE channel = ? AND chat_id = ?
                  AND (
                    (timestamp IS NOT NULL AND timestamp >= ? AND timestamp <= ?)
                    OR
                    (timestamp IS NULL AND created_at >= ? AND created_at <= ?)
                  )
                ORDER BY {order_expr} ASC, created_at ASC
                LIMIT ?
                """

        with self._lock:
            rows = self._conn.execute(
                sql,
                (
                    str(channel),
                    str(chat_id),
                    since_epoch,
                    until_epoch,
                    since_iso,
                    until_iso,
                    effective_limit,
                ),
            ).fetchall()

        return [dict(row) for row in rows]

    def purge_older_than(self, days: int = DEFAULT_RETENTION_DAYS) -> int:
        """Delete rows older than the retention window.

        Refuses to delete anything when the archive is in keep-forever mode, so an
        operator command cannot silently shorten a deliberately complete record.
        """
        if self.retention_days is None:
            return 0
        effective_days = max(1, int(days))
        cutoff = datetime.now(UTC) - timedelta(days=effective_days)
        cutoff_iso = cutoff.isoformat()
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM inbound_messages WHERE created_at < ?",
                (cutoff_iso,),
            )
            deleted = int(cur.rowcount or 0)
            self._conn.commit()
        return deleted

    def close(self) -> None:
        """Close the sqlite connection."""
        with self._lock:
            self._conn.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def _maybe_purge_locked(self) -> None:
        if self.retention_days is None:
            return  # keep-forever mode: nothing is ever deleted automatically
        now = time.monotonic()
        if now - self._last_purge_at < PURGE_INTERVAL_SECONDS:
            return
        self._last_purge_at = now
        try:
            deleted = self.purge_older_than(int(self.retention_days))
            if deleted > 0:
                logger.info(
                    "inbound archive retention purge removed {} rows ({} days)",
                    deleted,
                    self.retention_days,
                )
        except Exception as e:
            logger.warning(f"inbound archive purge failed: {e}")
