"""Offline inventory and explicit build of the person-knowledge store (phase P4.1).

This module is deliberately boring and offline:

* it never imports ``typer``, never calls a provider or the network and never touches
  :mod:`yeoman_gateway.app.bootstrap`;
* legacy snapshots are opened through a ``mode=ro`` SQLite URI only (see
  :func:`yeoman_gateway.knowledge._store.open_readonly`) and are never written to, not
  even by a journal or WAL rollback;
* every SQL *value* is a bound parameter.  SQL identifiers come from this module's
  allow-list or from ``PRAGMA table_info`` and are quoted, never interpolated from
  data;
* diagnostics are redacted: table names, counts, source ids and reasons only, never
  row content.

The legacy world is the previous on-disk layout: a contacts database and a memory
database, each holding its own subset of the tables the target schema also uses.  A
source object that is neither a known legacy table nor an ignorable one makes the
source unsupported, and ``migrate_sources`` then refuses to publish anything.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import uuid
from collections import Counter
from dataclasses import dataclass, fields, is_dataclass
from pathlib import Path
from typing import Any, Final

from yeoman_gateway.knowledge._store import (
    SCHEMA_VERSION,
    TOOL_VERSION,
    KnowledgeStore,
    open_readonly,
)
from yeoman_gateway.knowledge.models import ValidationError, normalize_identifier_value

__all__ = [
    "LEGACY_FTS_TABLE",
    "KNOWN_LEGACY_TABLES",
    "MANIFEST_VERSION",
    "MigrationInventory",
    "MigrationReport",
    "MigrationSourceError",
    "SourceInventory",
    "UnsupportedSchema",
    "VerificationReport",
    "inspect_sources",
    "migrate_sources",
    "verify_target",
]

MANIFEST_VERSION: Final[int] = 1

#: The virtual FTS table, rebuilt from the retained node rows instead of copied.
LEGACY_FTS_TABLE: Final[str] = "memory2_nodes_fts"

#: Every legacy table the migration copies or rebuilds.  Anything else in a source is
#: either explicitly ignorable or unsupported.
KNOWN_LEGACY_TABLES: Final[tuple[str, ...]] = (
    "contacts",
    "contact_identifiers",
    "contact_aliases",
    "contact_fields",
    "memory2_nodes",
    "memory2_facts",
    "memory2_fact_sources",
    "memory2_fact_principals",
    "memory2_fact_jobs",
    "memory2_embeddings",
    "memory2_meta",
    "idea_backlog_items",
    LEGACY_FTS_TABLE,
)

#: Copy order: parents before children, so a foreign key never blocks a valid row.
_TABLE_ORDER: Final[tuple[str, ...]] = (
    "contacts",
    "contact_identifiers",
    "contact_aliases",
    "contact_fields",
    "memory2_nodes",
    "memory2_facts",
    "memory2_fact_sources",
    "memory2_fact_principals",
    "memory2_fact_jobs",
    "memory2_embeddings",
    "memory2_meta",
    "idea_backlog_items",
)

#: Which legacy file owns a table.  Only matters when both sources carry the same
#: table; the owner wins and the other copy is reported as superseded.
_TABLE_OWNER: Final[dict[str, str]] = {
    "contacts": "contacts",
    "contact_identifiers": "contacts",
    "contact_aliases": "contacts",
    "contact_fields": "contacts",
    "memory2_nodes": "memory",
    "memory2_facts": "memory",
    "memory2_fact_sources": "memory",
    "memory2_fact_principals": "memory",
    "memory2_fact_jobs": "memory",
    "memory2_embeddings": "memory",
    "memory2_meta": "memory",
    "idea_backlog_items": "memory",
    LEGACY_FTS_TABLE: "memory",
}

#: Minimal columns a known table must carry to be importable without guessing.
_REQUIRED_SOURCE_COLUMNS: Final[dict[str, tuple[str, ...]]] = {
    "contacts": ("id",),
    "contact_identifiers": ("channel", "identifier", "contact_id"),
    "contact_aliases": ("contact_id", "alias", "source"),
    "contact_fields": ("contact_id", "kind", "value"),
    "memory2_nodes": ("id", "content", "is_deleted"),
    "memory2_facts": ("fact_id",),
    "memory2_fact_sources": ("fact_id", "source_event_id", "source_revision"),
    "memory2_fact_principals": ("fact_id", "principal_id", "role"),
    "memory2_fact_jobs": ("job_key",),
    "memory2_embeddings": ("entry_id",),
    "memory2_meta": ("key", "value"),
    "idea_backlog_items": ("id",),
    LEGACY_FTS_TABLE: ("entry_id", "content"),
}

#: Canonical, redacted reason for a legacy row the target schema refuses.  It is one of
#: ``yeoman_gateway.knowledge._store.QUARANTINE_REASONS``; the precise integrity failure
#: is kept in ``knowledge_quarantine.detail_json``.
QUARANTINE_SCHEMA_REASON: Final[str] = "schema-unknown"

_FTS_SHADOW_SUFFIXES: Final[tuple[str, ...]] = ("_data", "_idx", "_content", "_docsize", "_config")
_IDENTIFIER_NAME: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_FINGERPRINT_CHUNK: Final[int] = 1024 * 1024

#: Object kinds that make a source unsupported when their name is not allow-listed.
_REPORTABLE_TYPES: Final[tuple[str, ...]] = ("table", "view")


class UnsupportedSchema(Exception):
    """Raised when a source snapshot cannot be imported without guessing."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail


