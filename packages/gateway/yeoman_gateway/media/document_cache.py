"""SQLite cache for lazily processed chat media.

This cache is intentionally separate from Yeoman's memory database. It stores
recent downloaded media metadata and bounded extraction/OCR results for
question-triggered retrieval only.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class MediaItem:
    id: int
    channel: str
    chat_id: str
    message_id: str
    sender_id: str | None
    sender_name: str | None
    kind: str
    mime_type: str | None
    file_name: str | None
    local_path: Path
    size_bytes: int | None
    timestamp: int
    expires_at: int


@dataclass(frozen=True, slots=True)
class MediaExtraction:
    id: int
    media_item_id: int
    mode: str
    content: str
    char_count: int
    page_count: int | None
    created_at: int


class DocumentCache:
    """Persist recent media metadata and bounded extraction results."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def record_media_item(
        self,
        *,
        channel: str,
        chat_id: str,
        message_id: str,
        sender_id: str | None,
        sender_name: str | None,
        kind: str,
        mime_type: str | None,
        file_name: str | None,
        local_path: Path | str,
        size_bytes: int | None,
        timestamp: int | None = None,
        retention_days: int = 30,
    ) -> int:
        now = int(time.time())
        ts = int(timestamp or now)
        expires_at = ts + max(1, int(retention_days)) * 86400
        path = str(Path(local_path).expanduser())
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO media_items (
                    channel, chat_id, message_id, sender_id, sender_name, kind,
                    mime_type, file_name, local_path, size_bytes, timestamp, expires_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(channel, chat_id, message_id) DO UPDATE SET
                    sender_id = excluded.sender_id,
                    sender_name = excluded.sender_name,
                    kind = excluded.kind,
                    mime_type = excluded.mime_type,
                    file_name = excluded.file_name,
                    local_path = excluded.local_path,
                    size_bytes = excluded.size_bytes,
                    timestamp = excluded.timestamp,
                    expires_at = excluded.expires_at
                RETURNING id
                """,
                (
                    channel,
                    chat_id,
                    message_id,
                    sender_id,
                    sender_name,
                    kind,
                    mime_type,
                    file_name,
                    path,
                    size_bytes,
                    ts,
                    expires_at,
                ),
            )
            return int(cur.fetchone()[0])

    def lookup_by_message(
        self,
        channel: str,
        chat_id: str,
        message_id: str,
    ) -> MediaItem | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM media_items
                WHERE channel = ? AND chat_id = ? AND message_id = ? AND expires_at >= ?
                """,
                (channel, chat_id, message_id, int(time.time())),
            ).fetchone()
        return self._media_item_from_row(row) if row else None

    def find_recent(
        self,
        *,
        channel: str,
        chat_id: str,
        kind: str | None = None,
        sender_name_hint: str | None = None,
        filename_hint: str | None = None,
        limit: int = 8,
    ) -> list[MediaItem]:
        clauses = ["channel = ?", "chat_id = ?", "expires_at >= ?"]
        params: list[Any] = [channel, chat_id, int(time.time())]
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if sender_name_hint:
            clauses.append("LOWER(COALESCE(sender_name, '')) LIKE ?")
            params.append(f"%{sender_name_hint.lower()}%")
        if filename_hint:
            clauses.append("LOWER(COALESCE(file_name, '')) LIKE ?")
            params.append(f"%{filename_hint.lower()}%")

        params.append(max(1, int(limit)))
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM media_items
                WHERE {' AND '.join(clauses)}
                ORDER BY timestamp DESC, id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [self._media_item_from_row(row) for row in rows]

    def save_extraction(
        self,
        *,
        media_item_id: int,
        mode: str,
        content: str,
        char_count: int | None = None,
        page_count: int | None = None,
    ) -> int:
        text = str(content or "")
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO media_extractions (
                    media_item_id, mode, content, char_count, page_count, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(media_item_id, mode) DO UPDATE SET
                    content = excluded.content,
                    char_count = excluded.char_count,
                    page_count = excluded.page_count,
                    created_at = excluded.created_at
                RETURNING id
                """,
                (
                    int(media_item_id),
                    mode,
                    text,
                    int(char_count if char_count is not None else len(text)),
                    page_count,
                    int(time.time()),
                ),
            )
            return int(cur.fetchone()[0])

    def get_extraction(self, media_item_id: int, mode: str) -> MediaExtraction | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM media_extractions
                WHERE media_item_id = ? AND mode = ?
                """,
                (int(media_item_id), mode),
            ).fetchone()
        return self._extraction_from_row(row) if row else None

    def cleanup_expired(self) -> int:
        now = int(time.time())
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM media_items WHERE expires_at < ?", (now,))
            return int(cur.rowcount or 0)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS media_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    sender_id TEXT,
                    sender_name TEXT,
                    kind TEXT NOT NULL,
                    mime_type TEXT,
                    file_name TEXT,
                    local_path TEXT NOT NULL,
                    size_bytes INTEGER,
                    timestamp INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    UNIQUE(channel, chat_id, message_id)
                );

                CREATE INDEX IF NOT EXISTS idx_media_items_chat_recent
                    ON media_items(channel, chat_id, timestamp DESC);

                CREATE TABLE IF NOT EXISTS media_extractions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    media_item_id INTEGER NOT NULL REFERENCES media_items(id) ON DELETE CASCADE,
                    mode TEXT NOT NULL,
                    content TEXT NOT NULL,
                    char_count INTEGER NOT NULL,
                    page_count INTEGER,
                    created_at INTEGER NOT NULL,
                    UNIQUE(media_item_id, mode)
                );
                """
            )

    @staticmethod
    def _media_item_from_row(row: sqlite3.Row) -> MediaItem:
        return MediaItem(
            id=int(row["id"]),
            channel=str(row["channel"]),
            chat_id=str(row["chat_id"]),
            message_id=str(row["message_id"]),
            sender_id=row["sender_id"],
            sender_name=row["sender_name"],
            kind=str(row["kind"]),
            mime_type=row["mime_type"],
            file_name=row["file_name"],
            local_path=Path(str(row["local_path"])),
            size_bytes=row["size_bytes"],
            timestamp=int(row["timestamp"]),
            expires_at=int(row["expires_at"]),
        )

    @staticmethod
    def _extraction_from_row(row: sqlite3.Row) -> MediaExtraction:
        return MediaExtraction(
            id=int(row["id"]),
            media_item_id=int(row["media_item_id"]),
            mode=str(row["mode"]),
            content=str(row["content"]),
            char_count=int(row["char_count"]),
            page_count=row["page_count"],
            created_at=int(row["created_at"]),
        )
