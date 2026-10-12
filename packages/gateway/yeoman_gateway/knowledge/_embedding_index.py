"""Read-only view of the existing, rebuildable Knowledge embedding index."""
from __future__ import annotations

import heapq
import json
import math
import sqlite3
import sys
import threading
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
        self._cache_lock = threading.RLock()
        self._caches: dict[tuple[Any, ...], dict[str, Any]] = {}

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
        pool: int = 128,
        model_id: str | None = None,
        preprocessing_version: str | None = None,
        allowed_node_ids: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Nearest sections with a *compatible* document key.

        The dimension is always bound to the query vector's length, and the model and
        preprocessing version are bound whenever the caller names them, so a stale index
        can never be scored against a different version.  Rows whose source is no longer
        proven and unrevoked are dropped here, immediately before output.
        """
        del candidate_limit  # Compatibility is independent of row age.
        if not query_vector or not all(math.isfinite(x) for x in query_vector):
            return []

        key = (workspace_id, model_id, len(query_vector), preprocessing_version)
        with self._cache_lock:
            cache = self._refresh_cache(key)
            norm = math.sqrt(math.sumprod(query_vector, query_vector))
            if norm <= 0 or not math.isfinite(norm):
                return []
            q = array("f", query_vector)
            norm = math.sqrt(math.sumprod(q, q))
            if norm <= 0 or not math.isfinite(norm):
                return []
            fingerprint = _fingerprint(q.tobytes())
            candidates = (((code ^ fingerprint).bit_count(), item, vector, vector_norm)
                          for items, vectors in cache["blocks"]
                          for item, (vector, code, vector_norm) in zip(items, vectors, strict=True)
                          if (not scope_keys or item["scope_key"] in scope_keys)
                          and (allowed_node_ids is None or item["node_id"] in allowed_node_ids))
            nearest = heapq.nsmallest(max(1, int(pool)), candidates, key=lambda row: (row[0], row[1]["document_id"]))
            scored = []
            for _, item, vector, vector_norm in nearest:
                score = math.sumprod(vector, q) / (vector_norm * norm)
                if score > 0:
                    scored.append((score, item))
            best = heapq.nlargest(max(1, int(limit)), scored,
                                 key=lambda pair: (pair[0], pair[1]["document_id"]))
            return [dict(item, score=min(1.0, score)) for score, item in best
                    if self._source_still_readable(item)]


    def warm_cache(self, *, workspace_id: str, model_id: str, dimension: int,
                   preprocessing_version: str) -> None:
        with self._cache_lock:
            self._refresh_cache((workspace_id, model_id, dimension, preprocessing_version))

    def _refresh_cache(self, key: tuple[Any, ...]) -> dict[str, Any]:
        workspace_id, model_id, dimension, preprocessing_version = key
        cache = self._caches.setdefault(key, {"stamp": None, "blocks": []})
        with self._lock:
            stamp = (self._conn.total_changes, self._conn.execute("PRAGMA data_version").fetchone()[0])
            if cache["stamp"] == stamp:
                return cache
            clauses = "workspace_id=? AND dimension=?"
            params: list[Any] = [workspace_id, dimension]
            for name, value in (("model_id", model_id), ("preprocessing_version", preprocessing_version)):
                if value is not None:
                    clauses += f" AND {name}=?"
                    params.append(value)
            active = {row[0] for row in self._conn.execute(
                f"SELECT rowid FROM memory2_embedding_index WHERE {clauses} AND status='active'", params
            )}
        blocks, cached = [], set()
        for items, vectors in cache["blocks"]:
            keep = [i for i, item in enumerate(items) if item["cache_id"] in active]
            if keep:
                retained = [items[i] for i in keep]
                blocks.append((retained, vectors if len(keep) == len(items) else [vectors[i] for i in keep]))
                cached.update(item["cache_id"] for item in retained)
        missing = sorted(active - cached)
        for start in range(0, len(missing), 256):
            ids = missing[start:start + 256]
            placeholders = ",".join("?" for _ in ids)
            with self._lock:
                rows = self._conn.execute(
                    f"SELECT rowid AS cache_id, * FROM memory2_embedding_index WHERE rowid IN ({placeholders}) AND status='active' ORDER BY rowid", ids
                ).fetchall()
            items, vectors = [], []
            for row in rows:
                blob = row["vector"]
                vector = array("f", blob)
                if len(vector) != dimension:
                    continue
                norm = math.sqrt(math.sumprod(vector, vector))
                if norm <= 0 or not math.isfinite(norm):
                    continue
                item = _embedding_index_row(row)
                del item["vector"]
                items.append(item)
                vectors.append((vector, _fingerprint(blob), norm))
            if vectors:
                blocks.append((items, vectors))
        cache["blocks"] = blocks
        # Any concurrent writer leaves this stamp stale, forcing reconciliation next time.
        cache["stamp"] = stamp
        return cache


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
    item = dict(zip(row.keys(), row, strict=True))
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



_SIGN_TRANSLATION = b"0" * 128 + b"1" * 128
_SIGN_BYTE = 3 if sys.byteorder == "little" else 0


def _fingerprint(blob: bytes) -> int:
    # Signed zero retains its sign in both stored and query float32 fingerprints.
    return int(blob[_SIGN_BYTE::4].translate(_SIGN_TRANSLATION), 2)
