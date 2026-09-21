"""Offline v1 -> v2 knowledge snapshot upgrade.

This is the *only* path that turns a schema-1 knowledge store into a schema-2 store.
The normal runtime start refuses a v1 file byte-for-byte (see
:func:`yeoman_gateway.knowledge.api.open_knowledge_store`); an operator has to run this
explicitly, twice, and compare the digests before anything goes live.

Design rules encoded here:

* Both inputs (the v1 knowledge snapshot and the processing journal) are opened
  **read-only**.  Neither is ever written, and nothing is migrated in place.
* Every imported row keeps its original primary key.  Only the rows that the v2 contract
  requires to be re-keyed (temporal identifier bindings) get a new key, and that key is a
  deterministic UUIDv5 over the complete binding key - never a random id, so two upgrades
  of the same input produce identical rows and identical digests.
* A person role or an identifier binding that has no proven mapping evidence is recorded,
  counted and **withheld**.  It is never silently confirmed and never silently dropped.
* Nothing is published until ``PRAGMA integrity_check``, ``PRAGMA foreign_key_check``, the
  source-reference check and the balance equation all pass; only then is
  ``migration_complete`` set to ``1`` inside the same transaction.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from yeoman_gateway.knowledge._store import SCHEMA_VERSION, TOOL_VERSION, KnowledgeStore

__all__ = [
    "UPGRADE_MANIFEST_VERSION",
    "UpgradeError",
    "UpgradeInventory",
    "UpgradeReport",
    "UpgradeVerification",
    "inspect_v1",
    "upgrade_v1",
    "upgrade_semantic_digest",
    "verify_upgrade",
]

UPGRADE_MANIFEST_VERSION: Final[int] = 1

#: Namespace of the deterministic binding ids.  Fixed forever: changing it would change
#: every derived id of an already published upgrade.
_BINDING_NAMESPACE: Final[uuid.UUID] = uuid.UUID("6f2b1d0a-6f6d-5e2a-9d3f-1a2b3c4d5e6f")

#: Evidence references that prove nothing durable about a principal -> person mapping.
_UNPROVEN_EVIDENCE: Final[frozenset[str]] = frozenset({"", "legacy-import", "legacy_import"})

#: Supersession reasons that a v1 statement audit row can establish.
_AUDIT_REASON_MAP: Final[tuple[tuple[str, str], ...]] = (
    ("correction", "correction"),
    ("correct", "correction"),
    ("state_change", "state_change"),
    ("state-change", "state_change"),
    ("move", "state_change"),
    ("quality_rejected", "quality_rejected"),
    ("quality-rejected", "quality_rejected"),
    ("rescreen", "quality_rejected"),
    ("re-screen", "quality_rejected"),
)

#: Tables whose rows are copied verbatim.  The order is not cosmetic: a statement row
#: references its ``memory2_nodes`` shell, so the text store has to exist first.
_COPY_ORDER: Final[tuple[str, ...]] = (
    "contacts",
    "memory2_nodes",
    "memory2_meta",
    "memory2_embeddings",
    "memory2_facts",
    "memory2_fact_sources",
    "memory2_fact_principals",
    "memory2_fact_jobs",
    "idea_backlog_items",
    "contact_identifiers",
    "contact_aliases",
    "contact_fields",
    "knowledge_identity_redirects",
    "knowledge_identity_ops",
    "knowledge_jobs",
    "knowledge_quarantine",
    "knowledge_statements",
    "knowledge_statement_sources",
    "knowledge_statement_principals",
    "knowledge_statement_audit",
    "conversations",
    "conversation_memberships",
    "conversation_relations",
    "knowledge_episodes",
    "knowledge_episode_sources",
)

#: Rebuilt from ``memory2_nodes`` instead of copied.
_FTS_TABLE: Final[str] = "memory2_nodes_fts"

#: Tables the upgrade owns itself and therefore never blind-copies.
_TRANSFORMED_TABLES: Final[frozenset[str]] = frozenset(
    {"knowledge_identifier_bindings", "knowledge_statement_people", "knowledge_meta"}
)

#: Required of a v1 knowledge snapshot before anything is read from it.
_REQUIRED_V1_TABLES: Final[frozenset[str]] = frozenset(
    {
        "contacts",
        "knowledge_meta",
        "knowledge_statements",
        "knowledge_statement_people",
        "knowledge_statement_sources",
    }
)


class UpgradeError(Exception):
    """A refused offline upgrade.  Carries a stable reason code, never row content."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code


# ── read-only handles ────────────────────────────────────────────────────────


@dataclass
class _ReadHandle:
    role: str
    path: Path
    connection: sqlite3.Connection
    tables: tuple[str, ...]
    counts: dict[str, int]

    @property
    def schema_version(self) -> str:
        return _schema_version(self.connection)

    def close(self) -> None:
        self.connection.close()


