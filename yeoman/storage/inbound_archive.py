"""Inbound message archive for reply-context lookup."""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.utils.helpers import ensure_dir, get_data_path

DEFAULT_RETENTION_DAYS = 30
PURGE_INTERVAL_SECONDS = 3600


class InboundArchive:
    """SQLite-backed archive keyed by channel/chat/message_id."""

    def __init__(
        self,
        db_path: Path | None = None,
        retention_days: int = DEFAULT_RETENTION_DAYS,
    ) -> None:
        self.db_path = db_path or (get_data_path() / "inbound" / "reply_context.db")
        self.retention_days = max(1, int(retention_days))
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
    ) -> None:
        """Record one inbound message if it has not been archived yet."""
        if not channel or not chat_id or not message_id or text is None:
            return

        created_at = datetime.now(UTC).isoformat()
        with self._lock:
            self._conn.execute(
                """
                INSERT OR IGNORE INTO inbound_messages (
                    channel, chat_id, message_id, participant, sender_id, text, timestamp, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
                SELECT channel, chat_id, message_id, participant, sender_id, text, timestamp, created_at
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
                SELECT channel, chat_id, message_id, participant, sender_id, text, timestamp, created_at
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
                    SELECT channel, chat_id, message_id, participant, sender_id, text, timestamp, created_at
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
                    SELECT channel, chat_id, message_id, participant, sender_id, text, timestamp, created_at
                    FROM inbound_messages
                    WHERE channel = ? AND chat_id = ? AND created_at < ?
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (str(channel), str(chat_id), anchor_created_at, effective_limit),
                ).fetchall()

        return [dict(row) for row in rows]

    def purge_older_than(self, days: int = DEFAULT_RETENTION_DAYS) -> int:
        """Delete rows older than the retention window."""
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

    def _maybe_purge_locked(self) -> None:
        now = time.monotonic()
        if now - self._last_purge_at < PURGE_INTERVAL_SECONDS:
            return
        self._last_purge_at = now
        try:
            deleted = self.purge_older_than(self.retention_days)
            if deleted > 0:
                logger.info(
                    "inbound archive retention purge removed {} rows ({} days)",
                    deleted,
                    self.retention_days,
                )
        except Exception as e:
            logger.warning(f"inbound archive purge failed: {e}")
