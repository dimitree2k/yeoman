"""Private storage owner of the knowledge module.

One SQLite file, one connection, one transaction owner.  Sub-stores (contacts,
memory, statements, identity) never open their own connection and never commit on
their own: a top-level knowledge operation owns ``BEGIN``/``COMMIT``/``ROLLBACK``.

Schema version 1 keeps the legacy table names (``contacts``, ``memory2_nodes``, ...)
so that migrated data keeps its primary keys, and adds the person-knowledge tables
(``knowledge_*``).  ``knowledge_meta.migration_complete`` marks a file as a verified
knowledge store; the runtime refuses to open a file that carries legacy schemas
without that marker.
"""

from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Final

from yeoman_shared.utils.helpers import ensure_dir

from yeoman_gateway.knowledge.models import (
    ERROR_CODES,
    KnowledgeError,
    ValidationError,
)

SCHEMA_VERSION: Final[int] = 1
TOOL_VERSION: Final[str] = "knowledge/1.0.0"

#: Reasons for quarantined legacy rows.  Never contains row content.
QUARANTINE_REASONS: Final[tuple[str, ...]] = (
    "profile-without-source",
    "unknown-audience",
    "unproven-role",
    "unproven-identifier-link",
    "schema-unknown",
    "revoked-source",
)

META_KEYS: Final[dict[str, str]] = {
    "schema_version": str(SCHEMA_VERSION),
    "identity_revision": "0",
    "acl_epoch": "1",
    "migration_complete": "0",
    "migration_id": "",
    "source_fingerprint": "",
    "tool_version": TOOL_VERSION,
    "created_ms": "0",
}

