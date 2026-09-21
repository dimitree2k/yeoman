"""Synthetic legacy snapshot builder for the migration tests.

This module is a test helper, not a ``conftest``: tests import it directly.  It builds
the *previous* on-disk layout with checked, literal ``CREATE TABLE`` statements instead
of reusing :class:`yeoman_gateway.knowledge._store.KnowledgeStore`, so a passing
migration test proves the migration against the legacy shape rather than against the
target schema.

The legacy world had two SQLite files:

* a contacts database (``contacts``, ``contact_identifiers``, ``contact_aliases``,
  ``contact_fields``) whose ``contacts`` table is a narrow subset of the target table;
* a memory database (``memory2_*``, ``idea_backlog_items``, ``memory2_nodes_fts``)
  whose ``memory2_nodes`` may or may not carry the later ``contact_id`` column.

:func:`legacy_snapshot_factory` builds both, keeps them WAL-backed while it snapshots
them with the :mod:`sqlite3` backup API, and can inject the awkward shapes the
migration has to refuse or account for.  Every value is obviously synthetic.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "ACTIVE_NODE_CONTENT",
    "ACTIVE_NODE_ID",
    "CONFLICT_CONTACT_ID",
    "CONTACTS_TABLE_ROWS",
    "DELETED_NODE_ID",
    "FACT_NODE_ID",
    "LEGACY_SCHEMA_VERSION",
    "LINKED_NODE_ID",
    "MEMORY_TABLE_ROWS",
    "ORPHAN_EMBEDDING_ID",
    "OWNER_CONTACT_ID",
    "SECOND_CONTACT_ID",
    "THIRD_CONTACT_ID",
    "V1_KNOWLEDGE_SCHEMA_VERSION",
    "WORKSPACE_ID",
    "LegacySources",
    "V1KnowledgeStore",
    "legacy_snapshot_factory",
    "v1_knowledge_store_factory",
]

WORKSPACE_ID = "legacy-workspace"
LEGACY_SCHEMA_VERSION = "3"

#: The schema version a v1 knowledge store wrote into ``knowledge_meta``.
V1_KNOWLEDGE_SCHEMA_VERSION = "1"

#: Synthetic UUIDs.  Distinct per contact so primary-key preservation is checkable.
OWNER_CONTACT_ID = "11111111-1111-4111-8111-111111111111"
SECOND_CONTACT_ID = "22222222-2222-4222-8222-222222222222"
THIRD_CONTACT_ID = "33333333-3333-4333-8333-333333333333"
#: Only present when ``conflicting_identifier=True``.
CONFLICT_CONTACT_ID = "44444444-4444-4444-8444-444444444444"

ACTIVE_NODE_ID = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
DELETED_NODE_ID = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"
LINKED_NODE_ID = "cccccccc-3333-4333-8333-cccccccccccc"
FACT_NODE_ID = "dddddddd-4444-4444-8444-dddddddddddd"
#: Only present when ``broken_embedding=True``: an embedding for a node that is not there.
ORPHAN_EMBEDDING_ID = "eeeeeeee-5555-4555-8555-eeeeeeeeeeee"

ACTIVE_NODE_CONTENT = "synthetic note one"
DELETED_NODE_CONTENT = "synthetic note deleted"
LINKED_NODE_CONTENT = "synthetic note linked"
FACT_NODE_CONTENT = "synthetic shared fact"

_IDENTIFIER_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: Row counts of the base fixture, per legacy file.  Extras change these on purpose.
CONTACTS_TABLE_ROWS: dict[str, int] = {
    "contacts": 3,
    "contact_identifiers": 4,
    "contact_aliases": 3,
    "contact_fields": 2,
}
MEMORY_TABLE_ROWS: dict[str, int] = {
    "memory2_nodes": 4,
    "memory2_facts": 1,
    "memory2_fact_sources": 2,
    "memory2_fact_principals": 2,
    "memory2_fact_jobs": 1,
    "memory2_embeddings": 1,
    "memory2_meta": 2,
    "idea_backlog_items": 1,
    "memory2_nodes_fts": 3,
}


@dataclass
class LegacySources:
    """Paths of one synthetic legacy world.

    ``contacts``/``memory`` are the frozen snapshots (what the migration inspects);
    ``contacts_db``/``memory_db`` are the pre-backup originals.
    """

    contacts: Path
    memory: Path
    contacts_db: Path
    memory_db: Path


# ── literal legacy DDL ───────────────────────────────────────────────────────

_CONTACTS_DDL: tuple[str, ...] = (
    # The legacy contacts row predates preferred-name bookkeeping: a real subset of
    # the target table, so the migration must fill defaults instead of guessing.
    """
    CREATE TABLE contacts (
        id TEXT PRIMARY KEY,
        display_name TEXT NOT NULL,
        phone_number TEXT,
        is_owner INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE contact_identifiers (
        channel TEXT NOT NULL,
        identifier TEXT NOT NULL,
        contact_id TEXT NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
        kind TEXT NOT NULL,
        PRIMARY KEY (channel, identifier)
    )
    """,
    "CREATE INDEX idx_ci_contact ON contact_identifiers (contact_id)",
    """
    CREATE TABLE contact_aliases (
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
    """
    CREATE TABLE contact_fields (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        contact_id TEXT NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
        kind TEXT NOT NULL,
        value TEXT NOT NULL,
        label TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
)

_MEMORY_DDL: tuple[str, ...] = (
    """
    CREATE TABLE memory2_facts (
        fact_id TEXT PRIMARY KEY REFERENCES memory2_nodes(id) ON DELETE CASCADE,
        workspace_id TEXT NOT NULL,
        chat_scope_key TEXT NOT NULL,
        author_principal TEXT NOT NULL,
        assertion_status TEXT NOT NULL
            CHECK(assertion_status IN ('assertion','confirmed','superseded','revoked','expired')),
        visibility_scope TEXT NOT NULL
            CHECK(visibility_scope IN ('chat_shared','principals','author_only')),
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
    CREATE TABLE memory2_fact_sources (
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
    CREATE TABLE memory2_fact_principals (
        fact_id TEXT NOT NULL REFERENCES memory2_facts(fact_id) ON DELETE CASCADE,
        principal_id TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('allowed','audience')),
        PRIMARY KEY (fact_id, principal_id, role)
    )
    """,
    """
    CREATE TABLE memory2_fact_jobs (
        job_key TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        chat_scope_key TEXT NOT NULL,
        source_refs_json TEXT NOT NULL,
        extractor_version TEXT NOT NULL,
        state TEXT NOT NULL
            CHECK(state IN ('queued','running','done','skipped','cancelled','failed')),
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
    CREATE TABLE memory2_embeddings (
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
    CREATE TABLE memory2_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    # The legacy backlog table was looser than the target: no CHECK constraints, so a
    # legacy row can still violate the target schema and has to be quarantined.
    """
    CREATE TABLE idea_backlog_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        stage TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'open',
        title TEXT NOT NULL,
        details TEXT NOT NULL DEFAULT '',
        priority INTEGER,
        tags TEXT NOT NULL DEFAULT '',
        source TEXT NOT NULL DEFAULT 'manual',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        promoted_at TEXT
    )
    """,
    """
    CREATE VIRTUAL TABLE memory2_nodes_fts
    USING fts5(entry_id UNINDEXED, content)
    """,
)

#: The node table with the later ALTER TABLE column.
_NODES_DDL_WITH_CONTACT = """
    CREATE TABLE memory2_nodes (
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
        is_deleted INTEGER NOT NULL DEFAULT 0,
        contact_id TEXT
    )
"""

#: The node table before the ALTER TABLE that added ``contact_id``.
_NODES_DDL_WITHOUT_CONTACT = """
    CREATE TABLE memory2_nodes (
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

_NODE_COLUMNS: tuple[str, ...] = (
    "id",
    "workspace_id",
    "scope_type",
    "scope_key",
    "channel",
    "chat_id",
    "sender_id",
    "sector",
    "kind",
    "content",
    "content_norm",
    "content_hash",
    "salience",
    "confidence",
    "source",
    "source_message_id",
    "source_role",
    "language",
    "meta_json",
    "created_at",
    "updated_at",
    "last_accessed_at",
    "valid_from",
    "valid_to",
    "is_deleted",
    "contact_id",
)

_STAMP = "2024-01-01T00:00:00+00:00"


def _extra_table_ddl(name: str) -> str:
    if not _IDENTIFIER_NAME.match(name):
        raise ValueError(f"extra_table must be a plain SQL identifier, got {name!r}")
    return f'CREATE TABLE "{name}" (id INTEGER PRIMARY KEY, payload TEXT)'


def legacy_snapshot_factory(
    tmp_path: Path,
    *,
    extra_table: str | None = None,
    with_wal: bool = False,
    conflicting_identifier: bool = False,
    with_contact_column: bool = True,
    broken_embedding: bool = False,
    fts_entry_for_deleted_node: bool = False,
) -> LegacySources:
    """Build one synthetic legacy world and freeze it with the backup API.

    ``extra_table`` injects an unknown table into *both* legacy files.  ``with_wal``
    keeps the originals WAL-backed while the snapshots are taken, which is how the real
    offline snapshot is produced.  ``with_contact_column=False`` builds the node table
    as it looked before ``contact_id`` was added.
    """
    directory = tmp_path / "legacy"
    directory.mkdir(parents=True, exist_ok=True)
    contacts_db = directory / "contacts.db"
    memory_db = directory / "memory.db"
    contacts_snapshot = directory / "contacts-snapshot.db"
    memory_snapshot = directory / "memory-snapshot.db"

    contacts_conn = sqlite3.connect(contacts_db)
    memory_conn = sqlite3.connect(memory_db)
    try:
        if with_wal:
            contacts_conn.execute("PRAGMA journal_mode=WAL")
            memory_conn.execute("PRAGMA journal_mode=WAL")
        _create_contacts_schema(contacts_conn, extra_table=extra_table)
        _create_memory_schema(
            memory_conn,
            extra_table=extra_table,
            with_contact_column=with_contact_column,
        )
        _populate_contacts(contacts_conn, conflicting_identifier=conflicting_identifier)
        _populate_memory(
            memory_conn,
            with_contact_column=with_contact_column,
            broken_embedding=broken_embedding,
            fts_entry_for_deleted_node=fts_entry_for_deleted_node,
        )
        contacts_conn.commit()
        memory_conn.commit()
        # The snapshot is taken while the source is still WAL-backed and open.
        _snapshot(contacts_conn, contacts_snapshot)
        _snapshot(memory_conn, memory_snapshot)
    finally:
        contacts_conn.close()
        memory_conn.close()

    return LegacySources(
        contacts=contacts_snapshot,
        memory=memory_snapshot,
        contacts_db=contacts_db,
        memory_db=memory_db,
    )


def _snapshot(source: sqlite3.Connection, target_path: Path) -> None:
    destination = sqlite3.connect(target_path)
    try:
        source.backup(destination)
        destination.commit()
    finally:
        destination.close()


def _create_contacts_schema(conn: sqlite3.Connection, *, extra_table: str | None) -> None:
    for statement in _CONTACTS_DDL:
        conn.execute(statement)
    if extra_table is not None:
        conn.execute(_extra_table_ddl(extra_table))
    conn.commit()


def _create_memory_schema(
    conn: sqlite3.Connection, *, extra_table: str | None, with_contact_column: bool
) -> None:
    conn.execute(_NODES_DDL_WITH_CONTACT if with_contact_column else _NODES_DDL_WITHOUT_CONTACT)
    for statement in _MEMORY_DDL:
        conn.execute(statement)
    if extra_table is not None:
        conn.execute(_extra_table_ddl(extra_table))
    conn.commit()


def _populate_contacts(conn: sqlite3.Connection, *, conflicting_identifier: bool) -> None:
    contacts = [
        (OWNER_CONTACT_ID, "Owner Synthetic", "4910000000001", 1, _STAMP, _STAMP),
        (SECOND_CONTACT_ID, "Second Synthetic", "4910000000002", 0, _STAMP, _STAMP),
        (THIRD_CONTACT_ID, "Third Synthetic", "4910000000003", 0, _STAMP, _STAMP),
    ]
    if conflicting_identifier:
        contacts.append(
            (CONFLICT_CONTACT_ID, "Fourth Synthetic", None, 0, _STAMP, _STAMP)
        )
    conn.executemany(
        "INSERT INTO contacts (id, display_name, phone_number, is_owner, created_at,"
        " updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        contacts,
    )
    identifiers = [
        ("whatsapp", "4910000000001@s.whatsapp.net", OWNER_CONTACT_ID, "phone_jid"),
        # Same normalised value in two channels owned by one contact: not a conflict.
        ("whatsapp", "4910000000002@s.whatsapp.net", SECOND_CONTACT_ID, "phone_jid"),
        ("telegram", "4910000000002", SECOND_CONTACT_ID, "telegram_id"),
        ("telegram", "4910000000003", THIRD_CONTACT_ID, "telegram_id"),
    ]
    if conflicting_identifier:
        # Same normalised value as the owner's WhatsApp JID, different contact.
        identifiers.append(("telegram", "4910000000001", CONFLICT_CONTACT_ID, "telegram_id"))
    conn.executemany(
        "INSERT INTO contact_identifiers (channel, identifier, contact_id, kind)"
        " VALUES (?, ?, ?, ?)",
        identifiers,
    )
    conn.executemany(
        "INSERT INTO contact_aliases (contact_id, alias, source, first_seen, last_seen,"
        " visibility, first_seen_ms, last_seen_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (OWNER_CONTACT_ID, "synthetic-owner", "observed", _STAMP, _STAMP, "public", 1, 2),
            (SECOND_CONTACT_ID, "synthetic-second", "observed", _STAMP, _STAMP, "public", 3, 4),
            (THIRD_CONTACT_ID, "synthetic-third", "owner_confirmed", _STAMP, _STAMP, "private", 5, 6),
        ],
    )
    conn.executemany(
        "INSERT INTO contact_fields (contact_id, kind, value, label, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [
            (OWNER_CONTACT_ID, "note", "synthetic-note-a", None, _STAMP, _STAMP),
            (SECOND_CONTACT_ID, "birthday", "synthetic-01-02", "synthetic", _STAMP, _STAMP),
        ],
    )
    conn.commit()


def _populate_memory(
    conn: sqlite3.Connection,
    *,
    with_contact_column: bool,
    broken_embedding: bool,
    fts_entry_for_deleted_node: bool,
) -> None:
    columns = [name for name in _NODE_COLUMNS if with_contact_column or name != "contact_id"]
    placeholders = ", ".join("?" for _ in columns)
    node_sql = f"INSERT INTO memory2_nodes ({', '.join(columns)}) VALUES ({placeholders})"
    nodes = [
        _node_row(
            ACTIVE_NODE_ID,
            ACTIVE_NODE_CONTENT,
            contact_id=None,
            sector="episodic",
            with_contact_column=with_contact_column,
        ),
        _node_row(
            DELETED_NODE_ID,
            DELETED_NODE_CONTENT,
            contact_id=None,
            sector="episodic",
            is_deleted=1,
            with_contact_column=with_contact_column,
        ),
        _node_row(
            LINKED_NODE_ID,
            LINKED_NODE_CONTENT,
            contact_id=OWNER_CONTACT_ID,
            sector="profile",
            with_contact_column=with_contact_column,
        ),
        _node_row(
            FACT_NODE_ID,
            FACT_NODE_CONTENT,
            contact_id=None,
            sector="fact",
            kind="fact",
            with_contact_column=with_contact_column,
        ),
    ]
    conn.executemany(node_sql, [tuple(row[name] for name in columns) for row in nodes])

    conn.execute(
        "INSERT INTO memory2_facts (fact_id, workspace_id, chat_scope_key,"
        " author_principal, assertion_status, visibility_scope, group_rule,"
        " audience_snapshot_id, valid_from_ms, valid_until_ms, superseded_by,"
        " revoked_at_ms, extractor_version, created_ms, updated_ms)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            FACT_NODE_ID,
            WORKSPACE_ID,
            "whatsapp:group-synthetic",
            "whatsapp:4910000000001",
            "assertion",
            "chat_shared",
            "chat_members_at_source",
            "snapshot-1",
            1700000000000,
            None,
            None,
            None,
            "legacy-extractor-1",
            1700000000000,
            1700000000000,
        ),
    )
    conn.executemany(
        "INSERT INTO memory2_fact_sources (fact_id, source_event_id, source_revision,"
        " source_trace_id, author_principal, source_channel, source_chat_id, occurred_ms)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                FACT_NODE_ID,
                "event-1",
                1,
                "trace-1",
                "whatsapp:4910000000001",
                "whatsapp",
                "group-synthetic",
                1700000000000,
            ),
            (
                FACT_NODE_ID,
                "event-2",
                1,
                "trace-2",
                "whatsapp:4910000000002",
                "whatsapp",
                "group-synthetic",
                1700000001000,
            ),
        ],
    )
    conn.executemany(
        "INSERT INTO memory2_fact_principals (fact_id, principal_id, role) VALUES (?, ?, ?)",
        [
            (FACT_NODE_ID, "principal-a", "audience"),
            (FACT_NODE_ID, "principal-b", "audience"),
        ],
    )
    conn.execute(
        "INSERT INTO memory2_fact_jobs (job_key, workspace_id, chat_scope_key,"
        " source_refs_json, extractor_version, state, reason, first_activity_ms,"
        " last_activity_ms, due_ms, attempts, created_ms, updated_ms)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "job-1",
            WORKSPACE_ID,
            "whatsapp:group-synthetic",
            '["event-1"]',
            "legacy-extractor-1",
            "done",
            None,
            1700000000000,
            1700000002000,
            1700000002000,
            1,
            1700000000000,
            1700000002000,
        ),
    )
    embeddings = [
        (ACTIVE_NODE_ID, WORKSPACE_ID, "synthetic-embed-v1", 4, b"\x01\x02\x03\x04", _STAMP)
    ]
    if broken_embedding:
        # A legacy orphan: the node it points at never existed, so the target has to
        # quarantine the row instead of publishing a foreign-key violation.
        embeddings.append(
            (ORPHAN_EMBEDDING_ID, WORKSPACE_ID, "synthetic-embed-v1", 4, b"\x05\x06", _STAMP)
        )
    conn.executemany(
        "INSERT INTO memory2_embeddings (entry_id, workspace_id, model, dims, vector,"
        " created_at) VALUES (?, ?, ?, ?, ?, ?)",
        embeddings,
    )
    conn.executemany(
        "INSERT INTO memory2_meta (key, value) VALUES (?, ?)",
        [("memory_schema_version", LEGACY_SCHEMA_VERSION), ("workspace_id", WORKSPACE_ID)],
    )
    conn.execute(
        "INSERT INTO idea_backlog_items (stage, status, title, details, priority, tags,"
        " source, created_at, updated_at, promoted_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "inbox",
            "open",
            "synthetic idea",
            "synthetic details",
            3,
            "synthetic",
            "manual",
            _STAMP,
            _STAMP,
            None,
        ),
    )
    fts_rows = [
        (ACTIVE_NODE_ID, ACTIVE_NODE_CONTENT),
        (LINKED_NODE_ID, LINKED_NODE_CONTENT),
        (FACT_NODE_ID, FACT_NODE_CONTENT),
    ]
    if fts_entry_for_deleted_node:
        # Older builds soft-deleted a node without pruning the index entry.
        fts_rows.append((DELETED_NODE_ID, DELETED_NODE_CONTENT))
    conn.executemany(
        "INSERT INTO memory2_nodes_fts (entry_id, content) VALUES (?, ?)",
        fts_rows,
    )
    conn.commit()


def _node_row(
    node_id: str,
    content: str,
    *,
    contact_id: str | None,
    sector: str = "episodic",
    kind: str = "note",
    is_deleted: int = 0,
    with_contact_column: bool,
) -> dict[str, object]:
    row: dict[str, object] = {
        "id": node_id,
        "workspace_id": WORKSPACE_ID,
        "scope_type": "chat",
        "scope_key": "whatsapp:group-synthetic",
        "channel": "whatsapp",
        "chat_id": "group-synthetic",
        "sender_id": "4910000000001",
        "sector": sector,
        "kind": kind,
        "content": content,
        "content_norm": content.lower(),
        "content_hash": f"hash-{node_id[:4]}",
        "salience": 0.5,
        "confidence": 0.5,
        "source": "legacy-capture",
        "source_message_id": f"message-{node_id[:4]}",
        "source_role": "user",
        "language": "en",
        "meta_json": "{}",
        "created_at": _STAMP,
        "updated_at": _STAMP,
        "last_accessed_at": None,
        "valid_from": None,
        "valid_to": None,
        "is_deleted": is_deleted,
    }
    if with_contact_column:
        row["contact_id"] = contact_id
    return row


# ── synthetic v1 knowledge store ─────────────────────────────────────────────
#
# The frozen v1 shape.  Written with literal DDL on purpose: a v2 binary that refuses
# this file must be refused by the *probe*, and the fixture must not depend on the code
# under test to describe the file it refuses.

_V1_DDL: tuple[str, ...] = (
    """
    CREATE TABLE contacts (
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
    CREATE TABLE contact_identifiers (
        channel TEXT NOT NULL,
        identifier TEXT NOT NULL,
        contact_id TEXT NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
        kind TEXT NOT NULL,
        PRIMARY KEY (channel, identifier)
    )
    """,
    """
    CREATE TABLE contact_aliases (
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
    """
    CREATE TABLE contact_fields (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        contact_id TEXT NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
        kind TEXT NOT NULL,
        value TEXT NOT NULL,
        label TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE knowledge_identifier_bindings (
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
    CREATE TABLE knowledge_identity_redirects (
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
    CREATE TABLE knowledge_identity_ops (
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
    """
    CREATE TABLE memory2_nodes (
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
    """
    CREATE TABLE knowledge_statements (
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
            CHECK(group_rule IN ('chat_members_at_source','explicit_principals',
                                 'author_only','none')),
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
    CREATE TABLE knowledge_statement_people (
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
    CREATE TABLE knowledge_statement_sources (
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
    """
    CREATE TABLE knowledge_statement_principals (
        statement_id TEXT NOT NULL REFERENCES knowledge_statements(statement_id)
            ON DELETE CASCADE,
        principal_id TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('allowed','audience')),
        PRIMARY KEY (statement_id, principal_id, role)
    )
    """,
    """
    CREATE TABLE knowledge_statement_audit (
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
    """
    CREATE TABLE knowledge_jobs (
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
    """
    CREATE TABLE knowledge_quarantine (
        quarantine_id TEXT PRIMARY KEY,
        source_table TEXT NOT NULL,
        source_pk TEXT NOT NULL,
        reason TEXT NOT NULL,
        detail_json TEXT NOT NULL DEFAULT '{}',
        created_ms INTEGER NOT NULL,
        UNIQUE (source_table, source_pk, reason)
    )
    """,
    """
    CREATE TABLE knowledge_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
)

#: Synthetic v1 people.  Names are obviously fake and stay stable across runs.
V1_PERSON_BOUND = "a1a1a1a1-1111-4111-8111-111111111111"
V1_PERSON_LEGACY_ONLY = "b2b2b2b2-2222-4222-8222-222222222222"
V1_PERSON_NO_PRINCIPAL = "c3c3c3c3-3333-4333-8333-333333333333"
V1_STATEMENT_ONE = "d4d4d4d4-4444-4444-8444-444444444444"
V1_STATEMENT_TWO = "e5e5e5e5-5555-4555-8555-555555555555"
V1_STATEMENT_THREE = "f6f6f6f6-6666-4666-8666-666666666666"
V1_STATEMENT_SUPERSEDED = "a7a7a7a7-7777-4777-8777-777777777777"
V1_STATEMENT_REVOKED = "b8b8b8b8-8888-4888-8888-888888888888"


@dataclass
class V1KnowledgeStore:
    """Paths and the synthetic inventory of one v1 knowledge snapshot."""

    path: Path
    bound_person: str = V1_PERSON_BOUND
    legacy_only_person: str = V1_PERSON_LEGACY_ONLY
    unproven_person: str = V1_PERSON_NO_PRINCIPAL

    def sha256(self) -> str:
        import hashlib

        return hashlib.sha256(self.path.read_bytes()).hexdigest()

    def mtime_ns(self) -> int:
        return self.path.stat().st_mtime_ns

    def counts(self) -> dict[str, int]:
        conn = sqlite3.connect(self.path)
        try:
            names = [
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                    " AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
            ]
            return {
                name: int(conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0])
                for name in names
            }
        finally:
            conn.close()

    def table_names(self) -> tuple[str, ...]:
        return tuple(sorted(self.counts()))

    def meta(self) -> dict[str, str]:
        conn = sqlite3.connect(self.path)
        try:
            return {
                str(key): str(value)
                for key, value in conn.execute("SELECT key, value FROM knowledge_meta")
            }
        finally:
            conn.close()

    def dump_sidecars(self) -> dict[str, bytes]:
        """Bytes of every WAL/journal sidecar, so a probe can prove it left none."""
        return {
            suffix: path.read_bytes()
            for suffix in ("-wal", "-shm", "-journal")
            if (path := self.path.with_name(self.path.name + suffix)).exists()
        }


def v1_knowledge_store_factory(path: Path) -> V1KnowledgeStore:
    """Build a synthetic v1 knowledge store on disk.

    Three people cover the cutover classes: one with a durable, verified binding that
    cites a real binding operation, one with only a ``contact_identifiers`` row, and one
    whose stored speaker edge has no principal evidence at all.  Two statements are
    ``superseded`` without a durable audit reason and one is ``revoked``.
    """
    path = Path(path)
    conn = sqlite3.connect(path)
    try:
        for statement in _V1_DDL:
            conn.execute(statement)
        conn.executemany(
            "INSERT INTO contacts (id, display_name, phone_number, is_owner, created_at,"
            " updated_at, revision, status, preferred_name, preferred_name_source,"
            " preferred_name_visibility, preferred_name_set_ms, preferred_name_set_by)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    V1_PERSON_BOUND,
                    "Synthetic Bound",
                    "4910000000101",
                    1,
                    _STAMP,
                    _STAMP,
                    1,
                    "active",
                    None,
                    None,
                    "public",
                    None,
                    None,
                ),
                (
                    V1_PERSON_LEGACY_ONLY,
                    "Synthetic Legacy",
                    "4910000000102",
                    0,
                    _STAMP,
                    _STAMP,
                    1,
                    "active",
                    None,
                    None,
                    "public",
                    None,
                    None,
                ),
                (
                    V1_PERSON_NO_PRINCIPAL,
                    "Synthetic Orphan",
                    None,
                    0,
                    _STAMP,
                    _STAMP,
                    1,
                    "active",
                    None,
                    None,
                    "public",
                    None,
                    None,
                ),
            ],
        )
        conn.executemany(
            "INSERT INTO contact_identifiers (channel, identifier, contact_id, kind)"
            " VALUES (?, ?, ?, ?)",
            [
                ("whatsapp", "4910000000101@s.whatsapp.net", V1_PERSON_BOUND, "phone_jid"),
                (
                    "whatsapp",
                    "4910000000102@s.whatsapp.net",
                    V1_PERSON_LEGACY_ONLY,
                    "phone_jid",
                ),
                # An untyped bare number: never a phone JID and never durable evidence.
                ("whatsapp", "4910000000103", V1_PERSON_LEGACY_ONLY, "handle"),
                ("telegram", "4910000000101", V1_PERSON_BOUND, "telegram_id"),
            ],
        )
        conn.execute(
            "INSERT INTO contact_aliases (contact_id, alias, source, first_seen, last_seen,"
            " visibility, first_seen_ms, last_seen_ms)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (V1_PERSON_BOUND, "Boundy", "observed", _STAMP, _STAMP, "public", 1, 1),
        )
        conn.execute(
            "INSERT INTO contact_fields (contact_id, kind, value, created_at, updated_at)"
            " VALUES (?, 'note', 'synthetic legacy field', ?, ?)",
            (V1_PERSON_LEGACY_ONLY, _STAMP, _STAMP),
        )
        # (a) A verified binding whose durable binding op exists.  The evidence ref points
        # at that operation, so the migration has a checkable trail.
        conn.execute(
            "INSERT INTO knowledge_identifier_bindings (channel, kind, value, person_id,"
            " status, evidence_ref, mapping_verified, created_ms, updated_ms)"
            " VALUES ('whatsapp', 'phone_jid', '4910000000101@s.whatsapp.net', ?, 'active',"
            " 'binding-op:00000000-0000-4000-8000-0000000000a1', 1, 1, 1)",
            (V1_PERSON_BOUND,),
        )
        conn.execute(
            "INSERT INTO knowledge_identity_ops (operation_id, kind, actor_principal,"
            " authorization_ref, payload_json, created_ms, undone)"
            " VALUES ('00000000-0000-4000-8000-0000000000a1', 'binding',"
            " 'whatsapp:4910000000101', 'admin-ref-v1',"
            " '{\"person_id\":\"%s\",\"identifier\":[\"whatsapp\",\"phone_jid\","
            "\"4910000000101@s.whatsapp.net\"]}', 1, 0)" % V1_PERSON_BOUND,
        )
        conn.execute(
            "INSERT INTO knowledge_identity_ops (operation_id, kind, actor_principal,"
            " authorization_ref, payload_json, created_ms, undone)"
            " VALUES ('00000000-0000-4000-8000-0000000000a2', 'preferred_name',"
            " 'whatsapp:4910000000101', 'admin-ref-v1', '{}', 1, 0)",
        )
        conn.executemany(
            "INSERT INTO memory2_nodes (id, workspace_id, scope_type, scope_key, channel,"
            " chat_id, sender_id, contact_id, sector, kind, content, content_norm,"
            " content_hash, salience, confidence, source, source_message_id, source_role,"
            " language, meta_json, created_at, updated_at, last_accessed_at, valid_from,"
            " valid_to, is_deleted) VALUES (?, 'legacy-workspace', 'chat',"
            " 'whatsapp:group-synthetic', 'whatsapp', 'group-synthetic', '4910000000101',"
            " ?, 'semantic', 'fact', ?, ?, ?, 0.5, 0.5, 'legacy-capture', NULL, 'user',"
            " 'en', '{}', ?, ?, NULL, NULL, NULL, 0)",
            [
                (
                    V1_STATEMENT_ONE,
                    V1_PERSON_BOUND,
                    "synthetic statement one",
                    "synthetic statement one",
                    f"hash-{V1_STATEMENT_ONE[:4]}",
                    _STAMP,
                    _STAMP,
                ),
                (
                    V1_STATEMENT_TWO,
                    V1_PERSON_LEGACY_ONLY,
                    "synthetic statement two",
                    "synthetic statement two",
                    f"hash-{V1_STATEMENT_TWO[:4]}",
                    _STAMP,
                    _STAMP,
                ),
                (
                    V1_STATEMENT_THREE,
                    V1_PERSON_NO_PRINCIPAL,
                    "synthetic statement three",
                    "synthetic statement three",
                    f"hash-{V1_STATEMENT_THREE[:4]}",
                    _STAMP,
                    _STAMP,
                ),
                (
                    V1_STATEMENT_SUPERSEDED,
                    V1_PERSON_BOUND,
                    "synthetic statement superseded",
                    "synthetic statement superseded",
                    f"hash-{V1_STATEMENT_SUPERSEDED[:4]}",
                    _STAMP,
                    _STAMP,
                ),
                (
                    V1_STATEMENT_REVOKED,
                    V1_PERSON_BOUND,
                    "synthetic statement revoked",
                    "synthetic statement revoked",
                    f"hash-{V1_STATEMENT_REVOKED[:4]}",
                    _STAMP,
                    _STAMP,
                ),
            ],
        )
        statements = [
            (V1_STATEMENT_ONE, "assertion", V1_PERSON_BOUND, "whatsapp:4910000000101"),
            (V1_STATEMENT_TWO, "assertion", V1_PERSON_LEGACY_ONLY, "whatsapp:4910000000102"),
            # (c) A speaker edge whose authoritative principal is missing entirely.
            (V1_STATEMENT_THREE, "assertion", V1_PERSON_NO_PRINCIPAL, ""),
            (V1_STATEMENT_SUPERSEDED, "superseded", V1_PERSON_BOUND, "whatsapp:4910000000101"),
            (V1_STATEMENT_REVOKED, "revoked", V1_PERSON_BOUND, "whatsapp:4910000000101"),
        ]
        for index, (statement_id, status, person_id, author) in enumerate(statements):
            conn.execute(
                "INSERT INTO knowledge_statements (statement_id, workspace_id, scope_key,"
                " author_principal, speaker_person_id, status, visibility_scope, group_rule,"
                " source_chat_id, source_channel, audience_snapshot_id, valid_from_ms,"
                " valid_until_ms, superseded_by, revoked_at_ms, extractor_version,"
                " content_hash, unresolved_mentions_json, dedupe_key, created_ms, updated_ms)"
                " VALUES (?, 'legacy-workspace', 'whatsapp:group-synthetic', ?, ?, ?,"
                " 'chat_shared', 'chat_members_at_source', 'group-synthetic', 'whatsapp',"
                " NULL, 1, NULL, NULL, ?, 'v1-extractor', ?, '[]', ?, 1, 1)",
                (
                    statement_id,
                    author,
                    person_id if status != "superseded" else None,
                    status,
                    None if status != "revoked" else 1,
                    f"hash-{statement_id[:4]}",
                    f"dedupe-{index}",
                ),
            )
            conn.execute(
                "INSERT INTO knowledge_statement_people (statement_id, person_id, role,"
                " evidence_source_id, evidence_revision, attribution, created_ms)"
                " VALUES (?, ?, 'speaker', ?, 1, 'transport', 1)",
                (statement_id, person_id, f"event-{index}"),
            )
            conn.execute(
                "INSERT INTO knowledge_statement_sources (statement_id, event_id, revision,"
                " channel, chat_id, author_principal, occurred_at_ms, source_audience_json,"
                " snapshot_id, status) VALUES (?, ?, 1, 'whatsapp', 'group-synthetic', ?,"
                " 1, '[\"whatsapp:4910000000101\"]', NULL, 'active')",
                (statement_id, f"event-{index}", author),
            )
            conn.execute(
                "INSERT INTO knowledge_statement_principals (statement_id, principal_id, role)"
                " VALUES (?, ?, 'audience')",
                (statement_id, "whatsapp:4910000000101"),
            )
        conn.executemany(
            "INSERT INTO knowledge_meta (key, value) VALUES (?, ?)",
            [
                ("schema_version", V1_KNOWLEDGE_SCHEMA_VERSION),
                ("identity_revision", "5"),
                ("acl_epoch", "1"),
                ("migration_complete", "1"),
                ("migration_id", "legacy-knowledge-v1"),
                ("source_fingerprint", "synthetic-fingerprint"),
                ("tool_version", "knowledge/1.0.0"),
                ("created_ms", "1"),
            ],
        )
        # An unknown extra table: the inspection must account for it, never drop it.
        conn.execute("CREATE TABLE synthetic_unknown_object (id TEXT PRIMARY KEY, note TEXT)")
        conn.execute(
            "INSERT INTO synthetic_unknown_object (id, note) VALUES ('x', 'synthetic')"
        )
        conn.commit()
    finally:
        conn.close()
    return V1KnowledgeStore(path=path)
