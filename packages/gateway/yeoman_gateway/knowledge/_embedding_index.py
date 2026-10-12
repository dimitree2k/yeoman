"""Read-only view of the existing, rebuildable Knowledge embedding index."""
from __future__ import annotations

import json
import sqlite3
from array import array
from collections.abc import Mapping
from typing import Any

EMBEDDING_PREPROCESSING_VERSION = "memory-text-v1"

_EMBEDDING_INDEX_SCHEMA: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS memory2_embedding_jobs (
        job_key TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        node_id TEXT NOT NULL,
        scope_key TEXT NOT NULL DEFAULT '',
        channel TEXT NOT NULL DEFAULT '',
        chat_id TEXT NOT NULL DEFAULT '',
        source_event_id TEXT NOT NULL DEFAULT '',
        source_revision INTEGER NOT NULL DEFAULT 0,
        source_revision_hash TEXT NOT NULL DEFAULT '',
        model_id TEXT NOT NULL DEFAULT '',
        dimension INTEGER NOT NULL DEFAULT 0,
        preprocessing_version TEXT NOT NULL DEFAULT '',
        conversation_ids_json TEXT NOT NULL DEFAULT '[]',
        source_refs_json TEXT NOT NULL DEFAULT '[]',
        page_number INTEGER,
        state TEXT NOT NULL
            CHECK(state IN ('queued','running','done','failed','skipped','cancelled')),
        reason TEXT,
        attempts INTEGER NOT NULL DEFAULT 0,
        due_ms INTEGER NOT NULL,
        created_ms INTEGER NOT NULL,
        updated_ms INTEGER NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_memory2_embedding_jobs_due
      ON memory2_embedding_jobs (state, due_ms)
    """,
    """
    CREATE TABLE IF NOT EXISTS memory2_embedding_index (
        document_id TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        source_revision_hash TEXT NOT NULL,
        model_id TEXT NOT NULL,
        dimension INTEGER NOT NULL,
        preprocessing_version TEXT NOT NULL,
        workspace_id TEXT NOT NULL,
        scope_key TEXT NOT NULL DEFAULT '',
        channel TEXT NOT NULL DEFAULT '',
        chat_id TEXT NOT NULL DEFAULT '',
        source_event_id TEXT NOT NULL DEFAULT '',
        source_revision INTEGER NOT NULL DEFAULT 0,
        node_id TEXT NOT NULL DEFAULT '',
        section_index INTEGER NOT NULL DEFAULT 0,
        section_start INTEGER NOT NULL DEFAULT 0,
        section_end INTEGER NOT NULL DEFAULT 0,
        page_number INTEGER,
        conversation_ids_json TEXT NOT NULL DEFAULT '[]',
        vector BLOB NOT NULL,
        status TEXT NOT NULL DEFAULT 'active'
            CHECK(status IN ('active','retired')),
        created_ms INTEGER NOT NULL,
        retired_ms INTEGER,
        PRIMARY KEY (document_id, content_hash, source_revision_hash, model_id,
                     dimension, preprocessing_version)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_memory2_embedding_index_active
      ON memory2_embedding_index (workspace_id, scope_key, status, model_id, dimension)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_memory2_embedding_index_source
      ON memory2_embedding_index (source_event_id, source_revision)
    """,
)


class EmbeddingIndex:
    """Join the Knowledge owner connection without schema or transaction changes."""

    def __init__(self, *, owner: object, source_authority: object | None = None) -> None:
        self.source_authority = source_authority
        self._lock = getattr(owner, "lock")
        self._conn = getattr(owner, "connection")

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())


    def query_one(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()




    @staticmethod
    def _deserialize_vector(blob: bytes) -> list[float]:
        unpacked = array("f")
        unpacked.frombytes(blob)
        return unpacked.tolist()


    def search_embedding_index(
        self,
        *,
        workspace_id: str,
        query_vector: list[float],
        scope_keys: list[str],
        limit: int = 12,
        candidate_limit: int = 256,
        model_id: str | None = None,
        preprocessing_version: str | None = None,
    ) -> list[dict[str, Any]]:
        """Nearest sections with a *compatible* document key.

        The dimension is always bound to the query vector's length, and the model and
        preprocessing version are bound whenever the caller names them, so a stale index
        can never be scored against a different version.  Rows whose source is no longer
        proven and unrevoked are dropped here, immediately before output.
        """
        if not scope_keys or not query_vector:
            return []
        clauses = [
            "workspace_id = ?",
            "status = 'active'",
            "dimension = ?",
        ]
        params: list[Any] = [str(workspace_id), len(query_vector)]
        if model_id is not None:
            clauses.append("model_id = ?")
            params.append(str(model_id))
        if preprocessing_version is not None:
            clauses.append("preprocessing_version = ?")
            params.append(str(preprocessing_version))
        scope_placeholders = ",".join("?" for _ in scope_keys)
        clauses.append(f"scope_key IN ({scope_placeholders})")
        params.extend(str(item) for item in scope_keys)
        rows = self.query(
            f"SELECT * FROM memory2_embedding_index WHERE {' AND '.join(clauses)}"
            " ORDER BY created_ms DESC LIMIT ?",
            (*params, int(candidate_limit)),
        )
        scored: list[tuple[float, dict[str, Any]]] = []
        for row in rows:
            payload = row["vector"]
            if payload is None:
                continue
            scored.append(
                (
                    _cosine_similarity(query_vector, self._deserialize_vector(bytes(payload))),
                    _embedding_index_row(row),
                )
            )
        kept: list[dict[str, Any]] = []
        for score, item in sorted(scored, key=lambda pair: (-pair[0], pair[1]["document_id"])):
            if score <= 0.0:
                continue
            if not self._source_still_readable(item):
                continue
            item["score"] = max(0.0, min(1.0, float(score)))
            kept.append(item)
            if len(kept) >= max(1, int(limit)):
                break
        return kept


    def _source_still_readable(self, item: Mapping[str, Any]) -> bool:
        """Re-check provenance and revocation against the live authority, if there is one."""
        authority = self.source_authority
        event_id = str(item.get("source_event_id") or "")
        if not event_id:
            return True
        node_id = str(item.get("node_id") or "")
        if node_id:
            row = self.query_one(
                "SELECT is_deleted FROM memory2_nodes WHERE id = ? LIMIT 1", (node_id,)
            )
            if row is not None and bool(int(row["is_deleted"])):
                return False
        if authority is None:
            return True
        getter = getattr(authority, "verify_source_ref", None)
        revoked = getattr(authority, "source_revoked", None)
        if not callable(getter):
            return True
        source = getter(event_id, int(item.get("source_revision") or 0))
        if source is None:
            return False
        return not (callable(revoked) and revoked(source))


def _embedding_index_row(row: sqlite3.Row) -> dict[str, Any]:
    item = {key: row[key] for key in row.keys()}
    item["conversation_ids"] = _decode_ids(item.get("conversation_ids_json"))
    return item


def _decode_ids(raw: object) -> list[str]:
    try:
        parsed = json.loads(str(raw or "[]"))
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed]


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
    return dot / ((norm_a**0.5) * (norm_b**0.5))

