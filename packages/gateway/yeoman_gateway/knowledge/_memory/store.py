"""Read the shared embedding index from the Knowledge store."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from typing import Any


class MemoryStore:
    """Read-only view of embedding rows owned by a KnowledgeStore."""

    def __init__(self, *, owner: object, source_authority: object | None = None) -> None:
        self._owner = owner
        self._lock = getattr(owner, "lock")
        self._conn = getattr(owner, "connection")
        self._conn.row_factory = sqlite3.Row
        self.source_authority = source_authority

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())

    def query_one(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    @staticmethod
    def _deserialize_vector(blob: bytes) -> list[float]:
        from array import array

        values = array("f")
        values.frombytes(blob)
        return list(values)

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
        """Return compatible, live sections after provenance and revocation checks."""
        if not scope_keys or not query_vector:
            return []
        clauses = ["workspace_id = ?", "status = 'active'", "dimension = ?"]
        params: list[Any] = [str(workspace_id), len(query_vector)]
        if model_id is not None:
            clauses.append("model_id = ?")
            params.append(str(model_id))
        if preprocessing_version is not None:
            clauses.append("preprocessing_version = ?")
            params.append(str(preprocessing_version))
        clauses.append(f"scope_key IN ({','.join('?' for _ in scope_keys)})")
        params.extend(str(item) for item in scope_keys)
        rows = self.query(
            f"SELECT * FROM memory2_embedding_index WHERE {' AND '.join(clauses)}"
            " ORDER BY created_ms DESC LIMIT ?",
            (*params, int(candidate_limit)),
        )
        scored = []
        for row in rows:
            if row["vector"] is None:
                continue
            item = {key: row[key] for key in row.keys()}
            item["conversation_ids"] = _decode_ids(item.get("conversation_ids_json"))
            score = _cosine_similarity(query_vector, self._deserialize_vector(bytes(row["vector"])))
            if score > 0 and self._source_still_readable(item):
                scored.append((score, item))
        result = []
        for score, item in sorted(scored, key=lambda pair: (-pair[0], pair[1]["document_id"])):
            item["score"] = max(0.0, min(1.0, float(score)))
            result.append(item)
            if len(result) >= max(1, int(limit)):
                break
        return result

    def _source_still_readable(self, item: Mapping[str, Any]) -> bool:
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
        authority = self.source_authority
        getter = getattr(authority, "verify_source_ref", None)
        revoked = getattr(authority, "source_revoked", None)
        if not callable(getter):
            return True
        source = getter(event_id, int(item.get("source_revision") or 0))
        return source is not None and not (callable(revoked) and revoked(source))


def _decode_ids(raw: object) -> list[str]:
    try:
        parsed = json.loads(str(raw or "[]"))
    except (TypeError, ValueError):
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    return dot / (norm_a * norm_b) if norm_a and norm_b else 0.0
