"""SQLite storage backend for active semantic memory."""

from __future__ import annotations

import re
import sqlite3
import threading
import uuid
from array import array
from datetime import UTC, datetime
from pathlib import Path

from nanobot.memory.models import MemoryEntry, MemoryHit, MemorySector
from nanobot.utils.helpers import ensure_dir


class MemoryStore:
    """Persist semantic memory entries with FTS and optional embedding vectors."""

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
                CREATE TABLE IF NOT EXISTS memory2_nodes (
                    id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    scope_type TEXT NOT NULL,
                    scope_key TEXT NOT NULL,
                    channel TEXT,
                    chat_id TEXT,
                    sender_id TEXT,
                    sector TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    content TEXT NOT NULL,
                    content_norm TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    salience REAL NOT NULL,
                    confidence REAL NOT NULL,
                    source TEXT NOT NULL,
                    source_message_id TEXT,
                    source_role TEXT,
                    language TEXT,
                    meta_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_accessed_at TEXT,
                    valid_from TEXT,
                    valid_to TEXT,
                    is_deleted INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_memory2_scope_sector_updated
                ON memory2_nodes (workspace_id, scope_key, sector, is_deleted, updated_at DESC)
                """
            )
            self._conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_memory2_dedupe_active
                ON memory2_nodes (workspace_id, scope_key, sector, content_hash, is_deleted)
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory2_embeddings (
                    entry_id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    model TEXT NOT NULL,
                    dims INTEGER NOT NULL,
                    vector BLOB NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(entry_id) REFERENCES memory2_nodes(id) ON DELETE CASCADE
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory2_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS memory2_nodes_fts
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
    def _serialize_vector(vector: list[float]) -> bytes:
        packed = array("f", [float(v) for v in vector])
        return packed.tobytes()

    @staticmethod
    def _deserialize_vector(blob: bytes) -> list[float]:
        unpacked = array("f")
        unpacked.frombytes(blob)
        return unpacked.tolist()

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
            sector=str(row["sector"]),
            kind=str(row["kind"]),
            content=str(row["content"]),
            content_norm=str(row["content_norm"]),
            content_hash=str(row["content_hash"]),
            salience=float(row["salience"]),
            confidence=float(row["confidence"]),
            source=str(row["source"]),
            source_message_id=str(row["source_message_id"]) if row["source_message_id"] else None,
            source_role=str(row["source_role"]) if row["source_role"] else None,
            language=str(row["language"]) if row["language"] else None,
            meta_json=str(row["meta_json"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            last_accessed_at=str(row["last_accessed_at"]) if row["last_accessed_at"] else None,
            valid_from=str(row["valid_from"]) if row["valid_from"] else None,
            valid_to=str(row["valid_to"]) if row["valid_to"] else None,
            is_deleted=bool(int(row["is_deleted"])),
        )

    def upsert_node(
        self,
        entry: MemoryEntry,
        *,
        embedding_model: str | None = None,
        embedding: list[float] | None = None,
    ) -> tuple[MemoryEntry, bool]:
        """Insert or merge one entry. Returns (entry, inserted_new)."""
        now_iso = datetime.now(UTC).isoformat()
        with self._lock:
            existing = self._conn.execute(
                """
                SELECT *
                FROM memory2_nodes
                WHERE workspace_id = ?
                  AND scope_key = ?
                  AND sector = ?
                  AND content_hash = ?
                  AND is_deleted = 0
                LIMIT 1
                """,
                (entry.workspace_id, entry.scope_key, entry.sector, entry.content_hash),
            ).fetchone()
            if existing is not None:
                existing_entry = self._row_to_entry(existing)
                self._conn.execute(
                    """
                    UPDATE memory2_nodes
                    SET salience = ?,
                        confidence = ?,
                        updated_at = ?,
                        last_accessed_at = ?
                    WHERE id = ?
                    """,
                    (
                        max(existing_entry.salience, entry.salience),
                        max(existing_entry.confidence, entry.confidence),
                        now_iso,
                        now_iso,
                        existing_entry.id,
                    ),
                )
                row = self._conn.execute(
                    "SELECT * FROM memory2_nodes WHERE id = ? LIMIT 1",
                    (existing_entry.id,),
                ).fetchone()
                if row is None:
                    self._conn.commit()
                    return existing_entry, False
                merged = self._row_to_entry(row)
                if embedding_model and embedding is not None:
                    self._upsert_embedding(merged.id, merged.workspace_id, embedding_model, embedding)
                self._conn.commit()
                return merged, False

            entry_id = entry.id or str(uuid.uuid4())
            created_at = entry.created_at or now_iso
            updated_at = entry.updated_at or now_iso
            self._conn.execute(
                """
                INSERT INTO memory2_nodes (
                    id, workspace_id, scope_type, scope_key,
                    channel, chat_id, sender_id,
                    sector, kind, content, content_norm, content_hash,
                    salience, confidence,
                    source, source_message_id, source_role, language, meta_json,
                    created_at, updated_at, last_accessed_at, valid_from, valid_to, is_deleted
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry_id,
                    entry.workspace_id,
                    entry.scope_type,
                    entry.scope_key,
                    entry.channel,
                    entry.chat_id,
                    entry.sender_id,
                    entry.sector,
                    entry.kind,
                    entry.content,
                    entry.content_norm,
                    entry.content_hash,
                    float(entry.salience),
                    float(entry.confidence),
                    entry.source,
                    entry.source_message_id,
                    entry.source_role,
                    entry.language,
                    entry.meta_json,
                    created_at,
                    updated_at,
                    entry.last_accessed_at,
                    entry.valid_from,
                    entry.valid_to,
                    1 if entry.is_deleted else 0,
                ),
            )
            self._conn.execute(
                "INSERT INTO memory2_nodes_fts (entry_id, content) VALUES (?, ?)",
                (entry_id, entry.content_norm or entry.content),
            )
            if embedding_model and embedding is not None:
                self._upsert_embedding(entry_id, entry.workspace_id, embedding_model, embedding)
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM memory2_nodes WHERE id = ? LIMIT 1",
                (entry_id,),
            ).fetchone()
            if row is None:
                return entry, True
            return self._row_to_entry(row), True

    def _upsert_embedding(
        self,
        entry_id: str,
        workspace_id: str,
        model: str,
        vector: list[float],
    ) -> None:
        payload = self._serialize_vector(vector)
        now_iso = datetime.now(UTC).isoformat()
        self._conn.execute(
            """
            INSERT INTO memory2_embeddings (entry_id, workspace_id, model, dims, vector, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(entry_id) DO UPDATE SET
              model = excluded.model,
              dims = excluded.dims,
              vector = excluded.vector,
              created_at = excluded.created_at
            """,
            (entry_id, workspace_id, model, len(vector), payload, now_iso),
        )

    def search_lexical(
        self,
        *,
        workspace_id: str,
        query: str,
        scope_keys: list[str],
        sectors: set[MemorySector] | None = None,
        limit: int = 12,
    ) -> list[MemoryHit]:
        if not scope_keys:
            return []
        fts_query = self._normalize_query(query)
        if not fts_query:
            return []

        scope_placeholders = ",".join(["?"] * len(scope_keys))
        where = [
            "n.workspace_id = ?",
            "n.is_deleted = 0",
            f"n.scope_key IN ({scope_placeholders})",
        ]
        params: list[object] = [workspace_id, *scope_keys]
        if sectors:
            sector_values = sorted(sectors)
            sector_placeholders = ",".join(["?"] * len(sector_values))
            where.append(f"n.sector IN ({sector_placeholders})")
            params.extend(sector_values)
        sql = (
            "SELECT n.*, bm25(memory2_nodes_fts) AS fts_score "
            "FROM memory2_nodes_fts "
            "JOIN memory2_nodes n ON n.id = memory2_nodes_fts.entry_id "
            f"WHERE {' AND '.join(where)} "
            "AND memory2_nodes_fts MATCH ? "
            "ORDER BY fts_score ASC, n.updated_at DESC "
            "LIMIT ?"
        )

        with self._lock:
            try:
                rows = self._conn.execute(sql, (*params, fts_query, int(limit))).fetchall()
            except sqlite3.OperationalError:
                like_sql = (
                    "SELECT n.*, 1.0 AS fts_score "
                    "FROM memory2_nodes n "
                    f"WHERE {' AND '.join(where)} "
                    "AND n.content_norm LIKE ? "
                    "ORDER BY n.updated_at DESC "
                    "LIMIT ?"
                )
                rows = self._conn.execute(
                    like_sql,
                    (*params, f"%{query.lower()}%", int(limit)),
                ).fetchall()

            now_iso = datetime.now(UTC).isoformat()
            hit_ids: list[str] = []
            hits: list[MemoryHit] = []
            for row in rows:
                entry = self._row_to_entry(row)
                hit_ids.append(entry.id)
                raw = float(row["fts_score"] if row["fts_score"] is not None else 0.0)
                lexical_score = 1.0 / (1.0 + max(0.0, raw))
                hits.append(MemoryHit(entry=entry, lexical_score=lexical_score))

            if hit_ids:
                placeholders = ",".join(["?"] * len(hit_ids))
                self._conn.execute(
                    f"UPDATE memory2_nodes SET last_accessed_at = ? WHERE id IN ({placeholders})",
                    (now_iso, *hit_ids),
                )
                self._conn.commit()
            return hits

    def search_vector(
        self,
        *,
        workspace_id: str,
        query_vector: list[float],
        scope_keys: list[str],
        sectors: set[MemorySector] | None = None,
        limit: int = 12,
        candidate_limit: int = 256,
    ) -> list[MemoryHit]:
        if not scope_keys or not query_vector:
            return []
        scope_placeholders = ",".join(["?"] * len(scope_keys))
        where = [
            "n.workspace_id = ?",
            "n.is_deleted = 0",
            f"n.scope_key IN ({scope_placeholders})",
        ]
        params: list[object] = [workspace_id, *scope_keys]
        if sectors:
            sector_values = sorted(sectors)
            sector_placeholders = ",".join(["?"] * len(sector_values))
            where.append(f"n.sector IN ({sector_placeholders})")
            params.extend(sector_values)
        sql = (
            "SELECT n.*, e.vector "
            "FROM memory2_nodes n "
            "JOIN memory2_embeddings e ON e.entry_id = n.id "
            f"WHERE {' AND '.join(where)} "
            "ORDER BY n.updated_at DESC "
            "LIMIT ?"
        )

        with self._lock:
            rows = self._conn.execute(sql, (*params, int(candidate_limit))).fetchall()
            now_iso = datetime.now(UTC).isoformat()
            hit_ids: list[str] = []
            scored: list[MemoryHit] = []
            for row in rows:
                blob = row["vector"]
                if blob is None:
                    continue
                node_vector = self._deserialize_vector(bytes(blob))
                if len(node_vector) != len(query_vector):
                    continue
                sim = _cosine_similarity(query_vector, node_vector)
                if sim <= 0.0:
                    continue
                entry = self._row_to_entry(row)
                hit_ids.append(entry.id)
                scored.append(MemoryHit(entry=entry, vector_score=max(0.0, min(1.0, sim))))

            scored.sort(key=lambda h: h.vector_score, reverse=True)
            hits = scored[: max(1, int(limit))]
            if hit_ids:
                placeholders = ",".join(["?"] * len(hit_ids))
                self._conn.execute(
                    f"UPDATE memory2_nodes SET last_accessed_at = ? WHERE id IN ({placeholders})",
                    (now_iso, *hit_ids),
                )
                self._conn.commit()
            return hits

    def stats(self, *, workspace_id: str) -> dict[str, int]:
        with self._lock:
            total_nodes = self._conn.execute(
                "SELECT COUNT(*) AS c FROM memory2_nodes WHERE workspace_id = ? AND is_deleted = 0",
                (workspace_id,),
            ).fetchone()
            total_embeddings = self._conn.execute(
                "SELECT COUNT(*) AS c FROM memory2_embeddings WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone()
        return {
            "nodes": int(total_nodes["c"] if total_nodes else 0),
            "embeddings": int(total_embeddings["c"] if total_embeddings else 0),
        }

    def reindex(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM memory2_nodes_fts")
            self._conn.execute(
                """
                INSERT INTO memory2_nodes_fts (entry_id, content)
                SELECT id, content_norm
                FROM memory2_nodes
                WHERE is_deleted = 0
                """
            )
            self._conn.commit()


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b, strict=False):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    return dot / ((norm_a ** 0.5) * (norm_b ** 0.5))
