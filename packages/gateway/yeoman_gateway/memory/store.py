"""SQLite storage backend for active semantic memory."""

from __future__ import annotations

import re
import sqlite3
import threading
import uuid
from array import array
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from yeoman_shared.utils.helpers import ensure_dir

from yeoman_gateway.memory.models import MemoryEntry, MemoryHit, MemorySector
from yeoman_gateway.memory.read_gate import FactAclPredicate
from yeoman_gateway.memory.shared_facts import (
    ASSERTION_STATUSES,
    FactSource,
    SharedFact,
    fact_content_hash,
)

# Plan 05, Aufgabe 1: additive shared-fact schema. Never touches memory2_nodes.
_SHARED_FACT_SCHEMA: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS memory2_facts (
      fact_id TEXT PRIMARY KEY REFERENCES memory2_nodes(id) ON DELETE CASCADE,
      workspace_id TEXT NOT NULL,
      chat_scope_key TEXT NOT NULL,
      author_principal TEXT NOT NULL,
      assertion_status TEXT NOT NULL
        CHECK(assertion_status IN ('assertion','confirmed','superseded','revoked','expired')),
      visibility_scope TEXT NOT NULL CHECK(visibility_scope IN ('chat_shared','principals','author_only')),
      group_rule TEXT NOT NULL
        CHECK(group_rule IN ('chat_members_at_source','explicit_principals','author_only','none')),
      audience_snapshot_id TEXT,
      valid_from_ms INTEGER NOT NULL,
      valid_until_ms INTEGER,
      superseded_by TEXT REFERENCES memory2_facts(fact_id),
      revoked_at_ms INTEGER,
      extractor_version TEXT NOT NULL,
      created_ms INTEGER NOT NULL,
      updated_ms INTEGER NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_memory2_facts_scope
      ON memory2_facts (workspace_id, chat_scope_key, assertion_status, valid_until_ms)
    """,
    """
    CREATE TABLE IF NOT EXISTS memory2_fact_sources (
      fact_id TEXT NOT NULL REFERENCES memory2_facts(fact_id) ON DELETE CASCADE,
      source_event_id TEXT NOT NULL,
      source_revision INTEGER NOT NULL,
      source_trace_id TEXT NOT NULL DEFAULT '',
      author_principal TEXT NOT NULL,
      source_channel TEXT NOT NULL DEFAULT '',
      source_chat_id TEXT NOT NULL DEFAULT '',
      occurred_ms INTEGER,
      PRIMARY KEY (fact_id, source_event_id, source_revision)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memory2_fact_principals (
      fact_id TEXT NOT NULL REFERENCES memory2_facts(fact_id) ON DELETE CASCADE,
      principal_id TEXT NOT NULL,
      role TEXT NOT NULL CHECK(role IN ('allowed','audience')),
      PRIMARY KEY (fact_id, principal_id, role)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memory2_fact_jobs (
      job_key TEXT PRIMARY KEY,
      workspace_id TEXT NOT NULL,
      chat_scope_key TEXT NOT NULL,
      source_refs_json TEXT NOT NULL,
      extractor_version TEXT NOT NULL,
      state TEXT NOT NULL CHECK(state IN ('queued','running','done','skipped','cancelled','failed')),
      reason TEXT,
      first_activity_ms INTEGER NOT NULL,
      last_activity_ms INTEGER NOT NULL,
      due_ms INTEGER NOT NULL,
      attempts INTEGER NOT NULL DEFAULT 0,
      created_ms INTEGER NOT NULL,
      updated_ms INTEGER NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_memory2_fact_jobs_due ON memory2_fact_jobs (state, due_ms)
    """,
)


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
                CREATE TABLE IF NOT EXISTS idea_backlog_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    stage TEXT NOT NULL CHECK(stage IN ('inbox', 'backlog')),
                    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open', 'done', 'archived')),
                    title TEXT NOT NULL,
                    details TEXT NOT NULL DEFAULT '',
                    priority INTEGER CHECK(priority BETWEEN 1 AND 5 OR priority IS NULL),
                    tags TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT 'manual',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    promoted_at TEXT
                )
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_idea_backlog_stage_status
                ON idea_backlog_items (stage, status, created_at DESC)
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_idea_backlog_priority
                ON idea_backlog_items (stage, priority DESC, created_at DESC)
                """
            )
            self._conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS memory2_nodes_fts
                USING fts5(entry_id UNINDEXED, content)
                """
            )
            # Migration: add contact_id column if missing
            try:
                self._conn.execute("SELECT contact_id FROM memory2_nodes LIMIT 0")
            except sqlite3.OperationalError:
                self._conn.execute("ALTER TABLE memory2_nodes ADD COLUMN contact_id TEXT")
            self._migrate_shared_facts()
            self._conn.commit()

    def _migrate_shared_facts(self) -> None:
        """Additive shared-fact schema (Plan 05).

        No column is added to ``memory2_nodes``: an existing row therefore has no fact
        row and can never acquire shared-fact read rights by accident.
        """
        with self._lock:
            for statement in _SHARED_FACT_SCHEMA:
                self._conn.execute(statement)
            self._conn.execute(
                "INSERT OR IGNORE INTO memory2_meta (key, value)"
                " VALUES ('memory_schema_version', '2')"
            )
            self._conn.execute(
                "INSERT OR IGNORE INTO memory2_meta (key, value) VALUES ('acl_epoch', '1')"
            )

    def get_meta(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM memory2_meta WHERE key = ? LIMIT 1", (str(key),)
            ).fetchone()
        return None if row is None else str(row["value"])

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO memory2_meta (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (str(key), str(value)),
            )
            self._conn.commit()

    def fact_audience(self, fact_id: str) -> tuple[str, ...]:
        """Stored audience rows of a fact. Missing rows mean nobody, never everybody."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT principal_id FROM memory2_fact_principals"
                " WHERE fact_id = ? AND role = 'audience' ORDER BY principal_id",
                (str(fact_id),),
            ).fetchall()
        return tuple(str(row["principal_id"]) for row in rows)

    def fact_allowed_principals(self, fact_id: str) -> tuple[str, ...]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT principal_id FROM memory2_fact_principals"
                " WHERE fact_id = ? AND role = 'allowed' ORDER BY principal_id",
                (str(fact_id),),
            ).fetchall()
        return tuple(str(row["principal_id"]) for row in rows)

    def select_fact_ids(
        self,
        *,
        sql: str,
        params: tuple[Any, ...] = (),
        limit: int | None = None,
    ) -> list[str]:
        """Fact ids matching a gate predicate. The predicate is parameterised SQL."""
        query = f"SELECT n.id FROM memory2_nodes n WHERE n.is_deleted = 0 AND ({sql})"
        bound: tuple[Any, ...] = tuple(params)
        if limit is not None:
            query += " LIMIT ?"
            bound = (*bound, int(limit))
        with self._lock:
            rows = self._conn.execute(query, bound).fetchall()
        return [str(row["id"]) for row in rows]

    def list_fact_sources(self, fact_id: str) -> tuple[FactSource, ...]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM memory2_fact_sources
                 WHERE fact_id = ?
                 ORDER BY source_event_id, source_revision
                """,
                (str(fact_id),),
            ).fetchall()
        return tuple(
            FactSource(
                source_event_id=str(row["source_event_id"]),
                source_revision=int(row["source_revision"]),
                source_trace_id=str(row["source_trace_id"] or ""),
                author_principal=str(row["author_principal"] or ""),
                source_channel=str(row["source_channel"] or ""),
                source_chat_id=str(row["source_chat_id"] or ""),
                occurred_ms=None if row["occurred_ms"] is None else int(row["occurred_ms"]),
            )
            for row in rows
        )

    def facts_by_source_event(self, source_event_id: str) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT fact_id FROM memory2_fact_sources WHERE source_event_id = ?",
                (str(source_event_id),),
            ).fetchall()
        return [str(row["fact_id"]) for row in rows]

    def delete_fact_sources(self, fact_id: str, *, source_event_ids: list[str]) -> int:
        if not source_event_ids:
            return 0
        placeholders = ",".join(["?"] * len(source_event_ids))
        with self._lock:
            cursor = self._conn.execute(
                f"DELETE FROM memory2_fact_sources WHERE fact_id = ?"
                f" AND source_event_id IN ({placeholders})",
                (str(fact_id), *[str(item) for item in source_event_ids]),
            )
            self._conn.commit()
        return int(cursor.rowcount)

    def upsert_fact_job(
        self,
        *,
        job_key: str,
        workspace_id: str,
        chat_scope_key: str,
        source_refs_json: str,
        extractor_version: str,
        state: str,
        due_ms: int,
        now_ms: int,
        reason: str | None = None,
        first_activity_ms: int | None = None,
        last_activity_ms: int | None = None,
        attempts: int | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO memory2_fact_jobs (
                    job_key, workspace_id, chat_scope_key, source_refs_json,
                    extractor_version, state, reason, first_activity_ms,
                    last_activity_ms, due_ms, attempts, created_ms, updated_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_key) DO UPDATE SET
                    source_refs_json = excluded.source_refs_json,
                    state = excluded.state,
                    reason = COALESCE(excluded.reason, memory2_fact_jobs.reason),
                    first_activity_ms = MIN(memory2_fact_jobs.first_activity_ms, excluded.first_activity_ms),
                    last_activity_ms = excluded.last_activity_ms,
                    due_ms = excluded.due_ms,
                    attempts = COALESCE(?, memory2_fact_jobs.attempts),
                    updated_ms = excluded.updated_ms
                """,
                (
                    str(job_key),
                    str(workspace_id),
                    str(chat_scope_key),
                    str(source_refs_json),
                    str(extractor_version),
                    str(state),
                    reason,
                    int(first_activity_ms if first_activity_ms is not None else now_ms),
                    int(last_activity_ms if last_activity_ms is not None else now_ms),
                    int(due_ms),
                    int(attempts or 0),
                    int(now_ms),
                    int(now_ms),
                    None if attempts is None else int(attempts),
                ),
            )
            self._conn.commit()

    def get_fact_job(self, job_key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM memory2_fact_jobs WHERE job_key = ? LIMIT 1", (str(job_key),)
            ).fetchone()
        return None if row is None else {key: row[key] for key in row.keys()}

    def list_fact_jobs(
        self, *, state: str | None = None, due_before_ms: int | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if state is not None:
            clauses.append("state = ?")
            params.append(str(state))
        if due_before_ms is not None:
            clauses.append("due_ms <= ?")
            params.append(int(due_before_ms))
        where = "" if not clauses else " WHERE " + " AND ".join(clauses)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM memory2_fact_jobs{where} ORDER BY due_ms, job_key LIMIT ?",
                (*params, int(limit)),
            ).fetchall()
        return [{key: row[key] for key in row.keys()} for row in rows]

    def count_fact_jobs(self, *, state: str | None = None) -> int:
        with self._lock:
            if state is None:
                row = self._conn.execute("SELECT COUNT(*) AS c FROM memory2_fact_jobs").fetchone()
            else:
                row = self._conn.execute(
                    "SELECT COUNT(*) AS c FROM memory2_fact_jobs WHERE state = ?", (str(state),)
                ).fetchone()
        return int(row["c"])

    def has_fact_embeddings(self) -> bool:
        """True when at least one shared fact carries a vector.

        Facts are written without embeddings today, so the semantic half of a shared-fact
        retrieval would otherwise pay for a query embedding and match nothing.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM memory2_embeddings e"
                " JOIN memory2_facts f ON f.fact_id = e.entry_id LIMIT 1"
            ).fetchone()
        return row is not None

    def acl_epoch(self) -> int:
        raw = self.get_meta("acl_epoch")
        try:
            return int(raw) if raw is not None else 0
        except ValueError:
            return 0

    def bump_acl_epoch(self) -> int:
        """Invalidate every cached permission decision after a rights change."""
        with self._lock:
            next_epoch = self.acl_epoch() + 1
            self._conn.execute(
                """
                INSERT INTO memory2_meta (key, value) VALUES ('acl_epoch', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (str(next_epoch),),
            )
            self._conn.commit()
        return next_epoch

    # -- shared facts (Plan 05, Aufgabe 1) --------------------------------------

    def upsert_fact(self, fact: SharedFact) -> SharedFact:
        """Write node, fact row, sources and principals; idempotent per fact id."""
        now_ms = int(fact.updated_ms or fact.created_ms or 0)
        entry = MemoryEntry(
            id=fact.fact_id,
            workspace_id=fact.workspace_id,
            scope_type="chat",
            scope_key=fact.chat_scope_key,
            sector="semantic",
            kind="shared_fact",
            content=fact.content,
            content_norm=fact.content.strip().lower(),
            content_hash=fact_content_hash(fact.fact_id, fact.content),
            salience=0.6,
            confidence=0.6,
            source="shared_fact",
        )
        self.upsert_node(entry)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO memory2_facts (
                    fact_id, workspace_id, chat_scope_key, author_principal,
                    assertion_status, visibility_scope, group_rule, audience_snapshot_id,
                    valid_from_ms, valid_until_ms, superseded_by, revoked_at_ms,
                    extractor_version, created_ms, updated_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(fact_id) DO UPDATE SET
                    assertion_status = excluded.assertion_status,
                    visibility_scope = excluded.visibility_scope,
                    group_rule = excluded.group_rule,
                    audience_snapshot_id = excluded.audience_snapshot_id,
                    valid_until_ms = excluded.valid_until_ms,
                    superseded_by = excluded.superseded_by,
                    revoked_at_ms = excluded.revoked_at_ms,
                    updated_ms = excluded.updated_ms
                """,
                (
                    fact.fact_id,
                    fact.workspace_id,
                    fact.chat_scope_key,
                    fact.author_principal,
                    fact.assertion_status,
                    fact.visibility_scope,
                    fact.group_rule,
                    fact.audience_snapshot_id,
                    int(fact.valid_from_ms),
                    None if fact.valid_until_ms is None else int(fact.valid_until_ms),
                    fact.superseded_by,
                    None if fact.revoked_at_ms is None else int(fact.revoked_at_ms),
                    fact.extractor_version,
                    int(fact.created_ms or now_ms),
                    now_ms,
                ),
            )
            self._conn.execute(
                "DELETE FROM memory2_fact_sources WHERE fact_id = ?", (fact.fact_id,)
            )
            for source in fact.sources:
                self._conn.execute(
                    """
                    INSERT OR IGNORE INTO memory2_fact_sources (
                        fact_id, source_event_id, source_revision, source_trace_id,
                        author_principal, source_channel, source_chat_id, occurred_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        fact.fact_id,
                        source.source_event_id,
                        int(source.source_revision),
                        source.source_trace_id,
                        source.author_principal,
                        source.source_channel,
                        source.source_chat_id,
                        None if source.occurred_ms is None else int(source.occurred_ms),
                    ),
                )
            self._conn.execute(
                "DELETE FROM memory2_fact_principals WHERE fact_id = ?", (fact.fact_id,)
            )
            for principal in sorted(fact.audience):
                self._conn.execute(
                    "INSERT OR IGNORE INTO memory2_fact_principals (fact_id, principal_id, role)"
                    " VALUES (?, ?, 'audience')",
                    (fact.fact_id, principal),
                )
            for principal in sorted(fact.allowed_principals):
                self._conn.execute(
                    "INSERT OR IGNORE INTO memory2_fact_principals (fact_id, principal_id, role)"
                    " VALUES (?, ?, 'allowed')",
                    (fact.fact_id, principal),
                )
            self._conn.commit()
        stored = self.get_fact(fact.fact_id)
        if stored is None:  # pragma: no cover - defensive
            raise RuntimeError(f"fact vanished right after write: {fact.fact_id}")
        return stored

    def get_fact(self, fact_id: str) -> SharedFact | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM memory2_facts WHERE fact_id = ? LIMIT 1", (str(fact_id),)
            ).fetchone()
            if row is None:
                return None
            node = self._conn.execute(
                "SELECT content FROM memory2_nodes WHERE id = ? LIMIT 1", (str(fact_id),)
            ).fetchone()
            source_rows = self._conn.execute(
                """
                SELECT * FROM memory2_fact_sources
                 WHERE fact_id = ?
                 ORDER BY source_event_id, source_revision
                """,
                (str(fact_id),),
            ).fetchall()
            principal_rows = self._conn.execute(
                "SELECT principal_id, role FROM memory2_fact_principals WHERE fact_id = ?",
                (str(fact_id),),
            ).fetchall()
        audience = frozenset(
            str(r["principal_id"]) for r in principal_rows if str(r["role"]) == "audience"
        )
        allowed = frozenset(
            str(r["principal_id"]) for r in principal_rows if str(r["role"]) == "allowed"
        )
        return SharedFact(
            fact_id=str(row["fact_id"]),
            workspace_id=str(row["workspace_id"]),
            chat_scope_key=str(row["chat_scope_key"]),
            content="" if node is None else str(node["content"]),
            author_principal=str(row["author_principal"]),
            assertion_status=str(row["assertion_status"]),  # type: ignore[arg-type]
            visibility_scope=str(row["visibility_scope"]),  # type: ignore[arg-type]
            group_rule=str(row["group_rule"]),  # type: ignore[arg-type]
            valid_from_ms=int(row["valid_from_ms"]),
            extractor_version=str(row["extractor_version"]),
            sources=tuple(
                FactSource(
                    source_event_id=str(r["source_event_id"]),
                    source_revision=int(r["source_revision"]),
                    source_trace_id=str(r["source_trace_id"] or ""),
                    author_principal=str(r["author_principal"] or ""),
                    source_channel=str(r["source_channel"] or ""),
                    source_chat_id=str(r["source_chat_id"] or ""),
                    occurred_ms=None if r["occurred_ms"] is None else int(r["occurred_ms"]),
                )
                for r in source_rows
            ),
            allowed_principals=allowed,
            audience=audience,
            audience_snapshot_id=(
                None if row["audience_snapshot_id"] is None else str(row["audience_snapshot_id"])
            ),
            valid_until_ms=None if row["valid_until_ms"] is None else int(row["valid_until_ms"]),
            superseded_by=None if row["superseded_by"] is None else str(row["superseded_by"]),
            revoked_at_ms=None if row["revoked_at_ms"] is None else int(row["revoked_at_ms"]),
            created_ms=int(row["created_ms"]),
            updated_ms=int(row["updated_ms"]),
        )

    def list_facts(
        self,
        *,
        workspace_id: str | None = None,
        chat_scope_key: str | None = None,
        include_inactive: bool = True,
        limit: int | None = None,
    ) -> list[SharedFact]:
        clauses: list[str] = []
        params: list[Any] = []
        if workspace_id is not None:
            clauses.append("workspace_id = ?")
            params.append(str(workspace_id))
        if chat_scope_key is not None:
            clauses.append("chat_scope_key = ?")
            params.append(str(chat_scope_key))
        if not include_inactive:
            clauses.append("assertion_status IN ('assertion', 'confirmed')")
            clauses.append("revoked_at_ms IS NULL")
        where = "" if not clauses else " WHERE " + " AND ".join(clauses)
        sql = f"SELECT fact_id FROM memory2_facts{where} ORDER BY created_ms, fact_id"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, tuple(params)).fetchall()
        facts: list[SharedFact] = []
        for row in rows:
            fact = self.get_fact(str(row["fact_id"]))
            if fact is not None:
                facts.append(fact)
        return facts

    def set_fact_status(
        self,
        fact_id: str,
        *,
        status: str,
        now_ms: int,
        superseded_by: str | None = None,
    ) -> bool:
        if status not in ASSERTION_STATUSES:
            raise ValueError(f"unknown assertion status: {status}")
        with self._lock:
            cursor = self._conn.execute(
                """
                UPDATE memory2_facts
                   SET assertion_status = ?,
                       superseded_by = COALESCE(?, superseded_by),
                       revoked_at_ms = CASE WHEN ? = 'revoked' THEN ? ELSE revoked_at_ms END,
                       updated_ms = ?
                 WHERE fact_id = ?
                """,
                (
                    status,
                    superseded_by,
                    status,
                    int(now_ms),
                    int(now_ms),
                    str(fact_id),
                ),
            )
            self._conn.commit()
            changed = cursor.rowcount > 0
        if changed:
            self.bump_acl_epoch()
        return changed

    def redact_fact(self, fact_id: str, *, now_ms: int) -> bool:
        """Drop content and audience of a fact, keeping the tombstone and its sources."""
        with self._lock:
            cursor = self._conn.execute(
                """
                UPDATE memory2_facts
                   SET assertion_status = 'revoked', revoked_at_ms = ?, updated_ms = ?
                 WHERE fact_id = ?
                """,
                (int(now_ms), int(now_ms), str(fact_id)),
            )
            if cursor.rowcount == 0:
                self._conn.commit()
                return False
            self._conn.execute(
                "DELETE FROM memory2_fact_principals WHERE fact_id = ?", (str(fact_id),)
            )
            self._conn.execute(
                """
                UPDATE memory2_nodes
                   SET content = '', content_norm = '', updated_at = ?
                 WHERE id = ?
                """,
                (datetime.now(UTC).isoformat(), str(fact_id)),
            )
            self._conn.execute(
                "DELETE FROM memory2_nodes_fts WHERE entry_id = ?", (str(fact_id),)
            )
            # Unlike soft_delete, redaction must drop the vector as well: an embedding is
            # a full copy of the text and would keep deleted content recoverable.
            self._conn.execute(
                "DELETE FROM memory2_embeddings WHERE entry_id = ?", (str(fact_id),)
            )
            self._conn.commit()
        self.bump_acl_epoch()
        return True

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
        contact_id: str | None = None,
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
                    self._upsert_embedding(
                        merged.id, merged.workspace_id, embedding_model, embedding
                    )
                self._conn.commit()
                return merged, False

            entry_id = entry.id or str(uuid.uuid4())
            created_at = entry.created_at or now_iso
            updated_at = entry.updated_at or now_iso
            self._conn.execute(
                """
                INSERT INTO memory2_nodes (
                    id, workspace_id, scope_type, scope_key,
                    channel, chat_id, sender_id, contact_id,
                    sector, kind, content, content_norm, content_hash,
                    salience, confidence,
                    source, source_message_id, source_role, language, meta_json,
                    created_at, updated_at, last_accessed_at, valid_from, valid_to, is_deleted
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry_id,
                    entry.workspace_id,
                    entry.scope_type,
                    entry.scope_key,
                    entry.channel,
                    entry.chat_id,
                    entry.sender_id,
                    contact_id,
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
        acl: "FactAclPredicate | None" = None,
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
        if acl is not None:
            where.append(acl.sql)
            params.extend(acl.params)
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
        acl: "FactAclPredicate | None" = None,
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
        if acl is not None:
            where.append(acl.sql)
            params.extend(acl.params)
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

    def list_recent(
        self,
        *,
        workspace_id: str,
        scope_keys: list[str],
        sectors: set[MemorySector] | None = None,
        kinds: set[str] | None = None,
        content_prefix: str | None = None,
        limit: int = 12,
    ) -> list[MemoryHit]:
        """Return recent active memories for already-known scopes without lexical search."""
        if not scope_keys:
            return []
        scope_placeholders = ",".join(["?"] * len(scope_keys))
        where = [
            "workspace_id = ?",
            "is_deleted = 0",
            f"scope_key IN ({scope_placeholders})",
        ]
        params: list[object] = [workspace_id, *scope_keys]
        if sectors:
            sector_values = sorted(sectors)
            sector_placeholders = ",".join(["?"] * len(sector_values))
            where.append(f"sector IN ({sector_placeholders})")
            params.extend(sector_values)
        if kinds:
            kind_values = sorted(kinds)
            kind_placeholders = ",".join(["?"] * len(kind_values))
            where.append(f"kind IN ({kind_placeholders})")
            params.extend(kind_values)
        if content_prefix:
            where.append("content LIKE ?")
            params.append(f"{content_prefix}%")
        sql = (
            "SELECT * "
            "FROM memory2_nodes "
            f"WHERE {' AND '.join(where)} "
            "ORDER BY updated_at DESC "
            "LIMIT ?"
        )

        with self._lock:
            rows = self._conn.execute(sql, (*params, max(1, int(limit)))).fetchall()
            hits = [MemoryHit(entry=self._row_to_entry(row)) for row in rows]
            if hits:
                now_iso = datetime.now(UTC).isoformat()
                placeholders = ",".join(["?"] * len(hits))
                self._conn.execute(
                    f"UPDATE memory2_nodes SET last_accessed_at = ? WHERE id IN ({placeholders})",
                    (now_iso, *(hit.entry.id for hit in hits)),
                )
                self._conn.commit()
            return hits

    def soft_delete(self, ids: list[str]) -> int:
        """Mark entries as deleted. Returns count of rows affected."""
        if not ids:
            return 0
        now_iso = datetime.now(UTC).isoformat()
        placeholders = ",".join(["?"] * len(ids))
        with self._lock:
            cursor = self._conn.execute(
                f"UPDATE memory2_nodes SET is_deleted = 1, updated_at = ?"
                f" WHERE id IN ({placeholders}) AND is_deleted = 0",
                (now_iso, *ids),
            )
            self._conn.commit()
            return cursor.rowcount

    def get_node(self, entry_id: str, *, workspace_id: str) -> MemoryEntry | None:
        """Return one active entry by ID."""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT *
                FROM memory2_nodes
                WHERE id = ? AND workspace_id = ? AND is_deleted = 0
                LIMIT 1
                """,
                (entry_id, workspace_id),
            ).fetchone()
        return self._row_to_entry(row) if row is not None else None

    def update_node_meta(
        self,
        entry_id: str,
        *,
        workspace_id: str,
        meta_json: str,
    ) -> MemoryEntry | None:
        """Update metadata for one active entry and return the updated row."""
        now_iso = datetime.now(UTC).isoformat()
        with self._lock:
            cursor = self._conn.execute(
                """
                UPDATE memory2_nodes
                SET meta_json = ?, updated_at = ?
                WHERE id = ? AND workspace_id = ? AND is_deleted = 0
                """,
                (meta_json, now_iso, entry_id, workspace_id),
            )
            if cursor.rowcount <= 0:
                self._conn.commit()
                return None
            row = self._conn.execute(
                "SELECT * FROM memory2_nodes WHERE id = ? AND workspace_id = ? LIMIT 1",
                (entry_id, workspace_id),
            ).fetchone()
            self._conn.commit()
        return self._row_to_entry(row) if row is not None else None

    def list_nodes_for_disclosure_backfill(
        self,
        *,
        workspace_id: str | None,
        only_missing: bool = True,
        limit: int | None = None,
    ) -> list[MemoryEntry]:
        """Return active entries eligible for disclosure metadata backfill."""
        where = ["is_deleted = 0"]
        params: list[object] = []
        if workspace_id is not None:
            where.append("workspace_id = ?")
            params.append(workspace_id)
        if only_missing:
            where.append(
                "("
                "meta_json IS NULL OR meta_json = '{}' "
                "OR (meta_json NOT LIKE '%\"sensitivity\"%' "
                "AND meta_json NOT LIKE '%\"topics\"%' "
                "AND meta_json NOT LIKE '%\"disclosure_mode\"%')"
                ")"
            )
        sql = (
            "SELECT * FROM memory2_nodes "
            f"WHERE {' AND '.join(where)} "
            "ORDER BY updated_at ASC, id ASC"
        )
        if limit is not None:
            sql += " LIMIT ?"
            params.append(max(1, int(limit)))
        with self._lock:
            rows = self._conn.execute(sql, tuple(params)).fetchall()
        return [self._row_to_entry(row) for row in rows]

    def distinct_scope_keys(self, workspace_id: str) -> list[str]:
        """Return all distinct scope_keys for a workspace (active entries only)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT scope_key FROM memory2_nodes"
                " WHERE workspace_id = ? AND is_deleted = 0",
                (workspace_id,),
            ).fetchall()
        return [str(row["scope_key"]) for row in rows]

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

    def link_nodes_to_contact(self, sender_id: str, contact_id: str) -> int:
        """Link existing memory nodes to a contact by sender_id."""
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE memory2_nodes SET contact_id = ?"
                " WHERE sender_id = ? AND (contact_id IS NULL OR contact_id = '')",
                (contact_id, sender_id),
            )
            self._conn.commit()
            return cursor.rowcount

    def append_idea_backlog_item(
        self,
        *,
        stage: str,
        title: str,
        source: str,
    ) -> int:
        now_iso = datetime.now(UTC).isoformat()
        promoted_at = now_iso if stage == "backlog" else None
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO idea_backlog_items (
                    stage, status, title, details, priority, tags, source, created_at, updated_at, promoted_at
                ) VALUES (?, 'open', ?, '', NULL, '', ?, ?, ?, ?)
                """,
                (stage, title, source, now_iso, now_iso, promoted_at),
            )
            self._conn.commit()
            return int(cur.lastrowid)


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