_CORE_SCHEMA: tuple[str, ...] = (
    # ── people ───────────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS contacts (
        id TEXT PRIMARY KEY,
        display_name TEXT NOT NULL,
        phone_number TEXT,
        is_owner INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        revision INTEGER NOT NULL DEFAULT 1,
        status TEXT NOT NULL DEFAULT 'active',
        preferred_name TEXT,
        preferred_name_source TEXT,
        preferred_name_visibility TEXT NOT NULL DEFAULT 'public',
        preferred_name_set_ms INTEGER,
        preferred_name_set_by TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS contact_identifiers (
        channel TEXT NOT NULL,
        identifier TEXT NOT NULL,
        contact_id TEXT NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
        kind TEXT NOT NULL,
        PRIMARY KEY (channel, identifier)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_ci_contact ON contact_identifiers (contact_id)",
    """
    CREATE TABLE IF NOT EXISTS contact_aliases (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        contact_id TEXT NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
        alias TEXT NOT NULL,
        source TEXT NOT NULL,
        first_seen TEXT NOT NULL,
        last_seen TEXT NOT NULL,
        visibility TEXT NOT NULL DEFAULT 'public',
        first_seen_ms INTEGER NOT NULL DEFAULT 0,
        last_seen_ms INTEGER NOT NULL DEFAULT 0,
        UNIQUE (contact_id, alias, source)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_ca_contact ON contact_aliases (contact_id)",
    "CREATE INDEX IF NOT EXISTS idx_ca_alias ON contact_aliases (alias COLLATE NOCASE)",
    """
    CREATE TABLE IF NOT EXISTS contact_fields (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        contact_id TEXT NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
        kind TEXT NOT NULL,
        value TEXT NOT NULL,
        label TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_cf_contact ON contact_fields (contact_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_cf_dedupe"
    " ON contact_fields (contact_id, kind, value)",
    # ── knowledge identity side tables ───────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS knowledge_identifier_bindings (
        channel TEXT NOT NULL,
        kind TEXT NOT NULL,
        value TEXT NOT NULL,
        person_id TEXT NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
        status TEXT NOT NULL DEFAULT 'active'
            CHECK(status IN ('active','conflict','withheld')),
        evidence_ref TEXT NOT NULL,
        mapping_verified INTEGER NOT NULL DEFAULT 0,
        created_ms INTEGER NOT NULL,
        updated_ms INTEGER NOT NULL,
        PRIMARY KEY (channel, kind, value)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS knowledge_identifier_bindings_by_person
      ON knowledge_identifier_bindings (person_id, status)
    """,
    """
    CREATE TABLE IF NOT EXISTS knowledge_identity_redirects (
        operation_id TEXT PRIMARY KEY,
        seq INTEGER NOT NULL DEFAULT 0,
        source_id TEXT NOT NULL,
        target_id TEXT NOT NULL,
        actor_principal TEXT NOT NULL,
        authorization_ref TEXT NOT NULL,
        created_ms INTEGER NOT NULL,
        active INTEGER NOT NULL DEFAULT 1,
        undone_ms INTEGER
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS knowledge_identity_redirects_by_source
      ON knowledge_identity_redirects (source_id, active)
    """,
    """
    CREATE TABLE IF NOT EXISTS knowledge_identity_ops (
        operation_id TEXT PRIMARY KEY,
        kind TEXT NOT NULL
            CHECK(kind IN ('merge','undo_merge','preferred_name','binding','correction')),
        actor_principal TEXT NOT NULL,
        authorization_ref TEXT NOT NULL,
        payload_json TEXT NOT NULL DEFAULT '{}',
        created_ms INTEGER NOT NULL,
        undone INTEGER NOT NULL DEFAULT 0
    )
    """,
    # ── statements ───────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS knowledge_statements (
        statement_id TEXT PRIMARY KEY REFERENCES memory2_nodes(id) ON DELETE CASCADE,
        workspace_id TEXT NOT NULL,
        scope_key TEXT NOT NULL,
        author_principal TEXT NOT NULL,
        speaker_person_id TEXT,
        status TEXT NOT NULL
            CHECK(status IN ('assertion','confirmed','superseded','revoked','expired')),
        visibility_scope TEXT NOT NULL
            CHECK(visibility_scope IN ('chat_shared','principals','author_only')),
        group_rule TEXT NOT NULL
            CHECK(group_rule IN ('chat_members_at_source','explicit_principals','author_only','none')),
        source_chat_id TEXT NOT NULL DEFAULT '',
        source_channel TEXT NOT NULL DEFAULT '',
        audience_snapshot_id TEXT,
        valid_from_ms INTEGER NOT NULL,
        valid_until_ms INTEGER,
        superseded_by TEXT,
        revoked_at_ms INTEGER,
        extractor_version TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        unresolved_mentions_json TEXT NOT NULL DEFAULT '[]',
        dedupe_key TEXT NOT NULL,
        created_ms INTEGER NOT NULL,
        updated_ms INTEGER NOT NULL
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS knowledge_statements_dedupe
      ON knowledge_statements (dedupe_key)
    """,
    """
    CREATE INDEX IF NOT EXISTS knowledge_statements_by_scope
      ON knowledge_statements (workspace_id, scope_key, status)
    """,
    """
    CREATE TABLE IF NOT EXISTS knowledge_statement_people (
        statement_id TEXT NOT NULL REFERENCES knowledge_statements(statement_id)
            ON DELETE CASCADE,
        person_id TEXT NOT NULL REFERENCES contacts(id),
        role TEXT NOT NULL
            CHECK(role IN ('speaker','reported_speaker','subject','participant','mentioned')),
        evidence_source_id TEXT NOT NULL,
        evidence_revision INTEGER NOT NULL,
        attribution TEXT NOT NULL
            CHECK(attribution IN ('transport','explicit','extracted','confirmed')),
        created_ms INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(statement_id, person_id, role, evidence_source_id, evidence_revision)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS knowledge_statement_people_by_person
      ON knowledge_statement_people(person_id, role, statement_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS knowledge_statement_sources (
        statement_id TEXT NOT NULL REFERENCES knowledge_statements(statement_id)
            ON DELETE CASCADE,
        event_id TEXT NOT NULL,
        revision INTEGER NOT NULL,
        channel TEXT NOT NULL DEFAULT '',
        chat_id TEXT NOT NULL DEFAULT '',
        author_principal TEXT NOT NULL,
        occurred_at_ms INTEGER NOT NULL DEFAULT 0,
        source_audience_json TEXT,
        snapshot_id TEXT,
        status TEXT NOT NULL DEFAULT 'active'
            CHECK(status IN ('active','revoked','unknown')),
        PRIMARY KEY (statement_id, event_id, revision)
    )
    """,
    "CREATE INDEX IF NOT EXISTS knowledge_statement_sources_by_event"
    " ON knowledge_statement_sources (event_id, revision)",
    """
    CREATE TABLE IF NOT EXISTS knowledge_statement_principals (
        statement_id TEXT NOT NULL REFERENCES knowledge_statements(statement_id)
            ON DELETE CASCADE,
        principal_id TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('allowed','audience')),
        PRIMARY KEY (statement_id, principal_id, role)
    )
    """,
    "CREATE INDEX IF NOT EXISTS knowledge_statement_principals_by_principal"
    " ON knowledge_statement_principals (principal_id, role)",
    """
    CREATE TABLE IF NOT EXISTS knowledge_statement_audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        statement_id TEXT NOT NULL,
        operation TEXT NOT NULL,
        actor_principal TEXT NOT NULL DEFAULT '',
        evidence_ref TEXT NOT NULL DEFAULT '',
        reason TEXT NOT NULL DEFAULT '',
        detail_json TEXT NOT NULL DEFAULT '{}',
        created_ms INTEGER NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS knowledge_statement_audit_by_statement"
    " ON knowledge_statement_audit (statement_id, id)",
    """
    CREATE TABLE IF NOT EXISTS knowledge_jobs (
        job_id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        scope_key TEXT NOT NULL,
        kind TEXT NOT NULL,
        sources_json TEXT NOT NULL,
        extractor_version TEXT NOT NULL,
        state TEXT NOT NULL
            CHECK(state IN ('queued','running','done','skipped','cancelled','failed')),
        reason TEXT,
        attempts INTEGER NOT NULL DEFAULT 0,
        due_ms INTEGER NOT NULL,
        created_ms INTEGER NOT NULL,
        updated_ms INTEGER NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS knowledge_jobs_due ON knowledge_jobs (state, due_ms)",
    """
    CREATE TABLE IF NOT EXISTS knowledge_quarantine (
        quarantine_id TEXT PRIMARY KEY,
        source_table TEXT NOT NULL,
        source_pk TEXT NOT NULL,
        reason TEXT NOT NULL,
        detail_json TEXT NOT NULL DEFAULT '{}',
        created_ms INTEGER NOT NULL,
        UNIQUE (source_table, source_pk, reason)
    )
    """,
    "CREATE INDEX IF NOT EXISTS knowledge_quarantine_by_reason"
    " ON knowledge_quarantine (reason)",
    """
    CREATE TABLE IF NOT EXISTS knowledge_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    # ── reused semantic memory tables (one file, one owner) ──────────────────
    # These are the exact legacy definitions from the previous memory store so a
    # migrated database keeps its shape and primary keys.
    """
    CREATE TABLE IF NOT EXISTS memory2_nodes (
        id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        scope_type TEXT NOT NULL,
        scope_key TEXT NOT NULL,
        channel TEXT,
        chat_id TEXT,
        sender_id TEXT,
        contact_id TEXT,
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
    """,
    "CREATE INDEX IF NOT EXISTS idx_memory2_scope_sector_updated"
    " ON memory2_nodes (workspace_id, scope_key, sector, is_deleted, updated_at DESC)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_memory2_dedupe_active"
    " ON memory2_nodes (workspace_id, scope_key, sector, content_hash, is_deleted)",
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
    """,
    """
    CREATE TABLE IF NOT EXISTS memory2_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
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
    """,
    "CREATE INDEX IF NOT EXISTS idx_idea_backlog_stage_status"
    " ON idea_backlog_items (stage, status, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_idea_backlog_priority"
    " ON idea_backlog_items (stage, priority DESC, created_at DESC)",
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS memory2_nodes_fts
    USING fts5(entry_id UNINDEXED, content)
    """,
    # Shared-fact shell: reused unchanged so the existing read gates keep working.
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
    "CREATE INDEX IF NOT EXISTS idx_memory2_facts_scope"
    " ON memory2_facts (workspace_id, chat_scope_key, assertion_status, valid_until_ms)",
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
    "CREATE INDEX IF NOT EXISTS idx_memory2_fact_jobs_due ON memory2_fact_jobs (state, due_ms)",
)


class StorageUnavailable(KnowledgeError):
    """The knowledge database cannot serve the request."""

    def __init__(self, message: str = "knowledge storage unavailable") -> None:
        super().__init__("storage_unavailable", message)


def _now_ms() -> int:
    return int(time.time() * 1000)


class MonotonicMs:
    """Strictly increasing millisecond stamp.

    Ordering of merge redirects must be exact even when two operations happen inside
    the same millisecond, so a collision bumps the value instead of tying.
    """

    def __init__(self) -> None:
        self._last = 0

    def __call__(self) -> int:
        candidate = _now_ms()
        if candidate <= self._last:
            candidate = self._last + 1
        self._last = candidate
        return candidate


class KnowledgeStore:
    """Owns the knowledge connection, its schema and its transaction boundaries.

    All knowledge tables live in one file.  ``transaction()`` is re-entrant: a nested
    call joins the outer transaction instead of committing early, which is what makes
    "identity write + statement write" atomic.
    """

    def __init__(
        self,
        db_path: Path | str,
        *,
        create: bool = True,
        busy_timeout_ms: int = 5000,
    ) -> None:
        self.db_path = Path(db_path).expanduser()
        self._lock = threading.RLock()
        self._depth = 0
        self._failed = False
        self._closed = False
        # Test hook: raise just before a successful commit (after the body ran).
        self.fail_next_commit = False
        self._seq = 0
        self.stamp_ms = MonotonicMs()
        if self.db_path.exists() or create:
            ensure_dir(self.db_path.parent)
        self._conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            timeout=max(0.1, busy_timeout_ms / 1000.0),
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        if create:
            self._create_schema()

    # ── lifecycle ────────────────────────────────────────────────────────────

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    @property
    def connection(self) -> sqlite3.Connection:
        """The shared connection.  Private: only knowledge internals bind to it."""
        return self._conn

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            try:
                if self._depth:
                    self._conn.rollback()
                    self._depth = 0
            finally:
                self._conn.close()
                self._closed = True

    # ── schema ───────────────────────────────────────────────────────────────

    def _create_schema(self) -> None:
        with self._lock:
            try:
                for statement in _CORE_SCHEMA:
                    self._conn.execute(statement)
                for key, value in META_KEYS.items():
                    if key == "created_ms":
                        continue
                    self._conn.execute(
                        "INSERT OR IGNORE INTO knowledge_meta (key, value) VALUES (?, ?)",
                        (key, value),
                    )
                self._conn.execute(
                    "INSERT OR IGNORE INTO knowledge_meta (key, value) VALUES ('created_ms', ?)",
                    (str(_now_ms()),),
                )
            except BaseException:
                self._conn.rollback()
                raise
            self._conn.commit()

    def table_names(self) -> tuple[str, ...]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
                " AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        return tuple(str(row["name"]) for row in rows)

    def has_table(self, name: str) -> bool:
        return name in set(self.table_names())

    # ── meta ─────────────────────────────────────────────────────────────────

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM knowledge_meta WHERE key = ?", (str(key),)
            ).fetchone()
        return default if row is None else str(row["value"])

    def commit_if_idle(self) -> None:
        """Commit bookkeeping writes that happen outside a knowledge operation.

        Inside an operation the outer transaction stays in charge; there is never a
        second commit site that could split a unit of work.
        """
        with self._lock:
            if self._depth == 0 and self._conn.in_transaction:
                self._conn.commit()

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO knowledge_meta (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(key), str(value)),
            )
            self.commit_if_idle()

    def int_meta(self, key: str, default: int = 0) -> int:
        raw = self.get_meta(key)
        try:
            return int(raw) if raw is not None else default
        except ValueError:
            return default

    @property
    def schema_version(self) -> int:
        return self.int_meta("schema_version", 0)

    @property
    def identity_revision(self) -> int:
        return self.int_meta("identity_revision", 0)

    @property
    def acl_epoch(self) -> int:
        return self.int_meta("acl_epoch", 1)

    def bump_identity_revision(self) -> int:
        value = self.identity_revision + 1
        self.set_meta("identity_revision", str(value))
        return value

    def bump_acl_epoch(self) -> int:
        value = self.acl_epoch + 1
        self.set_meta("acl_epoch", str(value))
        return value

    def migration_complete(self) -> bool:
        return self.get_meta("migration_complete", "0") == "1"

    # ── transactions ─────────────────────────────────────────────────────────

    def _check_open(self) -> None:
        if self._closed:
            raise StorageUnavailable("knowledge store is closed")

    @contextmanager
    def transaction(self, *, write: bool = True) -> Iterator[sqlite3.Connection]:
        """Join or open the one transaction.  The outermost call commits."""
        with self._lock:
            self._check_open()
            outermost = self._depth == 0
            if outermost:
                self._failed = False
            self._depth += 1
            try:
                yield self._conn
            except BaseException:
                self._failed = True
                if outermost:
                    self._conn.rollback()
                raise
            finally:
                self._depth -= 1
                if outermost:
                    failed = self._failed
                    self._failed = False
                    if not failed:
                        if self.fail_next_commit:
                            self.fail_next_commit = False
                            self._conn.rollback()
                            raise StorageUnavailable("injected commit failure")
                        if write:
                            self._conn.commit()

    @contextmanager
    def savepoint(self) -> Iterator[sqlite3.Connection]:
        """A nested rollback point inside the current transaction.

        Used for optimistic writes (a losing stub creation rolls back only itself), so
        the enclosing operation keeps its atomicity.
        """
        with self._lock:
            self._check_open()
            outermost = self._depth == 0
            self._depth += 1
            name = f"sp_{uuid.uuid4().hex[:12]}"
            self._conn.execute(f"SAVEPOINT {name}")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute(f"ROLLBACK TO {name}")
                self._conn.execute(f"RELEASE {name}")
                raise
            else:
                self._conn.execute(f"RELEASE {name}")
            finally:
                self._depth -= 1
                if outermost:
                    # An optimistic write outside an operation still has to persist.
                    self._conn.commit()

    # ── small helpers used by the private sub-stores ─────────────────────────

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())

    def query_one(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def scalar(self, sql: str, params: tuple[Any, ...] = ()) -> Any:
        row = self.query_one(sql, params)
        return None if row is None else row[0]

    def new_id(self) -> str:
        return str(uuid.uuid4())

    def next_seq(self) -> int:
        """Monotonic operation counter for order-sensitive bookkeeping."""
        with self._lock:
            self._seq += 1
            return self._seq

    def now_ms(self) -> int:
        """Strictly increasing timestamp for order-sensitive rows."""
        return self.stamp_ms()

    def integrity_ok(self) -> bool:
        row = self.query_one("PRAGMA integrity_check")
        return bool(row) and str(row[0]).lower() == "ok"

    def foreign_keys_ok(self) -> bool:
        return not self.query("PRAGMA foreign_key_check")

    def counts(self) -> dict[str, int]:
        """Row counts of every table that carries durable knowledge state."""
        wanted = (
            "contacts",
            "contact_identifiers",
            "contact_aliases",
            "contact_fields",
            "knowledge_identifier_bindings",
            "knowledge_identity_redirects",
            "knowledge_statements",
            "knowledge_statement_people",
            "knowledge_statement_sources",
            "knowledge_statement_principals",
            "knowledge_jobs",
            "knowledge_quarantine",
            "memory2_nodes",
            "memory2_facts",
            "memory2_fact_sources",
            "memory2_fact_principals",
            "memory2_embeddings",
        )
        present = set(self.table_names())
        out: dict[str, int] = {}
        for name in wanted:
            if name in present:
                out[name] = int(self.scalar(f"SELECT COUNT(*) FROM {name}") or 0)
        return out


def open_readonly(db_path: Path | str) -> sqlite3.Connection:
    """Open a snapshot read-only for migration inspection.  Never writes."""
    path = Path(db_path).expanduser()
    uri = f"file:{path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def validate_reason(code: str) -> str:
    if code not in ERROR_CODES:
        raise ValidationError(f"unknown reason code: {code!r}")
    return code
