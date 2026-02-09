"""SQLite FTS backend for nanobot long-term memory."""

from __future__ import annotations

import re
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

from nanobot.memory.models import MemoryEntry, MemoryHit, MemoryKind
from nanobot.utils.helpers import ensure_dir


class SqliteFtsMemoryBackend:
    """SQLite backend with FTS5 retrieval for long-term memory."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path.expanduser()
        ensure_dir(self.db_path.parent)

        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._create_schema()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _create_schema(self) -> None:
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_entries (
                    id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    scope_type TEXT NOT NULL,
                    scope_key TEXT NOT NULL,
                    channel TEXT,
                    chat_id TEXT,
                    sender_id TEXT,
                    kind TEXT NOT NULL,
                    content TEXT NOT NULL,
                    content_norm TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    importance REAL NOT NULL,
                    confidence REAL NOT NULL,
                    source TEXT NOT NULL,
                    source_message_id TEXT,
                    source_role TEXT,
                    meta_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_accessed_at TEXT,
                    expires_at TEXT,
                    is_deleted INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_memory_scope_kind_updated
                ON memory_entries (workspace_id, scope_key, kind, is_deleted, updated_at DESC)
                """
            )
            self._conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_dedupe_active
                ON memory_entries (workspace_id, scope_key, kind, content_hash, is_deleted)
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS memory_entries_fts
                USING fts5(entry_id UNINDEXED, content)
                """
            )
            self._conn.commit()

    @staticmethod
    def _normalize_query(query: str) -> str:
        tokens = re.findall(r"[a-zA-Z0-9_]{2,}", query.lower())
        deduped: list[str] = []
        seen: set[str] = set()
        for token in tokens:
            if token in seen:
                continue
            seen.add(token)
            deduped.append(token)
            if len(deduped) >= 16:
                break
        return " OR ".join(deduped)

    @staticmethod
    def _row_to_entry(row: sqlite3.Row) -> MemoryEntry:
        return MemoryEntry(
            id=str(row["id"]),
            workspace_id=str(row["workspace_id"]),
            scope_type=str(row["scope_type"]),
            scope_key=str(row["scope_key"]),
            channel=str(row["channel"]) if row["channel"] else None,
            chat_id=str(row["chat_id"]) if row["chat_id"] else None,
            sender_id=str(row["sender_id"]) if row["sender_id"] else None,
            kind=str(row["kind"]),
            content=str(row["content"]),
            content_norm=str(row["content_norm"]),
            content_hash=str(row["content_hash"]),
            importance=float(row["importance"]),
            confidence=float(row["confidence"]),
            source=str(row["source"]),
            source_message_id=str(row["source_message_id"]) if row["source_message_id"] else None,
            source_role=str(row["source_role"]) if row["source_role"] else None,
            meta_json=str(row["meta_json"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            last_accessed_at=str(row["last_accessed_at"]) if row["last_accessed_at"] else None,
            expires_at=str(row["expires_at"]) if row["expires_at"] else None,
            is_deleted=bool(int(row["is_deleted"])),
        )

    def upsert_entry(self, entry: MemoryEntry) -> tuple[MemoryEntry, bool]:
        now_iso = datetime.now(UTC).isoformat()
        with self._lock:
            existing = self._conn.execute(
                """
                SELECT *
                FROM memory_entries
                WHERE workspace_id = ?
                  AND scope_key = ?
                  AND kind = ?
                  AND content_hash = ?
                  AND is_deleted = 0
                LIMIT 1
                """,
                (
                    entry.workspace_id,
                    entry.scope_key,
                    entry.kind,
                    entry.content_hash,
                ),
            ).fetchone()

            if existing is not None:
                existing_entry = self._row_to_entry(existing)
                merged_importance = max(existing_entry.importance, entry.importance)
                merged_confidence = max(existing_entry.confidence, entry.confidence)
                self._conn.execute(
                    """
                    UPDATE memory_entries
                    SET importance = ?,
                        confidence = ?,
                        updated_at = ?,
                        last_accessed_at = ?
                    WHERE id = ?
                    """,
                    (
                        merged_importance,
                        merged_confidence,
                        now_iso,
                        now_iso,
                        existing_entry.id,
                    ),
                )
                self._conn.commit()
                row = self._conn.execute(
                    "SELECT * FROM memory_entries WHERE id = ? LIMIT 1",
                    (existing_entry.id,),
                ).fetchone()
                if row is None:
                    return existing_entry, False
                return self._row_to_entry(row), False

            entry_id = entry.id or str(uuid.uuid4())
            created_at = entry.created_at or now_iso
            updated_at = entry.updated_at or now_iso
            self._conn.execute(
                """
                INSERT INTO memory_entries (
                    id, workspace_id, scope_type, scope_key,
                    channel, chat_id, sender_id,
                    kind, content, content_norm, content_hash,
                    importance, confidence,
                    source, source_message_id, source_role,
                    meta_json,
                    created_at, updated_at, last_accessed_at, expires_at, is_deleted
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry_id,
                    entry.workspace_id,
                    entry.scope_type,
                    entry.scope_key,
                    entry.channel,
                    entry.chat_id,
                    entry.sender_id,
                    entry.kind,
                    entry.content,
                    entry.content_norm,
                    entry.content_hash,
                    float(entry.importance),
                    float(entry.confidence),
                    entry.source,
                    entry.source_message_id,
                    entry.source_role,
                    entry.meta_json,
                    created_at,
                    updated_at,
                    entry.last_accessed_at,
                    entry.expires_at,
                    1 if entry.is_deleted else 0,
                ),
            )
            self._conn.execute(
                "INSERT INTO memory_entries_fts (entry_id, content) VALUES (?, ?)",
                (entry_id, entry.content_norm or entry.content),
            )
            self._conn.commit()

            row = self._conn.execute(
                "SELECT * FROM memory_entries WHERE id = ? LIMIT 1",
                (entry_id,),
            ).fetchone()
            if row is None:
                return entry, True
            return self._row_to_entry(row), True

    def search(
        self,
        *,
        workspace_id: str,
        query: str,
        scope_keys: list[str],
        kinds: set[MemoryKind] | None = None,
        limit: int = 8,
    ) -> list[MemoryHit]:
        if not scope_keys:
            return []

        fts_query = self._normalize_query(query)
        if not fts_query:
            return []

        scope_placeholders = ",".join(["?"] * len(scope_keys))
        where = [
            "e.workspace_id = ?",
            "e.is_deleted = 0",
            f"e.scope_key IN ({scope_placeholders})",
        ]
        params: list[object] = [workspace_id, *scope_keys]

        if kinds:
            kind_values = sorted(kinds)
            kind_placeholders = ",".join(["?"] * len(kind_values))
            where.append(f"e.kind IN ({kind_placeholders})")
            params.extend(kind_values)

        sql = (
            "SELECT e.*, bm25(memory_entries_fts) AS fts_score "
            "FROM memory_entries_fts "
            "JOIN memory_entries e ON e.id = memory_entries_fts.entry_id "
            f"WHERE {' AND '.join(where)} "
            "AND memory_entries_fts MATCH ? "
            "ORDER BY fts_score ASC, e.updated_at DESC "
            "LIMIT ?"
        )

        with self._lock:
            try:
                rows = self._conn.execute(sql, (*params, fts_query, int(limit))).fetchall()
            except sqlite3.OperationalError:
                like_sql = (
                    "SELECT e.*, 1.0 AS fts_score "
                    "FROM memory_entries e "
                    f"WHERE {' AND '.join(where)} "
                    "AND e.content_norm LIKE ? "
                    "ORDER BY e.updated_at DESC "
                    "LIMIT ?"
                )
                rows = self._conn.execute(
                    like_sql,
                    (*params, f"%{query.lower()}%", int(limit)),
                ).fetchall()

            now_iso = datetime.now(UTC).isoformat()
            entry_ids: list[str] = []
            hits: list[MemoryHit] = []
            for row in rows:
                entry = self._row_to_entry(row)
                entry_ids.append(entry.id)
                raw_score = float(row["fts_score"] if row["fts_score"] is not None else 0.0)
                hits.append(MemoryHit(entry=entry, fts_score=raw_score))

            if entry_ids:
                placeholders = ",".join(["?"] * len(entry_ids))
                self._conn.execute(
                    f"UPDATE memory_entries SET last_accessed_at = ? WHERE id IN ({placeholders})",
                    (now_iso, *entry_ids),
                )
                self._conn.commit()

            return hits

    def list_entries(
        self,
        *,
        workspace_id: str,
        scope_keys: list[str] | None = None,
        kinds: set[MemoryKind] | None = None,
        limit: int = 100,
        include_deleted: bool = False,
    ) -> list[MemoryEntry]:
        where = ["workspace_id = ?"]
        params: list[object] = [workspace_id]

        if not include_deleted:
            where.append("is_deleted = 0")

        if scope_keys:
            placeholders = ",".join(["?"] * len(scope_keys))
            where.append(f"scope_key IN ({placeholders})")
            params.extend(scope_keys)

        if kinds:
            kind_values = sorted(kinds)
            placeholders = ",".join(["?"] * len(kind_values))
            where.append(f"kind IN ({placeholders})")
            params.extend(kind_values)

        sql = (
            "SELECT * FROM memory_entries "
            f"WHERE {' AND '.join(where)} "
            "ORDER BY updated_at DESC "
            "LIMIT ?"
        )

        with self._lock:
            rows = self._conn.execute(sql, (*params, int(limit))).fetchall()
        return [self._row_to_entry(row) for row in rows]

    def soft_delete(self, *, workspace_id: str, entry_id: str) -> bool:
        with self._lock:
            now_iso = datetime.now(UTC).isoformat()
            cur = self._conn.execute(
                """
                UPDATE memory_entries
                SET is_deleted = 1,
                    updated_at = ?
                WHERE workspace_id = ?
                  AND id = ?
                  AND is_deleted = 0
                """,
                (now_iso, workspace_id, entry_id),
            )
            changed = int(cur.rowcount or 0)
            if changed > 0:
                self._conn.execute(
                    "DELETE FROM memory_entries_fts WHERE entry_id = ?",
                    (entry_id,),
                )
            self._conn.commit()
            return changed > 0

    def prune(
        self,
        *,
        workspace_id: str,
        older_than: datetime | None = None,
        kinds: set[MemoryKind] | None = None,
        scope_keys: list[str] | None = None,
        dry_run: bool = False,
    ) -> int:
        where = ["workspace_id = ?", "is_deleted = 0"]
        params: list[object] = [workspace_id]

        if older_than is not None:
            where.append("updated_at < ?")
            params.append(older_than.astimezone(UTC).isoformat())

        if kinds:
            kind_values = sorted(kinds)
            placeholders = ",".join(["?"] * len(kind_values))
            where.append(f"kind IN ({placeholders})")
            params.extend(kind_values)

        if scope_keys:
            placeholders = ",".join(["?"] * len(scope_keys))
            where.append(f"scope_key IN ({placeholders})")
            params.extend(scope_keys)

        select_sql = f"SELECT id FROM memory_entries WHERE {' AND '.join(where)}"
        with self._lock:
            rows = self._conn.execute(select_sql, params).fetchall()
            ids = [str(row["id"]) for row in rows]
            if dry_run or not ids:
                return len(ids)

            placeholders = ",".join(["?"] * len(ids))
            now_iso = datetime.now(UTC).isoformat()
            self._conn.execute(
                f"UPDATE memory_entries SET is_deleted = 1, updated_at = ? WHERE id IN ({placeholders})",
                (now_iso, *ids),
            )
            self._conn.execute(
                f"DELETE FROM memory_entries_fts WHERE entry_id IN ({placeholders})",
                ids,
            )
            self._conn.commit()
            return len(ids)

    def prune_expired(self, *, workspace_id: str, dry_run: bool = False) -> int:
        now_iso = datetime.now(UTC).isoformat()
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id
                FROM memory_entries
                WHERE workspace_id = ?
                  AND is_deleted = 0
                  AND expires_at IS NOT NULL
                  AND expires_at < ?
                """,
                (workspace_id, now_iso),
            ).fetchall()
            ids = [str(row["id"]) for row in rows]
            if dry_run or not ids:
                return len(ids)

            placeholders = ",".join(["?"] * len(ids))
            self._conn.execute(
                f"UPDATE memory_entries SET is_deleted = 1, updated_at = ? WHERE id IN ({placeholders})",
                (now_iso, *ids),
            )
            self._conn.execute(
                f"DELETE FROM memory_entries_fts WHERE entry_id IN ({placeholders})",
                ids,
            )
            self._conn.commit()
            return len(ids)

    def stats(self, *, workspace_id: str) -> dict[str, object]:
        with self._lock:
            total_active = int(
                self._conn.execute(
                    "SELECT COUNT(*) AS c FROM memory_entries WHERE workspace_id = ? AND is_deleted = 0",
                    (workspace_id,),
                ).fetchone()["c"]
            )
            total_deleted = int(
                self._conn.execute(
                    "SELECT COUNT(*) AS c FROM memory_entries WHERE workspace_id = ? AND is_deleted = 1",
                    (workspace_id,),
                ).fetchone()["c"]
            )

            by_kind_rows = self._conn.execute(
                """
                SELECT kind, COUNT(*) AS c
                FROM memory_entries
                WHERE workspace_id = ? AND is_deleted = 0
                GROUP BY kind
                ORDER BY kind
                """,
                (workspace_id,),
            ).fetchall()
            by_scope_rows = self._conn.execute(
                """
                SELECT scope_type, COUNT(*) AS c
                FROM memory_entries
                WHERE workspace_id = ? AND is_deleted = 0
                GROUP BY scope_type
                ORDER BY scope_type
                """,
                (workspace_id,),
            ).fetchall()

        return {
            "db_path": str(self.db_path),
            "total_active": total_active,
            "total_deleted": total_deleted,
            "by_kind": {str(row["kind"]): int(row["c"]) for row in by_kind_rows},
            "by_scope": {str(row["scope_type"]): int(row["c"]) for row in by_scope_rows},
        }

    def reindex(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM memory_entries_fts")
            self._conn.execute(
                """
                INSERT INTO memory_entries_fts (entry_id, content)
                SELECT id, content_norm
                FROM memory_entries
                WHERE is_deleted = 0
                """
            )
            self._conn.commit()

    def get_meta(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM memory_meta WHERE key = ? LIMIT 1",
                (key,),
            ).fetchone()
        if row is None:
            return None
        return str(row["value"])

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO memory_meta (key, value)
                VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )
            self._conn.commit()