class MigrationSourceError(Exception):
    """An unreadable, missing or non-snapshot migration source (or target)."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail


# ── inventory contracts ──────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SourceInventory:
    """Redacted description of one legacy snapshot.  Never contains row content."""

    path: Path
    fingerprint: str
    tables: tuple[str, ...]
    row_counts: tuple[tuple[str, int], ...]
    schema_version: str
    identifier_conflicts: tuple[tuple[str, str], ...]
    #: ``(node id, content hash, is_deleted)`` per retained node row, id-ordered.
    node_versions: tuple[tuple[str, str, int], ...]
    unsupported: tuple[str, ...]

    def to_json(self) -> str:
        return json.dumps(_jsonable(self), indent=2, sort_keys=True)


@dataclass(frozen=True, slots=True)
class MigrationInventory:
    """Both sources plus human-readable, redacted inventory statements."""

    contacts: SourceInventory
    memory: SourceInventory
    statements: tuple[str, ...]

    def to_json(self) -> str:
        return json.dumps(_jsonable(self), indent=2, sort_keys=True)


@dataclass(frozen=True, slots=True)
class MigrationReport:
    """What the build copied, quarantined and wrote.  Frozen and JSON-serialisable."""

    target_path: Path
    target_fingerprint: str
    manifest_path: Path
    migration_id: str
    created_ms: int
    tables: tuple[tuple[str, int, int], ...]
    quarantined: tuple[tuple[str, str, int], ...]
    source_fingerprints: tuple[tuple[str, str], ...]
    imported_rows: int
    unaccounted_rows: int
    manifest: dict[str, Any]

    def to_json(self) -> str:
        return json.dumps(_jsonable(self.manifest), indent=2, sort_keys=True)


@dataclass(frozen=True, slots=True)
class VerificationReport:
    """Read-only re-check of a built target against its manifest."""

    target_path: Path
    manifest_path: Path
    integrity_ok: bool
    foreign_keys_ok: bool
    fingerprint_ok: bool
    counts_match: bool
    verdict: str
    mismatches: tuple[tuple[str, int, int], ...]

    def to_json(self) -> str:
        return json.dumps(_jsonable(self), indent=2, sort_keys=True)


# ── inspection ───────────────────────────────────────────────────────────────


def inspect_sources(contacts_path: Path, memory_path: Path) -> MigrationInventory:
    """Read both snapshots read-only and describe them.  Never writes anything."""
    contacts_source = _expand(contacts_path)
    memory_source = _expand(memory_path)
    _require_distinct_sources(contacts_source, memory_source)
    handles: list[_SourceHandle] = []
    try:
        handles.append(_open_source("contacts", contacts_source))
        handles.append(_open_source("memory", memory_source))
        statements = _inventory_statements(handles[0], handles[1])
        return MigrationInventory(
            contacts=handles[0].inventory,
            memory=handles[1].inventory,
            statements=statements,
        )
    finally:
        for handle in handles:
            handle.close()


@dataclass(slots=True)
class _SourceHandle:
    role: str
    path: Path
    connection: sqlite3.Connection
    inventory: SourceInventory
    counts: dict[str, int]
    #: Allow-listed objects that are not migrated: ``(table, rows, reason)``.
    ignored: tuple[tuple[str, int, str], ...]

    def close(self) -> None:
        self.connection.close()


def _open_source(role: str, path: Path) -> _SourceHandle:
    _require_snapshot_file(path)
    connection = _connect_readonly(path)
    try:
        objects = _read_objects(connection, path)
        _require_integrity(connection, path)
        tables, counts, unsupported, ignored = _survey(connection, objects)
        importable = set(tables) - set(unsupported)
        conflicts = (
            _identifier_conflicts(connection) if "contact_identifiers" in importable else ()
        )
        node_versions = _node_versions(connection) if "memory2_nodes" in importable else ()
        schema_version = _schema_version(connection) if "memory2_meta" in importable else ""
        inventory = SourceInventory(
            path=path,
            fingerprint=file_fingerprint(path),
            tables=tables,
            row_counts=tuple(sorted(counts.items())),
            schema_version=schema_version,
            identifier_conflicts=conflicts,
            node_versions=node_versions,
            unsupported=unsupported,
        )
    except BaseException:
        connection.close()
        raise
    return _SourceHandle(
        role=role,
        path=path,
        connection=connection,
        inventory=inventory,
        counts=counts,
        ignored=ignored,
    )


def _require_snapshot_file(path: Path) -> None:
    if not path.exists():
        raise MigrationSourceError("missing_source", f"snapshot does not exist: {path}")
    if not path.is_file():
        raise MigrationSourceError(
            "source_not_a_file", f"snapshot is not a regular file: {path}"
        )
    if path.stat().st_size == 0:
        raise MigrationSourceError(
            "not_a_database", f"snapshot is empty and cannot be a SQLite database: {path}"
        )
    for suffix, reason in (("-journal", "hot_journal"), ("-wal", "hot_wal")):
        sidecar = path.with_name(path.name + suffix)
        try:
            size = sidecar.stat().st_size if sidecar.exists() else 0
        except OSError:  # pragma: no cover - defensive
            size = 0
        if size > 0:
            raise MigrationSourceError(
                reason,
                f"{path.name} has a non-empty {suffix} sidecar;"
                " a journal/WAL recovery would be required",
            )


def _connect_readonly(path: Path) -> sqlite3.Connection:
    try:
        return open_readonly(path)
    except sqlite3.Error as exc:
        raise MigrationSourceError(
            "unreadable_source", f"cannot open snapshot read-only: {path}"
        ) from exc


def _read_objects(connection: sqlite3.Connection, path: Path) -> tuple[tuple[str, str], ...]:
    placeholders = ", ".join("?" for _ in _REPORTABLE_TYPES)
    try:
        rows = connection.execute(
            "SELECT name, type FROM sqlite_master"
            f" WHERE type IN ({placeholders}) ORDER BY name",
            _REPORTABLE_TYPES,
        ).fetchall()
    except sqlite3.DatabaseError as exc:
        raise MigrationSourceError(
            "not_a_database", f"not a readable SQLite database: {path}"
        ) from exc
    return tuple((str(row[0]), str(row[1])) for row in rows)


def _require_integrity(connection: sqlite3.Connection, path: Path) -> None:
    try:
        row = connection.execute("PRAGMA integrity_check").fetchone()
    except sqlite3.DatabaseError as exc:
        raise MigrationSourceError(
            "integrity_check_failed", f"integrity_check could not run for {path.name}"
        ) from exc
    first = "" if row is None else str(row[0])
    if first.lower() != "ok":
        head = first.splitlines()[0][:120] if first else "no result"
        raise MigrationSourceError(
            "integrity_check_failed", f"integrity_check not ok for {path.name}: {head}"
        )


def _survey(
    connection: sqlite3.Connection,
    objects: tuple[tuple[str, str], ...],
) -> tuple[tuple[str, ...], dict[str, int], tuple[str, ...], tuple[tuple[str, int, str], ...]]:
    """Split the source schema into reportable, ignorable and unsupported objects."""
    tables: list[str] = []
    counts: dict[str, int] = {}
    unsupported: list[str] = []
    ignored: list[tuple[str, int, str]] = []
    for name, _kind in objects:
        rows = _count_rows(connection, name)
        if name.startswith("sqlite_"):
            ignored.append((name, rows, "sqlite-internal"))
            continue
        if _is_fts_shadow(name):
            ignored.append((name, rows, "fts-shadow"))
            continue
        if name.startswith("knowledge_"):
            # A source that already is a knowledge store is tolerated, not re-imported.
            ignored.append((name, rows, "knowledge-store-table"))
            continue
        tables.append(name)
        counts[name] = rows
        if name not in KNOWN_LEGACY_TABLES or _missing_required_columns(connection, name):
            # Unknown object or a known table with an unexpected shape: never guess.
            unsupported.append(name)
    return tuple(tables), counts, tuple(sorted(unsupported)), tuple(ignored)


def _is_fts_shadow(name: str) -> bool:
    if not name.startswith(LEGACY_FTS_TABLE + "_"):
        return False
    return name.endswith(_FTS_SHADOW_SUFFIXES)


def _count_rows(connection: sqlite3.Connection, name: str) -> int:
    try:
        row = connection.execute(
            f"SELECT COUNT(*) FROM {_quote_ident(name)}"
        ).fetchone()
    except sqlite3.Error:
        # A broken view or a virtual table that cannot be queried is reported as
        # unsupported elsewhere; the count itself must not break the inventory.
        return 0
    return 0 if row is None else int(row[0])


def _column_names(connection: sqlite3.Connection, name: str) -> tuple[str, ...]:
    rows = connection.execute(
        f"PRAGMA table_info({_quote_ident(name)})"
    ).fetchall()
    return tuple(str(row[1]) for row in rows)


def _missing_required_columns(connection: sqlite3.Connection, name: str) -> tuple[str, ...]:
    present = set(_column_names(connection, name))
    return tuple(item for item in _REQUIRED_SOURCE_COLUMNS.get(name, ()) if item not in present)


def _schema_version(connection: sqlite3.Connection) -> str:
    try:
        row = connection.execute(
            "SELECT value FROM memory2_meta WHERE key = ?", ("memory_schema_version",)
        ).fetchone()
    except sqlite3.DatabaseError:  # pragma: no cover - defensive
        return ""
    return "" if row is None else str(row[0])


def _node_versions(connection: sqlite3.Connection) -> tuple[tuple[str, str, int], ...]:
    present = set(_column_names(connection, "memory2_nodes"))
    if "id" not in present:
        return ()
    columns = ["id"]
    for candidate in ("content_hash", "is_deleted"):
        if candidate in present:
            columns.append(candidate)
    rows = connection.execute(
        f"SELECT {', '.join(_quote_ident(item) for item in columns)}"
        " FROM memory2_nodes ORDER BY id"
    ).fetchall()
    out: list[tuple[str, str, int]] = []
    for row in rows:
        values = dict(zip(columns, row, strict=True))
        out.append(
            (
                str(values["id"]),
                str(values.get("content_hash", "") or ""),
                int(values.get("is_deleted", 0) or 0),
            )
        )
    return tuple(out)


def _identifier_conflicts(
    connection: sqlite3.Connection,
) -> tuple[tuple[str, str], ...]:
    """``(channel, identifier)`` rows whose value points at more than one contact.

    A conflict is only possible *across* channels: the ``(channel, identifier)``
    primary key already prevents two owners inside one channel.  Nothing is merged
    here; the rows are reported so the operator can decide.
    """
    present = set(_column_names(connection, "contact_identifiers"))
    wanted = ("channel", "identifier", "contact_id", "kind")
    columns = [name for name in wanted if name in present]
    rows = connection.execute(
        f"SELECT {', '.join(_quote_ident(name) for name in columns)}"
        " FROM contact_identifiers"
    ).fetchall()
    owners: dict[str, set[str]] = {}
    members: dict[str, list[tuple[str, str]]] = {}
    for row in rows:
        values = dict(zip(columns, row, strict=True))
        channel = str(values.get("channel", ""))
        identifier = str(values.get("identifier", ""))
        contact_id = str(values.get("contact_id", ""))
        kind = str(values.get("kind", ""))
        key = _conflict_key(kind, identifier)
        if not key:
            continue
        owners.setdefault(key, set()).add(contact_id)
        members.setdefault(key, []).append((channel, identifier))
    conflicts: set[tuple[str, str]] = set()
    for key, contact_ids in owners.items():
        if len(contact_ids) > 1:
            conflicts.update(members[key])
    return tuple(sorted(conflicts))


def _conflict_key(kind: str, value: str) -> str:
    """Cross-channel comparison key: JID local parts compare with numeric ids."""
    try:
        normalized = normalize_identifier_value(kind, value)
    except ValidationError:
        normalized = value.strip()
    if kind in ("phone_jid", "lid") and "@" in normalized:
        normalized = normalized.split("@", 1)[0]
    return normalized.strip().lower()


def _inventory_statements(*handles: _SourceHandle) -> tuple[str, ...]:
    statements: list[str] = []
    for handle in handles:
        source = handle.inventory
        rows = sum(source.row_counts[index][1] for index in range(len(source.row_counts)))
        statements.append(
            f"{handle.role}: {len(source.tables)} tables, {rows} rows,"
            f" fingerprint {source.fingerprint[:16]},"
            f" schema_version {source.schema_version or 'unknown'}"
        )
        statements.append(
            f"{handle.role}: unsupported objects {len(source.unsupported)}"
        )
        if source.identifier_conflicts:
            channels = sorted({channel for channel, _ in source.identifier_conflicts})
            statements.append(
                f"{handle.role}: identifier conflicts {len(source.identifier_conflicts)}"
                f" across channels {', '.join(channels)}"
            )
    statements.append("migration is explicit: nothing is written until 'build' runs")
    return tuple(statements)


# ── migration ────────────────────────────────────────────────────────────────


def migrate_sources(
    *,
    contacts_path: Path,
    memory_path: Path,
    target: Path,
    manifest: Path,
) -> MigrationReport:
    """Build a fresh target store from both snapshots and write its manifest.

    The target is never overwritten and is only created after both sources pass the
    inventory: an unsupported source object aborts before anything is published.
    """
    contacts_source = _expand(contacts_path)
    memory_source = _expand(memory_path)
    target_path = _expand(target)
    manifest_path = _expand(manifest)
    _require_distinct_sources(contacts_source, memory_source)
    _require_target(target_path, contacts_source, memory_source, manifest_path)

    handles: list[_SourceHandle] = []
    try:
        handles.append(_open_source("contacts", contacts_source))
        handles.append(_open_source("memory", memory_source))
        unsupported = [
            (handle.role, name)
            for handle in handles
            for name in handle.inventory.unsupported
        ]
        if unsupported:
            detail = "; ".join(
                f"{role}: {name}"
                for role, name in sorted(unsupported)
            )
            raise UnsupportedSchema(
                "unsupported_source_objects",
                f"source objects outside the legacy allow-list: {detail}",
            )
        return _build(handles, target_path, manifest_path)
    finally:
        for handle in handles:
            handle.close()


def _require_target(
    target: Path, contacts: Path, memory: Path, manifest: Path
) -> None:
    if target == contacts or target == memory:
        raise MigrationSourceError(
            "target_is_source",
            f"refusing to use a source snapshot as the migration target: {target}",
        )
    if target == manifest:
        raise MigrationSourceError(
            "target_is_manifest", f"target and manifest must be different files: {target}"
        )
    if target.exists():
        raise MigrationSourceError(
            "target_exists", f"refusing to overwrite an existing target: {target}"
        )
    if manifest.exists():
        raise MigrationSourceError(
            "manifest_exists", f"refusing to overwrite an existing manifest: {manifest}"
        )


def _require_distinct_sources(contacts: Path, memory: Path) -> None:
    if contacts == memory:
        raise MigrationSourceError(
            "sources_identical",
            "contacts and memory snapshots must be two different files",
        )


def _build(
    handles: list[_SourceHandle], target_path: Path, manifest_path: Path
) -> MigrationReport:
    migration_id = uuid.uuid4().hex
    created_ms = int(time.time() * 1000)
    store = KnowledgeStore(target_path)
    published = False
    try:
        with store.transaction():
            outcomes = [
                _copy_table(store, handles, table, created_ms)
                for table in (*_TABLE_ORDER, LEGACY_FTS_TABLE)
            ]
            source_fingerprint = _combined_fingerprint(
                *(handle.inventory.fingerprint for handle in handles)
            )
            for key, value in (
                ("migration_id", migration_id),
                ("source_fingerprint", source_fingerprint),
                ("tool_version", TOOL_VERSION),
                ("migration_complete", "0"),
            ):
                store.execute(
                    "INSERT INTO knowledge_meta (key, value) VALUES (?, ?)"
                    " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )
        store.close()
        target_fingerprint = file_fingerprint(target_path)
        payload = _manifest_payload(
            handles=handles,
            outcomes=outcomes,
            target_path=target_path,
            target_fingerprint=target_fingerprint,
            migration_id=migration_id,
            created_ms=created_ms,
            source_fingerprint=source_fingerprint,
        )
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        published = True
    finally:
        if not store.closed:
            store.close()
        if not published:
            _discard(target_path)
            _discard(manifest_path)
    return _report_from_payload(
        payload,
        target_path=target_path,
        manifest_path=manifest_path,
        migration_id=migration_id,
        created_ms=created_ms,
        source_fingerprints=tuple(
            (handle.role, handle.inventory.fingerprint) for handle in handles
        ),
    )


@dataclass(frozen=True, slots=True)
class _TableOutcome:
    table: str
    source_rows: int
    imported_rows: int
    superseded_rows: int
    quarantined: tuple[tuple[str, int], ...]
    dropped_columns: tuple[str, ...]
    status: str


def _copy_table(
    store: KnowledgeStore,
    handles: list[_SourceHandle],
    table: str,
    created_ms: int,
) -> _TableOutcome:
    present = [handle for handle in handles if table in handle.inventory.tables]
    source_rows = sum(handle.counts.get(table, 0) for handle in present)
    if not present:
        return _TableOutcome(table, 0, 0, 0, (), (), "absent")
    owner_role = _TABLE_OWNER.get(table, present[0].role)
    primary = next(
        (handle for handle in present if handle.role == owner_role), present[0]
    )
    superseded = sum(
        handle.counts.get(table, 0) for handle in present if handle is not primary
    )
    if table == LEGACY_FTS_TABLE:
        return _rebuild_fts(store, primary, source_rows, superseded, created_ms)
    return _copy_rows(store, primary, table, source_rows, superseded, created_ms)


def _copy_rows(
    store: KnowledgeStore,
    source: _SourceHandle,
    table: str,
    source_rows: int,
    superseded_rows: int,
    created_ms: int,
) -> _TableOutcome:
    target_columns = _target_columns(store, table)
    target_set = {name for name, _pk in target_columns}
    cursor = source.connection.execute(
        f"SELECT * FROM {_quote_ident(table)}"
    )
    source_columns = tuple(str(item[0]) for item in (cursor.description or ()))
    columns = tuple(name for name in source_columns if name in target_set)
    dropped = tuple(name for name in source_columns if name not in target_set)
    if not columns:
        raise UnsupportedSchema(
            "table_shape_unknown",
            f"{table}: no column is shared with the target schema",
        )
    insert_sql = (
        f"INSERT INTO {_quote_ident(table)}"
        f" ({', '.join(_quote_ident(name) for name in columns)})"
        f" VALUES ({', '.join('?' for _ in columns)})"
    )
    pk_columns = tuple(name for name, pk in target_columns if pk and name in columns)
    reasons: Counter[str] = Counter()
    imported = 0
    for row in cursor:
        values = tuple(row[source_columns.index(name)] for name in columns)
        try:
            store.execute(insert_sql, values)
        except sqlite3.IntegrityError:
            # The row cannot exist in the target schema without inventing data.
            reasons[QUARANTINE_SCHEMA_REASON] += 1
            _record_quarantine(
                store,
                table=table,
                source_pk=_row_key(values, columns, pk_columns),
                reason=QUARANTINE_SCHEMA_REASON,
                created_ms=created_ms,
            )
            continue
        imported += 1
    return _TableOutcome(
        table=table,
        source_rows=source_rows,
        imported_rows=imported,
        superseded_rows=superseded_rows,
        quarantined=tuple(sorted(reasons.items())),
        dropped_columns=dropped,
        status="copied",
    )


def _rebuild_fts(
    store: KnowledgeStore,
    source: _SourceHandle,
    source_rows: int,
    superseded_rows: int,
    created_ms: int,
) -> _TableOutcome:
    """Rebuild the index from the retained node rows instead of copying index blocks."""
    store.execute(f"DELETE FROM {_quote_ident(LEGACY_FTS_TABLE)}")
    store.execute(
        f"INSERT INTO {_quote_ident(LEGACY_FTS_TABLE)} (entry_id, content)"
        " SELECT id, content FROM memory2_nodes WHERE is_deleted = 0"
    )
    imported = int(store.scalar(f"SELECT COUNT(*) FROM {_quote_ident(LEGACY_FTS_TABLE)}") or 0)
    missing = max(0, source_rows - imported)
    reasons: Counter[str] = Counter()
    if missing:
        # Index entries whose node was soft-deleted or never imported: the entry is
        # deliberately not rebuilt, and it is still accounted for.
        source_ids = {
            str(row[0]) for row in source.connection.execute(
                f"SELECT entry_id FROM {_quote_ident(LEGACY_FTS_TABLE)}"
            )
        }
        target_ids = {
            str(row[0]) for row in store.query(
                f"SELECT entry_id FROM {_quote_ident(LEGACY_FTS_TABLE)}"
            )
        }
        for entry_id in sorted(source_ids - target_ids):
            reasons[QUARANTINE_SCHEMA_REASON] += 1
            _record_quarantine(
                store,
                table=LEGACY_FTS_TABLE,
                source_pk=entry_id,
                reason=QUARANTINE_SCHEMA_REASON,
                created_ms=created_ms,
            )
        reasons[QUARANTINE_SCHEMA_REASON] = missing
    return _TableOutcome(
        table=LEGACY_FTS_TABLE,
        source_rows=source_rows,
        imported_rows=imported,
        superseded_rows=superseded_rows,
        quarantined=tuple(sorted(reasons.items())),
        dropped_columns=(),
        status="rebuilt",
    )


def _record_quarantine(
    store: KnowledgeStore,
    *,
    table: str,
    source_pk: str,
    reason: str,
    created_ms: int,
) -> None:
    store.execute(
        "INSERT OR IGNORE INTO knowledge_quarantine"
        " (quarantine_id, source_table, source_pk, reason, detail_json, created_ms)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (uuid.uuid4().hex, table, source_pk, reason, "{}", created_ms),
    )


def _row_key(
    values: tuple[Any, ...], columns: tuple[str, ...], pk_columns: tuple[str, ...]
) -> str:
    keys = pk_columns or columns[:1]
    return "|".join(str(values[columns.index(name)]) for name in keys)


def _target_columns(store: KnowledgeStore, table: str) -> tuple[tuple[str, int], ...]:
    rows = store.query(f"PRAGMA table_info({_quote_ident(table)})")
    return tuple((str(row[1]), int(row[5])) for row in rows)


def _manifest_payload(
    *,
    handles: list[_SourceHandle],
    outcomes: list[_TableOutcome],
    target_path: Path,
    target_fingerprint: str,
    migration_id: str,
    created_ms: int,
    source_fingerprint: str,
) -> dict[str, Any]:
    ignored: list[dict[str, Any]] = []
    for handle in handles:
        for name, rows, reason in handle.ignored:
            ignored.append(
                {"table": name, "role": handle.role, "source_rows": rows, "reason": reason}
            )
    for outcome in outcomes:
        if outcome.superseded_rows:
            ignored.append(
                {
                    "table": outcome.table,
                    "role": "non-owner-source",
                    "source_rows": outcome.superseded_rows,
                    "reason": "superseded-by-owning-source",
                }
            )
    dropped: list[dict[str, Any]] = []
    for outcome in outcomes:
        for column in outcome.dropped_columns:
            dropped.append(
                {
                    "table": outcome.table,
                    "column": column,
                    "reason": "column-not-in-target-schema",
                }
            )
    quarantined = [
        {"table": outcome.table, "reason": reason, "count": count}
        for outcome in outcomes
        for reason, count in outcome.quarantined
    ]
    conflicts = [
        (handle.role, name)
        for handle in handles
        for name in handle.inventory.identifier_conflicts
    ]
    return {
        "manifest_version": MANIFEST_VERSION,
        "tool_version": TOOL_VERSION,
        "target_schema_version": SCHEMA_VERSION,
        "migration_complete": False,
        "migration_id": migration_id,
        "created_ms": created_ms,
        "target_path": str(target_path),
        "target_fingerprint": target_fingerprint,
        "source_fingerprint": source_fingerprint,
        "sources": [
            {
                "role": handle.role,
                "path": str(handle.path),
                "fingerprint": handle.inventory.fingerprint,
                "schema_version": handle.inventory.schema_version,
                "tables": len(handle.inventory.tables),
                "rows": sum(handle.counts.values()),
            }
            for handle in handles
        ],
        "tables": [
            {
                "table": outcome.table,
                "source_rows": outcome.source_rows,
                "imported_rows": outcome.imported_rows,
                "superseded_rows": outcome.superseded_rows,
                "status": outcome.status,
                "dropped_columns": list(outcome.dropped_columns),
            }
            for outcome in outcomes
            if outcome.status != "absent"
        ],
        "ignored": sorted(ignored, key=lambda item: (item["table"], item["reason"])),
        "dropped_columns": sorted(
            dropped, key=lambda item: (item["table"], item["column"])
        ),
        "quarantined": sorted(
            quarantined, key=lambda item: (item["table"], item["reason"])
        ),
        "identifier_conflicts": {
            "count": len(conflicts),
            "channels": sorted({channel for _role, (channel, _value) in conflicts}),
            "tables": ["contact_identifiers"] if conflicts else [],
        },
        "unaccounted_rows": _unaccounted_rows(outcomes),
    }


def _unaccounted_rows(outcomes: list[_TableOutcome]) -> int:
    total = 0
    for outcome in outcomes:
        quarantined = sum(count for _reason, count in outcome.quarantined)
        accounted = (
            outcome.imported_rows + quarantined + outcome.superseded_rows
        )
        total += max(0, outcome.source_rows - accounted)
    return total


def _report_from_payload(
    payload: dict[str, Any],
    *,
    target_path: Path,
    manifest_path: Path,
    migration_id: str,
    created_ms: int,
    source_fingerprints: tuple[tuple[str, str], ...],
) -> MigrationReport:
    tables = tuple(
        (str(entry["table"]), int(entry["source_rows"]), int(entry["imported_rows"]))
        for entry in payload["tables"]
    )
    quarantined = tuple(
        (str(entry["table"]), str(entry["reason"]), int(entry["count"]))
        for entry in payload["quarantined"]
    )
    return MigrationReport(
        target_path=target_path,
        target_fingerprint=str(payload["target_fingerprint"]),
        manifest_path=manifest_path,
        migration_id=migration_id,
        created_ms=created_ms,
        tables=tuple(sorted(tables)),
        quarantined=quarantined,
        source_fingerprints=source_fingerprints,
        imported_rows=sum(item[2] for item in tables),
        unaccounted_rows=int(payload["unaccounted_rows"]),
        manifest=payload,
    )


# ── verification ─────────────────────────────────────────────────────────────


def verify_target(*, target: Path, manifest: Path) -> VerificationReport:
    """Re-read a built target read-only and compare it against its manifest."""
    target_path = _expand(target)
    manifest_path = _expand(manifest)
    payload = _read_manifest(manifest_path)
    if not target_path.exists():
        raise MigrationSourceError("target_missing", f"target does not exist: {target_path}")
    connection = _connect_readonly(target_path)
    try:
        integrity_ok = _integrity_ok(connection)
        foreign_keys_ok = _foreign_keys_ok(connection)
        mismatches = _count_mismatches(connection, payload, target_path)
    finally:
        connection.close()
    expected = str(payload.get("target_fingerprint") or "")
    fingerprint_ok = bool(expected) and expected == file_fingerprint(target_path)
    counts_match = not mismatches
    ok = integrity_ok and foreign_keys_ok and fingerprint_ok and counts_match
    return VerificationReport(
        target_path=target_path,
        manifest_path=manifest_path,
        integrity_ok=integrity_ok,
        foreign_keys_ok=foreign_keys_ok,
        fingerprint_ok=fingerprint_ok,
        counts_match=counts_match,
        verdict="ok" if ok else "failed",
        mismatches=mismatches,
    )


def _read_manifest(manifest_path: Path) -> dict[str, Any]:
    if not manifest_path.exists():
        raise MigrationSourceError(
            "manifest_missing", f"migration manifest does not exist: {manifest_path}"
        )
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MigrationSourceError(
            "manifest_invalid", f"migration manifest is not readable JSON: {manifest_path}"
        ) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("tables"), list):
        raise MigrationSourceError(
            "manifest_invalid", f"migration manifest has no table inventory: {manifest_path}"
        )
    return payload


def _integrity_ok(connection: sqlite3.Connection) -> bool:
    try:
        row = connection.execute("PRAGMA integrity_check").fetchone()
    except sqlite3.DatabaseError:
        return False
    return row is not None and str(row[0]).lower() == "ok"


def _foreign_keys_ok(connection: sqlite3.Connection) -> bool:
    try:
        rows = connection.execute("PRAGMA foreign_key_check").fetchall()
    except sqlite3.DatabaseError:
        return False
    return not rows


def _count_mismatches(
    connection: sqlite3.Connection, payload: dict[str, Any], path: Path
) -> tuple[tuple[str, int, int], ...]:
    try:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = ?", ("table",)
        ).fetchall()
    except sqlite3.DatabaseError as exc:  # pragma: no cover - defensive
        raise MigrationSourceError(
            "not_a_database", f"not a readable SQLite database: {path}"
        ) from exc
    present = {str(row[0]) for row in rows}
    mismatches: list[tuple[str, int, int]] = []
    for entry in payload["tables"]:
        if not isinstance(entry, dict) or "table" not in entry:
            continue
        table = str(entry["table"])
        expected = int(entry.get("imported_rows", 0))
        actual = _count_rows(connection, table) if table in present else -1
        if actual != expected:
            mismatches.append((table, expected, actual))
    return tuple(sorted(mismatches))


# ── small helpers ────────────────────────────────────────────────────────────


def _expand(path: Path | str) -> Path:
    return Path(path).expanduser()


def _quote_ident(name: str) -> str:
    """Quote an identifier that came from an allow-list or from ``PRAGMA table_info``."""
    if not _IDENTIFIER_NAME.match(name):
        raise MigrationSourceError(
            "unsupported_object_name", f"unsupported SQL object name in source: {name!r}"
        )
    return f'"{name}"'


def file_fingerprint(path: Path) -> str:
    """SHA-256 of the file bytes.  Read-only: the file is never opened for writing."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(_FINGERPRINT_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _combined_fingerprint(*fingerprints: str) -> str:
    digest = hashlib.sha256()
    for item in fingerprints:
        digest.update(item.encode("ascii"))
        digest.update(b"\x00")
    return digest.hexdigest()


def _discard(path: Path) -> None:
    """Remove a file this build created (and its sidecars).  Never a pre-existing file."""
    for candidate in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        try:
            candidate.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - defensive
            continue


def _jsonable(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: _jsonable(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set)):
        return [_jsonable(item) for item in value]
    return value