def _open_readonly(role: str, path: Path) -> _ReadHandle:
    path = Path(path).expanduser()
    if not path.exists():
        raise UpgradeError(f"{role}_missing", f"{role} snapshot does not exist: {path}")
    if not path.is_file():
        raise UpgradeError(f"{role}_not_a_file", f"{role} snapshot is not a file: {path}")
    uri = f"file:{path}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, check_same_thread=False)
    except sqlite3.Error as exc:
        raise UpgradeError(f"{role}_unreadable", f"{role} snapshot is not readable") from exc
    connection.row_factory = sqlite3.Row
    try:
        tables = tuple(
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
                " AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        )
        counts = {
            name: int(connection.execute(f"SELECT COUNT(*) FROM {_quote(name)}").fetchone()[0])
            for name in tables
        }
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
    except sqlite3.DatabaseError as exc:
        connection.close()
        raise UpgradeError(f"{role}_not_a_database", f"{role} snapshot is not SQLite") from exc
    if integrity is None or str(integrity[0]).lower() != "ok":
        connection.close()
        raise UpgradeError(f"{role}_integrity_failed", f"{role} snapshot failed integrity_check")
    return _ReadHandle(
        role=role, path=path, connection=connection, tables=tables, counts=counts
    )


def _schema_version(connection: sqlite3.Connection) -> str:
    try:
        row = connection.execute(
            "SELECT value FROM knowledge_meta WHERE key = 'schema_version'"
        ).fetchone()
    except sqlite3.DatabaseError:
        return ""
    return "" if row is None else str(row[0])


def _quote(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _columns(connection: sqlite3.Connection, table: str) -> tuple[str, ...]:
    return tuple(
        str(row[1]) for row in connection.execute(f"PRAGMA table_info({_quote(table)})")
    )


# ── inspection ───────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class UpgradeInventory:
    """Redacted, read-only inventory of both inputs.  Counts and object names only."""

    knowledge_path: str
    processing_path: str
    knowledge_schema_version: str
    knowledge_fingerprint: str
    processing_fingerprint: str
    knowledge_tables: tuple[str, ...]
    processing_tables: tuple[str, ...]
    counts: tuple[tuple[str, int], ...]
    processing_counts: tuple[tuple[str, int], ...]
    unknown_objects: tuple[str, ...]
    identifier_conflicts: int
    orphan_sources: int
    alias_collisions: int
    missing_required_tables: tuple[str, ...]
    verdict: str
    reason: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {
            "knowledge_path": self.knowledge_path,
            "processing_path": self.processing_path,
            "knowledge_schema_version": self.knowledge_schema_version,
            "knowledge_fingerprint": self.knowledge_fingerprint,
            "processing_fingerprint": self.processing_fingerprint,
            "knowledge_tables": list(self.knowledge_tables),
            "processing_tables": list(self.processing_tables),
            "counts": dict(self.counts),
            "processing_counts": dict(self.processing_counts),
            "unknown_objects": list(self.unknown_objects),
            "identifier_conflicts": self.identifier_conflicts,
            "orphan_sources": self.orphan_sources,
            "alias_collisions": self.alias_collisions,
            "missing_required_tables": list(self.missing_required_tables),
            "verdict": self.verdict,
            "reason": self.reason,
        }


def inspect_v1(*, source: Path, processing: Path) -> UpgradeInventory:
    """Inventory a v1 knowledge snapshot and the processing journal, read-only.

    Reports counts, schema version, unknown objects, identifier conflicts, orphaned
    sources and alias collisions.  It never contains a value, a name or a message.
    """
    knowledge = _open_readonly("knowledge", Path(source))
    journal = _open_readonly("processing", Path(processing))
    try:
        missing = tuple(sorted(_REQUIRED_V1_TABLES - set(knowledge.tables)))
        unknown: list[str] = []
        for name in knowledge.tables:
            if name in _COPY_ORDER or name in _TRANSFORMED_TABLES or name == _FTS_TABLE:
                continue
            if name.startswith("memory2_nodes_fts_"):
                # SQLite-managed FTS shadow tables: never treated as legacy leftovers.
                continue
            unknown.append(name)
        version = knowledge.schema_version
        if missing:
            verdict, reason = "refused", "knowledge snapshot lacks required tables"
        elif version != "1":
            verdict, reason = "refused", f"knowledge snapshot is not schema 1 ({version or 'none'})"
        else:
            verdict, reason = "ok", ""
        return UpgradeInventory(
            knowledge_path=str(knowledge.path),
            processing_path=str(journal.path),
            knowledge_schema_version=version,
            knowledge_fingerprint=_fingerprint(knowledge.path),
            processing_fingerprint=_fingerprint(journal.path),
            knowledge_tables=knowledge.tables,
            processing_tables=journal.tables,
            counts=tuple(sorted(knowledge.counts.items())),
            processing_counts=tuple(sorted(journal.counts.items())),
            unknown_objects=tuple(sorted(unknown)),
            identifier_conflicts=_identifier_conflicts(knowledge.connection),
            orphan_sources=_orphan_sources(knowledge.connection),
            alias_collisions=_alias_collisions(knowledge.connection),
            missing_required_tables=missing,
            verdict=verdict,
            reason=reason,
        )
    finally:
        knowledge.close()
        journal.close()


def _identifier_conflicts(connection: sqlite3.Connection) -> int:
    """Identifier values that more than one contact claims in the legacy projection."""
    try:
        row = connection.execute(
            "SELECT COUNT(*) FROM ("
            " SELECT channel, identifier FROM contact_identifiers"
            " GROUP BY channel, identifier HAVING COUNT(DISTINCT contact_id) > 1)"
        ).fetchone()
    except sqlite3.DatabaseError:
        return 0
    return int(row[0]) if row is not None else 0


def _orphan_sources(connection: sqlite3.Connection) -> int:
    """Stored speaker edges whose evidence revision is not in the source table."""
    try:
        row = connection.execute(
            "SELECT COUNT(*) FROM knowledge_statement_people p"
            " WHERE NOT EXISTS (SELECT 1 FROM knowledge_statement_sources s"
            "   WHERE s.statement_id = p.statement_id"
            "     AND s.event_id = p.evidence_source_id"
            "     AND s.revision = p.evidence_revision)"
        ).fetchone()
    except sqlite3.DatabaseError:
        return 0
    return int(row[0]) if row is not None else 0


def _alias_collisions(connection: sqlite3.Connection) -> int:
    """One alias string claimed by more than one contact in the same source."""
    try:
        row = connection.execute(
            "SELECT COUNT(*) FROM ("
            " SELECT alias, source FROM contact_aliases"
            " GROUP BY alias, source HAVING COUNT(DISTINCT contact_id) > 1)"
        ).fetchone()
    except sqlite3.DatabaseError:
        return 0
    return int(row[0]) if row is not None else 0


# ── upgrade ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _Balance:
    """The cutover ledger: every relevant v1 row is explained by exactly one class."""

    bindings: dict[str, int] = field(default_factory=dict)
    person_roles: dict[str, int] = field(default_factory=dict)
    supersessions: dict[str, int] = field(default_factory=dict)

    def to_payload(self) -> dict[str, dict[str, int]]:
        return {
            "bindings": dict(sorted(self.bindings.items())),
            "person_roles": dict(sorted(self.person_roles.items())),
            "supersessions": dict(sorted(self.supersessions.items())),
        }


@dataclass(frozen=True, slots=True)
class UpgradeReport:
    target_path: Path
    manifest_path: Path
    migration_id: str
    semantic_digest: str
    balance: _Balance
    counts: tuple[tuple[str, int], ...]
    created_ms: int

    @property
    def binding_balance(self) -> dict[str, int]:
        return dict(self.balance.bindings)


def upgrade_v1(
    *,
    source: Path,
    processing: Path,
    target: Path,
    manifest: Path,
    fail_before_publish: bool = False,
) -> UpgradeReport:
    """Build a v2 target from a v1 knowledge snapshot and its processing journal.

    Never overwrites an existing target or manifest, never writes to either input, and
    never marks the target complete before every check has passed.
    """
    source_path = Path(source).expanduser()
    processing_path = Path(processing).expanduser()
    target_path = Path(target).expanduser()
    manifest_path = Path(manifest).expanduser()

    if target_path in (source_path, processing_path):
        raise UpgradeError("target_is_source", "the target must not be an input snapshot")
    if target_path == manifest_path:
        raise UpgradeError("target_is_manifest", "target and manifest must differ")
    if target_path.exists():
        raise UpgradeError("target_exists", f"refusing to overwrite: {target_path}")
    if manifest_path.exists():
        raise UpgradeError("manifest_exists", f"refusing to overwrite: {manifest_path}")

    knowledge = _open_readonly("knowledge", source_path)
    journal = _open_readonly("processing", processing_path)
    try:
        missing = _REQUIRED_V1_TABLES - set(knowledge.tables)
        if missing:
            raise UpgradeError(
                "unsupported_source_schema",
                "knowledge snapshot lacks required tables: " + ", ".join(sorted(missing)),
            )
        if knowledge.schema_version != "1":
            raise UpgradeError(
                "unsupported_source_schema",
                f"knowledge snapshot is not schema 1: {knowledge.schema_version or 'none'}",
            )
        return _build(
            knowledge=knowledge,
            journal=journal,
            target_path=target_path,
            manifest_path=manifest_path,
            fail_before_publish=fail_before_publish,
        )
    finally:
        knowledge.close()
        journal.close()


def _build(
    *,
    knowledge: _ReadHandle,
    journal: _ReadHandle,
    target_path: Path,
    manifest_path: Path,
    fail_before_publish: bool,
) -> UpgradeReport:
    created_ms = int(time.time() * 1000)
    source_fingerprint = _combined(
        _fingerprint(knowledge.path), _fingerprint(journal.path)
    )
    # Deterministic and reproducible: the id is derived from the input fingerprints, so
    # running the upgrade twice on the same snapshots yields the same migration identity.
    migration_id = hashlib.sha256(
        f"v1v2\x1f{source_fingerprint}\x1f{TOOL_VERSION}".encode("utf-8")
    ).hexdigest()[:32]
    staging = target_path.with_name(f".{target_path.name}.upgrade-{migration_id}")

    store = KnowledgeStore(staging)
    payload: dict[str, Any] | None = None
    published = False
    try:
        with store.transaction():
            for table in _COPY_ORDER:
                _copy_table(store, knowledge, table, created_ms)
            _rebuild_fts(store, knowledge)
            binding_ledger = _transform_bindings(store, knowledge, created_ms)
            role_ledger = _transform_roles(store, knowledge, created_ms)
            reason_ledger = _transform_supersessions(store, knowledge, created_ms)
            balance = _Balance(
                bindings=binding_ledger,
                person_roles=role_ledger,
                supersessions=reason_ledger,
            )
            _write_meta(
                store,
                migration_id=migration_id,
                source_fingerprint=source_fingerprint,
                created_ms=created_ms,
                balance=balance,
            )
            _verify_staged(store, knowledge, balance)
            transformed = _transformed_outcomes(store, knowledge)
            digest = _semantic_digest(store)
            store.execute(
                "INSERT INTO knowledge_meta (key, value) VALUES ('semantic_digest', ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (digest,),
            )
            # Taken last, so the manifest counts describe the published file exactly.
            inventory = _target_inventory(store)
            if fail_before_publish:
                # Crash injection: the staged file must stay incomplete and unpublished.
                raise UpgradeError("injected_failure", "injected failure before publish")
            store.execute(
                "INSERT INTO knowledge_meta (key, value) VALUES ('migration_complete', '1')"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value"
            )
        store.close()

        target_fingerprint = _fingerprint(staging)
        payload = _manifest_payload(
            knowledge=knowledge,
            journal=journal,
            outcomes=inventory,
            transformed=transformed,
            balance=balance,
            target_path=target_path,
            target_fingerprint=target_fingerprint,
            migration_id=migration_id,
            created_ms=created_ms,
            source_fingerprint=source_fingerprint,
            semantic_digest=digest,
        )
        staging.replace(target_path)
        published = True
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    finally:
        if not store.closed:
            store.close()
        _discard(staging)
        if not published:
            _discard(target_path)
            _discard(manifest_path)
    assert payload is not None
    return UpgradeReport(
        target_path=target_path,
        manifest_path=manifest_path,
        migration_id=migration_id,
        semantic_digest=str(payload["semantic_digest"]),
        balance=_balance_from_payload(payload),
        counts=tuple(sorted((row["table"], int(row["imported_rows"])) for row in payload["tables"])),
        created_ms=created_ms,
    )


def _write_meta(
    store: KnowledgeStore,
    *,
    migration_id: str,
    source_fingerprint: str,
    created_ms: int,
    balance: _Balance,
) -> None:
    for key, value in (
        ("migration_id", migration_id),
        ("source_fingerprint", source_fingerprint),
        ("tool_version", TOOL_VERSION),
        ("created_ms", str(created_ms)),
        ("cutover_balance", json.dumps(balance.to_payload(), sort_keys=True)),
        # Explicitly not complete yet: the marker is the last write of a verified build.
        ("migration_complete", "0"),
    ):
        store.execute(
            "INSERT INTO knowledge_meta (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


# ── table copy ───────────────────────────────────────────────────────────────


def _copy_table(
    store: KnowledgeStore, knowledge: _ReadHandle, table: str, created_ms: int
) -> dict[str, Any]:
    if table not in knowledge.tables:
        return {"table": table, "source_rows": 0, "imported_rows": 0, "status": "absent"}
    target_columns = _columns(store.connection, table)
    target_set = set(target_columns)
    cursor = knowledge.connection.execute(f"SELECT * FROM {_quote(table)}")
    source_columns = tuple(str(item[0]) for item in (cursor.description or ()))
    columns = tuple(name for name in source_columns if name in target_set)
    dropped = tuple(name for name in source_columns if name not in target_set)
    if not columns:
        raise UpgradeError("table_shape_unknown", f"{table}: no shared column with the target")
    insert_sql = (
        f"INSERT INTO {_quote(table)} ({', '.join(_quote(name) for name in columns)})"
        f" VALUES ({', '.join('?' for _ in columns)})"
    )
    imported = 0
    refused = 0
    for row in cursor:
        values = tuple(row[source_columns.index(name)] for name in columns)
        try:
            store.execute(insert_sql, values)
        except sqlite3.IntegrityError:
            # A row that cannot exist in v2 without inventing data.  It is counted and
            # recorded as a schema rejection; it never disappears silently.
            refused += 1
            _record_quarantine(
                store,
                table=table,
                source_pk=_row_key(values, columns),
                reason="schema-unknown",
                created_ms=created_ms,
            )
            continue
        imported += 1
    return {
        "table": table,
        "source_rows": int(knowledge.counts.get(table, 0)),
        "imported_rows": imported,
        "refused_rows": refused,
        "dropped_columns": list(dropped),
        "status": "copied",
    }


def _rebuild_fts(store: KnowledgeStore, knowledge: _ReadHandle) -> None:
    """Rebuild the lexical index without carrying a locked statement forward.

    The v1 index was built before the lifecycle columns existed, so it can hold entries
    for statements that are now superseded or revoked.  Carrying them over would hand an
    index-based reader a statement it may not see - and a later re-derivation would trust
    that index.  Deleted nodes are excluded for the same reason.
    """
    store.execute(f"DELETE FROM {_quote(_FTS_TABLE)}")
    store.execute(
        f"INSERT INTO {_quote(_FTS_TABLE)} (entry_id, content)"
        " SELECT n.id, n.content FROM memory2_nodes n"
        " WHERE n.is_deleted = 0"
        "   AND NOT EXISTS ("
        "       SELECT 1 FROM knowledge_statements s"
        "       WHERE s.statement_id = n.id"
        "         AND (s.status IN ('superseded','revoked')"
        "              OR s.revoked_at_ms IS NOT NULL"
        "              OR s.superseded_by IS NOT NULL))"
    )


def _row_key(values: tuple[Any, ...], columns: tuple[str, ...]) -> str:
    head = columns[:1]
    return "|".join(str(values[columns.index(name)]) for name in head)


def _record_quarantine(
    store: KnowledgeStore, *, table: str, source_pk: str, reason: str, created_ms: int
) -> None:
    quarantine_id = hashlib.sha256(
        f"{table}\x1f{source_pk}\x1f{reason}".encode("utf-8")
    ).hexdigest()[:32]
    store.execute(
        "INSERT OR IGNORE INTO knowledge_quarantine (quarantine_id, source_table,"
        " source_pk, reason, detail_json, created_ms) VALUES (?, ?, ?, ?, '{}', ?)",
        (quarantine_id, table, source_pk, reason, created_ms),
    )


# ── identifier bindings: the cutover ─────────────────────────────────────────


def _durable_binding_ops(knowledge: _ReadHandle) -> set[str]:
    """Binding operations that really exist, so an evidence ref can be checked."""
    if "knowledge_identity_ops" not in knowledge.tables:
        return set()
    try:
        rows = knowledge.connection.execute(
            "SELECT operation_id FROM knowledge_identity_ops WHERE kind = 'binding'"
        ).fetchall()
    except sqlite3.DatabaseError:
        return set()
    return {str(row[0]) for row in rows}


def _binding_evidence_ok(
    *, evidence_ref: str, mapping_verified: bool, durable_ops: set[str]
) -> tuple[bool, str]:
    """Decide whether a v1 binding row may become an *active* v2 binding.

    ``contact_identifiers`` is never proof on its own, and a legacy import marker is not
    proof either.  An unproven row is kept as history and counted, not promoted.
    """
    reference = str(evidence_ref or "").strip()
    if reference in _UNPROVEN_EVIDENCE or reference.lower() in _UNPROVEN_EVIDENCE:
        return False, "evidence_is_a_legacy_import_marker"
    if not bool(mapping_verified):
        return False, "mapping_not_verified"
    if reference.startswith("binding-op:"):
        operation_id = reference.split(":", 1)[1]
        if operation_id not in durable_ops:
            return False, "binding_operation_is_missing"
        return True, "durable_binding_operation"
    # Any other reference must at least name something durable in this snapshot.
    if reference not in durable_ops:
        return False, "evidence_reference_has_no_durable_record"
    return True, "durable_evidence_reference"


def _transform_bindings(
    store: KnowledgeStore, knowledge: _ReadHandle, created_ms: int
) -> dict[str, int]:
    """Rewrite the v1 identifier bindings into temporal v2 bindings."""
    ledger: Counter[str] = Counter()
    if "knowledge_identifier_bindings" not in knowledge.tables:
        return dict(ledger)
    durable_ops = _durable_binding_ops(knowledge)
    people = _known_people(knowledge)
    rows = knowledge.connection.execute(
        "SELECT channel, kind, value, person_id, status, evidence_ref, mapping_verified,"
        " created_ms, updated_ms FROM knowledge_identifier_bindings"
        " ORDER BY channel, kind, value"
    ).fetchall()
    for row in rows:
        channel = str(row["channel"] or "").strip().lower()
        kind = str(row["kind"] or "").strip().lower()
        value = str(row["value"] or "").strip()
        person_id = str(row["person_id"] or "").strip()
        created = int(row["created_ms"] or created_ms)
        updated = int(row["updated_ms"] or created)
        binding_id = str(
            uuid.uuid5(_BINDING_NAMESPACE, "\x1f".join((channel, kind, "default", value)))
        )
        if not channel or not kind or not value:
            ledger["quarantined"] += 1
            _record_quarantine(
                store,
                table="knowledge_identifier_bindings",
                source_pk=f"{channel}:{kind}:{value}",
                reason="schema-unknown",
                created_ms=created_ms,
            )
            continue
        if person_id not in people:
            # The authoritative principal is the person row itself; without it there is
            # nothing to attach the identifier to and no person is invented.
            ledger["quarantined"] += 1
            _record_quarantine(
                store,
                table="knowledge_identifier_bindings",
                source_pk=binding_id,
                reason="unproven-identifier-link",
                created_ms=created_ms,
            )
            continue
        proven, _reason = _binding_evidence_ok(
            evidence_ref=str(row["evidence_ref"] or ""),
            mapping_verified=bool(row["mapping_verified"]),
            durable_ops=durable_ops,
        )
        if not proven:
            ledger["withheld"] += 1
            status = "withheld"
        else:
            ledger["active"] += 1
            status = "active"
        store.execute(
            "INSERT INTO knowledge_identifier_bindings (binding_id, channel, kind, namespace,"
            " value, person_id, status, valid_from_ms, valid_until_ms, observed_at_ms,"
            " evidence_ref, mapping_verified, revision, created_ms, updated_ms)"
            " VALUES (?, ?, ?, 'default', ?, ?, ?, 0, 0, ?, ?, ?, 1, ?, ?)",
            (
                binding_id,
                channel,
                kind,
                value,
                person_id,
                status,
                updated if proven else 0,
                str(row["evidence_ref"] or ""),
                int(1 if proven and bool(row["mapping_verified"]) else 0),
                created,
                updated,
            ),
        )
    # Only one active binding per identifier is allowed; a duplicate would be a v1
    # inconsistency that has to be refused rather than silently resolved.
    duplicates = store.query(
        "SELECT channel, kind, namespace, value, COUNT(*) AS n"
        " FROM knowledge_identifier_bindings WHERE status = 'active'"
        " GROUP BY channel, kind, namespace, value HAVING n > 1"
    )
    if duplicates:
        raise UpgradeError(
            "binding_overlap",
            f"{len(duplicates)} identifier(s) would carry more than one active binding",
        )
    return dict(ledger)


def _known_people(knowledge: _ReadHandle) -> set[str]:
    if "contacts" not in knowledge.tables:
        return set()
    return {
        str(row[0])
        for row in knowledge.connection.execute("SELECT id FROM contacts")
    }


# ── person roles ─────────────────────────────────────────────────────────────


def _transform_roles(
    store: KnowledgeStore, knowledge: _ReadHandle, created_ms: int
) -> dict[str, int]:
    """Carry stored speaker edges over, with an explicit proven/withheld verdict."""
    ledger: Counter[str] = Counter()
    if "knowledge_statement_people" not in knowledge.tables:
        return dict(ledger)
    active_index = {
        (str(row["channel"]), str(row["value"])): (str(row["binding_id"]), str(row["person_id"]))
        for row in store.query(
            "SELECT binding_id, channel, value, person_id FROM knowledge_identifier_bindings"
            " WHERE status = 'active'"
        )
    }
    author_by_statement = _statement_authors(knowledge)
    rows = knowledge.connection.execute(
        "SELECT statement_id, person_id, role, evidence_source_id, evidence_revision,"
        " attribution, created_ms FROM knowledge_statement_people"
        " ORDER BY statement_id, person_id, role, evidence_source_id, evidence_revision"
    ).fetchall()
    for row in rows:
        statement_id = str(row["statement_id"])
        person_id = str(row["person_id"])
        role = str(row["role"])
        event_id = str(row["evidence_source_id"])
        revision = int(row["evidence_revision"])
        author = author_by_statement.get((statement_id, event_id, revision), "")
        if not author:
            # No authoritative principal: the case is quarantined, never resolved from a
            # contact row, and the original ids stay in the ledger.
            ledger["quarantined"] += 1
            status, binding_id, reason = (
                "withheld",
                None,
                "no_authoritative_principal",
            )
            _record_quarantine(
                store,
                table="knowledge_statement_people",
                source_pk=f"{statement_id}\x1f{person_id}\x1f{role}",
                reason="unproven-role",
                created_ms=created_ms,
            )
        else:
            linked = _person_for_author(author, active_index)
            if linked is None:
                ledger["withheld"] += 1
                status, binding_id, reason = "withheld", None, "no_proven_mapping_for_principal"
            elif linked[1] == person_id or _canonical(knowledge, linked[1]) == person_id:
                ledger["active"] += 1
                status, binding_id, reason = "active", linked[0], "proven_active_binding"
            else:
                # The stored role and the proven mapping disagree.  The stored claim is
                # not confirmed and the proven mapping is not silently substituted.
                ledger["withheld"] += 1
                status, binding_id, reason = (
                    "withheld",
                    None,
                    "stored_role_disagrees_with_proven_mapping",
                )
        store.execute(
            "INSERT INTO knowledge_statement_people (statement_id, person_id, role,"
            " evidence_source_id, evidence_revision, attribution, created_ms, status,"
            " binding_id, resolution_reason)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                statement_id,
                person_id,
                role,
                event_id,
                revision,
                str(row["attribution"]),
                int(row["created_ms"] or created_ms),
                status,
                binding_id,
                reason,
            ),
        )
    return dict(ledger)


def _statement_authors(
    knowledge: _ReadHandle,
) -> dict[tuple[str, str, int], str]:
    if "knowledge_statement_sources" not in knowledge.tables:
        return {}
    rows = knowledge.connection.execute(
        "SELECT statement_id, event_id, revision, author_principal"
        " FROM knowledge_statement_sources"
    ).fetchall()
    return {
        (str(row["statement_id"]), str(row["event_id"]), int(row["revision"])): str(
            row["author_principal"] or ""
        ).strip()
        for row in rows
    }


def _person_for_author(
    author: str, active_index: dict[tuple[str, str], tuple[str, str]]
) -> tuple[str, str] | None:
    """Resolve a transport principal through *active proven bindings only*."""
    token = str(author or "").strip()
    if not token:
        return None
    channel, _, value = token.partition(":")
    candidates: list[tuple[str, str]] = []
    if value:
        candidates.append((channel, value))
        if channel == "whatsapp":
            candidates.append(("whatsapp", f"{value}@s.whatsapp.net"))
            candidates.append(("whatsapp", f"{value}@lid"))
    else:
        candidates.append(("whatsapp", token))
        candidates.append(("whatsapp", f"{token}@s.whatsapp.net"))
    for key in candidates:
        if key in active_index:
            return active_index[key]
    return None


def _canonical(knowledge: _ReadHandle, person_id: str) -> str:
    """Follow v1 merge redirects so a redirect does not read as a disagreement."""
    if "knowledge_identity_redirects" not in knowledge.tables:
        return person_id
    current = person_id
    seen: set[str] = set()
    while current not in seen:
        seen.add(current)
        row = knowledge.connection.execute(
            "SELECT target_id FROM knowledge_identity_redirects"
            " WHERE source_id = ? AND active = 1 ORDER BY seq ASC, operation_id ASC LIMIT 1",
            (current,),
        ).fetchone()
        if row is None:
            return current
        current = str(row[0])
    return current


# ── supersession reasons ─────────────────────────────────────────────────────


def _transform_supersessions(
    store: KnowledgeStore, knowledge: _ReadHandle, created_ms: int
) -> dict[str, int]:
    """Derive ``supersession_reason`` from a concrete audit row, never from the status."""
    ledger: Counter[str] = Counter()
    statements = knowledge.connection.execute(
        "SELECT statement_id, status FROM knowledge_statements"
    ).fetchall()
    audits = _statement_audits(knowledge)
    for row in statements:
        statement_id = str(row["statement_id"])
        status = str(row["status"])
        reason = "unknown"
        if status == "superseded":
            reason = _audit_reason(audits.get(statement_id, ()))
            ledger[reason] += 1
        store.execute(
            "UPDATE knowledge_statements SET supersession_reason = ? WHERE statement_id = ?",
            (reason, statement_id),
        )
    # Non-superseded rows keep the column default, which is not part of a decision.
    return dict(ledger)


def _statement_audits(knowledge: _ReadHandle) -> dict[str, tuple[str, ...]]:
    if "knowledge_statement_audit" not in knowledge.tables:
        return {}
    rows = knowledge.connection.execute(
        "SELECT statement_id, operation, reason FROM knowledge_statement_audit"
        " ORDER BY statement_id, id"
    ).fetchall()
    out: dict[str, list[str]] = {}
    for row in rows:
        bucket = out.setdefault(str(row["statement_id"]), [])
        for value in (row["operation"], row["reason"]):
            text = str(value or "").strip().lower()
            if text:
                bucket.append(text)
    return {key: tuple(value) for key, value in out.items()}


def _audit_reason(tokens: tuple[str, ...]) -> str:
    """Map concrete audit evidence to a reason; anything ambiguous stays ``unknown``."""
    found: set[str] = set()
    for token in tokens:
        for needle, reason in _AUDIT_REASON_MAP:
            if needle in token:
                found.add(reason)
    if len(found) == 1:
        return found.pop()
    # No evidence, or contradictory evidence: never guess ``state_change``.
    return "unknown"


# ── verification ─────────────────────────────────────────────────────────────


def _verify_staged(
    store: KnowledgeStore, knowledge: _ReadHandle, balance: _Balance
) -> None:
    if not store.integrity_ok():
        raise UpgradeError("staged_integrity_failed", "integrity_check failed on the target")
    if not store.foreign_keys_ok():
        raise UpgradeError("staged_foreign_key_failed", "foreign_key_check failed on the target")
    if store.scalar("SELECT COUNT(*) FROM knowledge_statements") != knowledge.counts.get(
        "knowledge_statements", 0
    ):
        raise UpgradeError("statement_count_mismatch", "statement rows were lost")
    if store.scalar("SELECT COUNT(*) FROM knowledge_statement_people") != knowledge.counts.get(
        "knowledge_statement_people", 0
    ):
        raise UpgradeError("role_count_mismatch", "stored person roles were lost")
    unaccounted = sum(
        1
        for row in store.query(
            "SELECT status FROM knowledge_identifier_bindings"
            " WHERE status NOT IN ('active','withheld','conflict','ended')"
        )
    )
    if unaccounted:
        raise UpgradeError("binding_balance_broken", "a binding has no cutover class")
    _require_source_references(store, knowledge)


def _require_source_references(store: KnowledgeStore, knowledge: _ReadHandle) -> None:
    """Every imported source revision must still exist, and cite a known statement."""
    orphans = store.query(
        "SELECT s.statement_id FROM knowledge_statement_sources s"
        " LEFT JOIN knowledge_statements k ON k.statement_id = s.statement_id"
        " WHERE k.statement_id IS NULL"
    )
    if orphans:
        raise UpgradeError(
            "orphan_source_rows", f"{len(orphans)} source row(s) lost their statement"
        )
    missing = int(store.scalar("SELECT COUNT(*) FROM knowledge_statement_sources") or 0)
    if missing != knowledge.counts.get("knowledge_statement_sources", 0):
        raise UpgradeError("source_count_mismatch", "stored source references were lost")


def verify_upgrade(*, target: Path, manifest: Path) -> "UpgradeVerification":
    """Re-read a published v2 target read-only and compare it against its manifest."""
    target_path = Path(target).expanduser()
    manifest_path = Path(manifest).expanduser()
    payload = _read_manifest(manifest_path)
    if not target_path.exists():
        raise UpgradeError("target_missing", f"target does not exist: {target_path}")
    connection = sqlite3.connect(f"file:{target_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        integrity_ok = integrity is not None and str(integrity[0]).lower() == "ok"
        foreign_keys_ok = not connection.execute("PRAGMA foreign_key_check").fetchall()
        meta = {
            str(row["key"]): str(row["value"])
            for row in connection.execute(
                "SELECT key, value FROM knowledge_meta WHERE key IN"
                " ('migration_complete','migration_id','semantic_digest','schema_version')"
            )
        }
        mismatches = _count_mismatches(connection, payload)
        digest = _semantic_digest_connection(connection)
    except sqlite3.DatabaseError as exc:
        raise UpgradeError("target_not_a_database", "target is not a readable database") from exc
    finally:
        connection.close()
    fingerprint_ok = str(payload.get("target_fingerprint") or "") == _fingerprint(target_path)
    complete = (
        payload.get("migration_complete") is True
        and meta.get("migration_complete") == "1"
        and meta.get("migration_id") == str(payload.get("migration_id") or "")
        and meta.get("semantic_digest") == str(payload.get("semantic_digest") or "")
    )
    digest_ok = digest == str(payload.get("semantic_digest") or "")
    balance = _balance_from_payload(payload)
    # The balance equation: every stored person role is either active, withheld or
    # quarantined, and no class may carry a negative count.
    role_classes = ("active", "withheld", "quarantined")
    balance_ok = all(balance.person_roles.get(key, 0) >= 0 for key in role_classes) and all(
        value >= 0 for value in balance.bindings.values()
    )
    balance_ok = balance_ok and sum(balance.person_roles.values()) == sum(
        balance.person_roles.get(key, 0) for key in role_classes
    )
    counts_match = not mismatches
    ok = (
        integrity_ok
        and foreign_keys_ok
        and fingerprint_ok
        and counts_match
        and complete
        and digest_ok
        and balance_ok
    )
    return UpgradeVerification(
        target_path=target_path,
        manifest_path=manifest_path,
        integrity_ok=integrity_ok,
        foreign_keys_ok=foreign_keys_ok,
        fingerprint_ok=fingerprint_ok,
        counts_match=counts_match,
        complete=complete,
        digest_ok=digest_ok,
        balance_ok=balance_ok,
        verdict="ok" if ok else "failed",
        mismatches=mismatches,
        balance=balance,
    )


@dataclass(frozen=True, slots=True)
class UpgradeVerification:
    target_path: Path
    manifest_path: Path
    integrity_ok: bool
    foreign_keys_ok: bool
    fingerprint_ok: bool
    counts_match: bool
    complete: bool
    digest_ok: bool
    balance_ok: bool
    verdict: str
    mismatches: tuple[tuple[str, int, int], ...] = ()
    balance: _Balance = field(default_factory=_Balance)

    @property
    def ok(self) -> bool:
        return self.verdict == "ok"


def upgrade_semantic_digest(target: Path) -> str:
    """Read-only semantic digest of a v2 target, independent of file layout."""
    path = Path(target).expanduser()
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        return _semantic_digest_connection(connection)
    finally:
        connection.close()


#: Timestamp-only columns: two upgrades of the same input must still produce one digest.
_VOLATILE: Final[frozenset[str]] = frozenset(
    {"created_ms", "updated_ms", "first_seen", "last_seen", "first_seen_ms", "last_seen_ms"}
)

#: Bookkeeping tables that describe *when* a build ran rather than what it contains.
#: They are copied and kept in the database, but they never enter the digest: a digest
#: that changed on every run could not prove reproducibility.
_VOLATILE_TABLES: Final[frozenset[str]] = frozenset({"knowledge_meta", "memory2_meta"})


def _semantic_digest(store: KnowledgeStore) -> str:
    return _semantic_digest_connection(store.connection)


def _semantic_digest_connection(connection: sqlite3.Connection) -> str:
    digest = hashlib.sha256()
    names = [
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
            " AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    for name in names:
        if name.startswith("memory2_nodes_fts") or name in _VOLATILE_TABLES:
            continue
        columns = [
            str(row[1])
            for row in connection.execute(f"PRAGMA table_info({_quote(name)})")
            if str(row[1]) not in _VOLATILE
        ]
        if not columns:
            continue
        projection = ", ".join(_quote(column) for column in columns)
        digest.update(f"\x1d{name}\x1e".encode("utf-8"))
        for row in connection.execute(
            f"SELECT {projection} FROM {_quote(name)} ORDER BY {projection}"
        ):
            digest.update(
                ("\x1f".join("" if value is None else str(value) for value in row)).encode(
                    "utf-8"
                )
            )
            digest.update(b"\x1e")
    return digest.hexdigest()


# ── manifest ─────────────────────────────────────────────────────────────────


def _manifest_payload(
    *,
    knowledge: _ReadHandle,
    journal: _ReadHandle,
    outcomes: list[dict[str, Any]],
    transformed: list[dict[str, Any]],
    balance: _Balance,
    target_path: Path,
    target_fingerprint: str,
    migration_id: str,
    created_ms: int,
    source_fingerprint: str,
    semantic_digest: str,
) -> dict[str, Any]:
    return {
        "upgrade_manifest_version": UPGRADE_MANIFEST_VERSION,
        "tool_version": TOOL_VERSION,
        "target_schema_version": SCHEMA_VERSION,
        "source_schema_version": 1,
        "migration_complete": True,
        "migration_id": migration_id,
        "semantic_digest": semantic_digest,
        "created_ms": created_ms,
        "target_path": str(target_path),
        "target_fingerprint": target_fingerprint,
        "source_fingerprint": source_fingerprint,
        "sources": [
            {
                "role": knowledge.role,
                "path": str(knowledge.path),
                "fingerprint": _fingerprint(knowledge.path),
                "schema_version": knowledge.schema_version,
                "tables": len(knowledge.tables),
                "rows": sum(knowledge.counts.values()),
            },
            {
                "role": journal.role,
                "path": str(journal.path),
                "fingerprint": _fingerprint(journal.path),
                "schema_version": "",
                "tables": len(journal.tables),
                "rows": sum(journal.counts.values()),
            },
        ],
        "tables": [row for row in outcomes if row["status"] != "absent"],
        "transformed_tables": transformed,
        "cutover": balance.to_payload(),
        "unknown_objects": sorted(
            name
            for name in knowledge.tables
            if name not in _COPY_ORDER
            and name not in _TRANSFORMED_TABLES
            and name != _FTS_TABLE
            and not name.startswith("memory2_nodes_fts_")
        ),
        "unaccounted_rows": sum(
            max(
                0,
                int(row["source_rows"])
                - int(row["imported_rows"])
                - int(row.get("refused_rows", 0)),
            )
            for row in outcomes
        ),
    }


def _target_inventory(store: KnowledgeStore) -> list[dict[str, Any]]:
    """Complete row inventory of the target, so the manifest accounts for every table.

    A table the v1 snapshot never had is imported with zero rows, not omitted: a manifest
    that only listed the source tables could not prove that a target table is empty.
    """
    out: list[dict[str, Any]] = []
    for name in store.table_names():
        if name.startswith("memory2_nodes_fts_"):
            # SQLite-managed FTS shadow tables have no independent row semantics.
            continue
        rows = int(store.scalar(f"SELECT COUNT(*) FROM {_quote(name)}") or 0)
        out.append(
            {
                "table": name,
                "source_rows": rows,
                "imported_rows": rows,
                "status": "copied" if rows else "empty",
            }
        )
    return out


def _transformed_outcomes(
    store: KnowledgeStore, knowledge: _ReadHandle
) -> list[dict[str, Any]]:
    """Row counts of the tables the upgrade rewrites rather than copies."""
    out: list[dict[str, Any]] = []
    for table in sorted(_TRANSFORMED_TABLES):
        if table == "knowledge_meta":
            continue
        if table not in knowledge.tables:
            continue
        out.append(
            {
                "table": table,
                "source_rows": int(knowledge.counts.get(table, 0)),
                "imported_rows": int(
                    store.scalar(f"SELECT COUNT(*) FROM {_quote(table)}") or 0
                ),
                "status": "transformed",
            }
        )
    return out


def _balance_from_payload(payload: dict[str, Any]) -> _Balance:
    cutover = payload.get("cutover")
    if not isinstance(cutover, dict):
        return _Balance()
    def _ints(key: str) -> dict[str, int]:
        raw = cutover.get(key)
        if not isinstance(raw, dict):
            return {}
        return {str(name): int(value) for name, value in raw.items()}
    return _Balance(
        bindings=_ints("bindings"),
        person_roles=_ints("person_roles"),
        supersessions=_ints("supersessions"),
    )


def _read_manifest(manifest_path: Path) -> dict[str, Any]:
    if not manifest_path.exists():
        raise UpgradeError("manifest_missing", f"manifest does not exist: {manifest_path}")
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise UpgradeError("manifest_invalid", "manifest is not readable JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("tables"), list):
        raise UpgradeError("manifest_invalid", "manifest carries no table inventory")
    return payload


def _count_mismatches(
    connection: sqlite3.Connection, payload: dict[str, Any]
) -> tuple[tuple[str, int, int], ...]:
    present = {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    mismatches: list[tuple[str, int, int]] = []
    for entry in payload.get("tables", []):
        if not isinstance(entry, dict) or "table" not in entry:
            continue
        table = str(entry["table"])
        expected = int(entry.get("imported_rows", 0))
        actual = (
            int(connection.execute(f"SELECT COUNT(*) FROM {_quote(table)}").fetchone()[0])
            if table in present
            else -1
        )
        if actual != expected:
            mismatches.append((table, expected, actual))
    return tuple(sorted(mismatches))


# ── small helpers ────────────────────────────────────────────────────────────


def _fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _combined(*values: str) -> str:
    digest = hashlib.sha256()
    for value in values:
        digest.update(value.encode("utf-8"))
        digest.update(b"\x1e")
    return digest.hexdigest()


def _discard(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except OSError:  # pragma: no cover - best effort cleanup
        pass
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = path.with_name(path.name + suffix)
        try:
            if sidecar.exists():
                sidecar.unlink()
        except OSError:  # pragma: no cover - best effort cleanup
            pass
