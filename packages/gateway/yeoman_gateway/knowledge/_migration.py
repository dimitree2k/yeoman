"""Offline inventory and explicit build of the person-knowledge store (phase P4.1).

This module is deliberately boring and offline:

* it never imports ``typer``, never calls a provider or the network and never touches
  :mod:`yeoman_gateway.app.bootstrap`;
* legacy snapshots are opened through a ``mode=ro`` SQLite URI only (see
  :func:`yeoman_gateway.knowledge._store.open_readonly`) and are never written to, not
  even by a journal or WAL rollback.  A read-only open of a WAL-flagged snapshot makes
  SQLite create empty ``-wal``/``-shm`` sidecars; those are removed again on close, so
  the snapshot directory is left exactly as it was found (a pre-existing sidecar is
  never touched, and a non-empty ``-wal``/``-journal`` is refused outright);
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
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, fields, is_dataclass
from pathlib import Path
from typing import Any, Final

from yeoman_gateway.knowledge._store import (
    SCHEMA_VERSION,
    TOOL_VERSION,
    KnowledgeStore,
    open_readonly,
)
from yeoman_gateway.knowledge.models import (
    DEFAULT_NAMESPACE,
    ValidationError,
    normalize_identifier_value,
)

__all__ = [
    "LEGACY_FTS_TABLE",
    "KNOWN_LEGACY_TABLES",
    "LEGACY_NO_FACT_REASON",
    "LEGACY_PROFILE_REASON",
    "LEGACY_UNPROVEN_REASON",
    "LEGACY_NODE_SOURCE_STATUSES",
    "MANIFEST_VERSION",
    "LegacyNodeInventory",
    "LegacyNodeRecord",
    "LegacyLinkCandidate",
    "LegacyLinkManifest",
    "MigrationInventory",
    "MigrationReport",
    "MigrationSourceError",
    "SourceInventory",
    "UnsupportedSchema",
    "VerificationReport",
    "LINEAGE_DECISIONS",
    "LINEAGE_SOURCE_CLASSES",
    "LineageDecision",
    "LineageImportReport",
    "LineageInventory",
    "import_lineage",
    "inspect_legacy_nodes",
    "inspect_lineage_sources",
    "inspect_sources",
    "propose_legacy_links",
    "lineage_fingerprint",
    "semantic_digest",
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


class UnsupportedSchema(Exception):  # noqa: N818 - public name fixed by the phase contract
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
    complete: bool
    digest_ok: bool
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
    #: ``-wal``/``-shm`` sidecars that existed before this read (never removed).
    sidecars_before: frozenset[Path]

    def close(self) -> None:
        self.connection.close()
        _remove_read_sidecars(self.path, self.sidecars_before)


def _read_sidecars(path: Path) -> frozenset[Path]:
    return frozenset(
        candidate
        for candidate in (
            path.with_name(path.name + "-wal"),
            path.with_name(path.name + "-shm"),
        )
        if candidate.exists()
    )


def _remove_read_sidecars(path: Path, before: frozenset[Path]) -> None:
    """Drop the empty WAL sidecars a read-only open of a WAL snapshot creates.

    SQLite creates ``-shm``/``-wal`` next to a WAL-flagged database even for a
    read-only connection; the source file itself is never touched.  Removing only the
    files that did not exist before this read keeps the snapshot directory exactly as
    it was found, and a pre-existing sidecar is never deleted.
    """
    for suffix in ("-wal", "-shm"):
        candidate = path.with_name(path.name + suffix)
        if candidate in before or not candidate.exists():
            continue
        try:
            if candidate.stat().st_size == 0 or suffix == "-shm":
                candidate.unlink()
        except OSError:  # pragma: no cover - defensive
            continue


def _open_source(role: str, path: Path) -> _SourceHandle:
    _require_snapshot_file(path)
    sidecars_before = _read_sidecars(path)
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
        _remove_read_sidecars(path, sidecars_before)
        raise
    return _SourceHandle(
        role=role,
        path=path,
        connection=connection,
        inventory=inventory,
        counts=counts,
        ignored=ignored,
        sidecars_before=sidecars_before,
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
    """Build a staged target, validate it, publish it, then write the manifest.

    Publication order is deliberate: the database carries the authoritative
    ``migration_complete`` marker, the external manifest is regenerable.  A crash
    between the two leaves a database that is present but explicitly incomplete, which
    ``verify_target`` reports instead of silently accepting.
    """
    migration_id = uuid.uuid4().hex
    created_ms = int(time.time() * 1000)
    staging = target_path.with_name(f".{target_path.name}.staging-{migration_id}")
    source_fingerprint = _combined_fingerprint(
        *(handle.inventory.fingerprint for handle in handles)
    )
    store = KnowledgeStore(staging)
    payload: dict[str, Any] | None = None
    published = False
    try:
        with store.transaction():
            outcomes = [
                _copy_table(store, handles, table, created_ms)
                for table in (*_TABLE_ORDER, LEGACY_FTS_TABLE)
            ]
            resolution = _resolve_legacy_rows(store, created_ms=created_ms)
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
            digest = _semantic_digest(store)
            store.execute(
                "INSERT INTO knowledge_meta (key, value) VALUES ('semantic_digest', ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (digest,),
            )
            store.execute(
                "INSERT INTO knowledge_meta (key, value) VALUES ('migration_complete', '1')"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value"
            )
            _validate_staged(store)
        store.close()

        target_fingerprint = file_fingerprint(staging)
        payload = _manifest_payload(
            handles=handles,
            outcomes=outcomes,
            resolution=resolution,
            target_path=target_path,
            target_fingerprint=target_fingerprint,
            migration_id=migration_id,
            created_ms=created_ms,
            source_fingerprint=source_fingerprint,
            semantic_digest=digest,
        )
        # Publish: the staged file is complete and validated, so a rename is the only
        # step that can still fail, and it fails atomically.
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


def _validate_staged(store: KnowledgeStore) -> None:
    """Refuse to publish a target that is not internally consistent."""
    if not store.integrity_ok():
        raise MigrationSourceError(
            "staged_integrity_failed", "PRAGMA integrity_check failed on the staged target"
        )
    if not store.foreign_keys_ok():
        raise MigrationSourceError(
            "staged_foreign_key_failed", "PRAGMA foreign_key_check failed on the staged target"
        )
    expected = int(store.scalar("SELECT COUNT(*) FROM memory2_nodes WHERE is_deleted = 0") or 0)
    indexed = int(store.scalar("SELECT COUNT(*) FROM memory2_nodes_fts") or 0)
    if indexed != expected:
        raise MigrationSourceError(
            "staged_fts_mismatch",
            f"FTS holds {indexed} entries for {expected} live nodes",
        )


def semantic_digest(target: Path) -> str:
    """Public read-only semantic digest of a built target.

    Sorted by primary key and independent of file layout, page order and timestamps, so
    two builds from the same snapshots produce the same value.
    """
    store = KnowledgeStore(Path(target).expanduser(), create=False)
    try:
        return _semantic_digest(store)
    finally:
        store.close()


#: Columns excluded from the semantic digest because they only record *when* an
#: operation ran.  They stay in the rows and in the manifest, but two builds of the same
#: snapshots must not differ because a second passed between them.
_VOLATILE_DIGEST_COLUMNS: Final[frozenset[str]] = frozenset(
    {"created_ms", "updated_ms", "created_at", "updated_at", "last_accessed_at", "undone_ms"}
)


def _semantic_digest(store: KnowledgeStore) -> str:
    """Content fingerprint of every durable knowledge row, order-independent.

    Sorted by the digested columns and free of operational timestamps, so it proves
    "same inputs, same knowledge" instead of "same wall clock".
    """
    digest = hashlib.sha256()
    for table in _DIGEST_TABLES:
        if not store.has_table(table):
            continue
        columns = [
            name
            for name, _pk in _target_columns(store, table)
            if name not in _VOLATILE_DIGEST_COLUMNS
        ]
        if not columns:
            continue
        order = ", ".join(_quote_ident(name) for name in columns)
        digest.update(f"\x1e{table}\x1e".encode("utf-8"))
        for row in store.query(
            f"SELECT {order} FROM {_quote_ident(table)} ORDER BY {order}"
        ):
            digest.update(
                ("\x1f".join("" if value is None else str(value) for value in row)).encode(
                    "utf-8"
                )
            )
            digest.update(b"\x1e")
    return digest.hexdigest()


_DIGEST_TABLES: Final[tuple[str, ...]] = (
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
    "memory2_nodes_fts",
    "memory2_facts",
    "memory2_fact_sources",
    "memory2_fact_principals",
    "memory2_fact_jobs",
    "memory2_embeddings",
    "memory2_meta",
    "idea_backlog_items",
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
    # Deterministic id: two builds from the same snapshots must produce identical rows,
    # otherwise the semantic digest cannot prove reproducibility.
    quarantine_id = hashlib.sha256(
        f"{table}\x1f{source_pk}\x1f{reason}".encode("utf-8")
    ).hexdigest()[:32]
    store.execute(
        "INSERT OR IGNORE INTO knowledge_quarantine"
        " (quarantine_id, source_table, source_pk, reason, detail_json, created_ms)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (quarantine_id, table, source_pk, reason, "{}", created_ms),
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
    resolution: _LegacyResolution,
    target_path: Path,
    target_fingerprint: str,
    migration_id: str,
    created_ms: int,
    source_fingerprint: str,
    semantic_digest: str,
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
    ] + [
        {"table": table, "reason": reason, "count": count}
        for table, reason, count in resolution.quarantined
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
        "migration_complete": True,
        "semantic_digest": semantic_digest,
        "legacy_resolution": resolution.to_payload(),
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
    sidecars_before = _read_sidecars(target_path)
    connection = _connect_readonly(target_path)
    try:
        integrity_ok = _integrity_ok(connection)
        foreign_keys_ok = _foreign_keys_ok(connection)
        mismatches = _count_mismatches(connection, payload, target_path)
        complete = _marker_is_complete(connection, payload)
    finally:
        connection.close()
        _remove_read_sidecars(target_path, sidecars_before)
    expected = str(payload.get("target_fingerprint") or "")
    fingerprint_ok = bool(expected) and expected == file_fingerprint(target_path)
    counts_match = not mismatches
    # The digest is content-based, so it survives a staging rename while still proving
    # that the published rows are the rows this manifest describes.
    digest_ok = semantic_digest(target_path) == str(payload.get("semantic_digest") or "")
    ok = (
        integrity_ok
        and foreign_keys_ok
        and fingerprint_ok
        and counts_match
        and complete
        and digest_ok
    )
    return VerificationReport(
        target_path=target_path,
        manifest_path=manifest_path,
        integrity_ok=integrity_ok,
        foreign_keys_ok=foreign_keys_ok,
        fingerprint_ok=fingerprint_ok,
        counts_match=counts_match,
        complete=complete,
        digest_ok=digest_ok,
        verdict="ok" if ok else "failed",
        mismatches=mismatches,
    )


def _marker_is_complete(
    connection: sqlite3.Connection, payload: dict[str, Any]
) -> bool:
    """The database - not the manifest - is the authority on completeness.

    A database without the marker was interrupted between the row copy and the final
    publish decision, or is a legacy file that must never be presented as migrated.
    """
    if payload.get("migration_complete") is not True:
        return False
    try:
        rows = connection.execute(
            "SELECT key, value FROM knowledge_meta WHERE key IN"
            " ('migration_complete', 'migration_id', 'semantic_digest')"
        ).fetchall()
    except sqlite3.DatabaseError:
        return False
    values = {str(row[0]): str(row[1]) for row in rows}
    if values.get("migration_complete") != "1":
        return False
    if values.get("migration_id") != str(payload.get("migration_id") or ""):
        return False
    return values.get("semantic_digest") == str(payload.get("semantic_digest") or "")


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


# ── P4.2: conservative legacy disposition ────────────────────────────────────

#: Reasons for a legacy fact that is kept but deliberately *not* promoted.
LEGACY_UNPROVEN_REASON: Final[str] = "unproven-role"
LEGACY_PROFILE_REASON: Final[str] = "profile-without-source"
LEGACY_NO_FACT_REASON: Final[str] = "legacy-node-without-fact-shell"
LEGACY_NODE_SOURCE_STATUSES: Final[tuple[str, ...]] = (
    "unverified",
    "partial",
    "missing",
    "conflict",
)

_LEGACY_NODE_REQUIRED_TABLES: Final[tuple[str, ...]] = (
    "memory2_nodes",
    "memory2_facts",
    "knowledge_statements",
    "knowledge_quarantine",
)
_LEGACY_NODE_REQUIRED_COLUMNS: Final[dict[str, tuple[str, ...]]] = {
    "memory2_nodes": (
        "id",
        "kind",
        "sector",
        "scope_type",
        "scope_key",
        "channel",
        "chat_id",
        "sender_id",
        "contact_id",
        "source_message_id",
        "source_role",
        "is_deleted",
    ),
    "memory2_facts": ("fact_id",),
    "knowledge_statements": ("statement_id",),
    "knowledge_quarantine": ("source_table", "source_pk", "reason"),
}

_SCOPE_KEY_RE: Final[re.Pattern[str]] = re.compile(r"^([a-z0-9_]+):(.+)$")

_LEGACY_LINK_AUDIT_STATUSES: Final[frozenset[str]] = frozenset(
    (*LEGACY_NODE_SOURCE_STATUSES, "resolved")
)
_LEGACY_LINK_REQUIRED_COLUMNS: Final[dict[str, tuple[str, ...]]] = {
    "contacts": ("id", "display_name", "preferred_name", "status"),
    "contact_identifiers": ("channel", "kind", "identifier", "contact_id"),
    "contact_aliases": (
        "contact_id",
        "alias",
        "source",
        "alias_kind",
        "scope_key",
        "status",
        "evidence_ref",
    ),
    "knowledge_identifier_bindings": (
        "binding_id",
        "channel",
        "kind",
        "namespace",
        "value",
        "person_id",
        "status",
        "mapping_verified",
        "evidence_ref",
        "valid_from_ms",
        "valid_until_ms",
    ),
}


@dataclass(frozen=True, slots=True)
class LegacyLinkCandidate:
    """One text-free, owner-reviewable candidate for one legacy node."""

    legacy_node_id: str
    kind: str
    sector: str
    scope_type: str
    scope_key: str
    channel: str | None
    chat_id: str | None
    sender_id: str | None
    contact_id: str | None
    source_message_id: str | None
    source_role: str | None
    source_status: str
    source_classes: tuple[str, ...]
    source_event_ids: tuple[str, ...]
    is_deleted: bool
    has_fact_shell: bool
    has_statement: bool
    quarantine_reasons: tuple[str, ...]
    speaker_candidates: tuple[dict[str, Any], ...]
    contact_context: dict[str, Any] | None
    candidate_state: str
    reason_codes: tuple[str, ...]
    proposed_disposition: str
    requires_owner_review: bool = True

    def to_payload(self) -> dict[str, Any]:
        return {
            "legacy_node_id": self.legacy_node_id,
            "kind": self.kind,
            "sector": self.sector,
            "scope_type": self.scope_type,
            "scope_key": self.scope_key,
            "channel": self.channel,
            "chat_id": self.chat_id,
            "sender_id": self.sender_id,
            "contact_id": self.contact_id,
            "source_message_id": self.source_message_id,
            "source_role": self.source_role,
            "source_status": self.source_status,
            "source_classes": list(self.source_classes),
            "source_event_ids": list(self.source_event_ids),
            "is_deleted": self.is_deleted,
            "has_fact_shell": self.has_fact_shell,
            "has_statement": self.has_statement,
            "quarantine_reasons": list(self.quarantine_reasons),
            "speaker_candidates": [dict(item) for item in self.speaker_candidates],
            "contact_context": self.contact_context,
            "candidate_state": self.candidate_state,
            "reason_codes": list(self.reason_codes),
            "proposed_disposition": self.proposed_disposition,
            "requires_owner_review": self.requires_owner_review,
        }


@dataclass(frozen=True, slots=True)
class LegacyLinkManifest:
    """Deterministic, private candidate rows for one audit and one target snapshot."""

    manifest_version: int
    audit_manifest_fingerprint: str
    target_fingerprint: str
    counts: dict[str, int]
    source_status_counts: dict[str, int]
    candidate_state_counts: dict[str, int]
    reason_counts: dict[str, int]
    disposition_counts: dict[str, int]
    candidates: tuple[LegacyLinkCandidate, ...]

    def to_payload(self) -> dict[str, Any]:
        return {
            "manifest_version": self.manifest_version,
            "audit_manifest_fingerprint": self.audit_manifest_fingerprint,
            "target_fingerprint": self.target_fingerprint,
            "counts": dict(self.counts),
            "source_status_counts": dict(self.source_status_counts),
            "candidate_state_counts": dict(self.candidate_state_counts),
            "reason_counts": dict(self.reason_counts),
            "disposition_counts": dict(self.disposition_counts),
            "candidates": [item.to_payload() for item in self.candidates],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_payload(), indent=2, sort_keys=True)


@dataclass(frozen=True, slots=True)
class _LegacyLinkIndexes:
    contacts: dict[str, dict[str, Any]]
    identifiers: dict[str, tuple[dict[str, Any], ...]]
    aliases: dict[str, tuple[dict[str, Any], ...]]
    bindings: dict[tuple[str, str, str], tuple[dict[str, Any], ...]]


def propose_legacy_links(
    audit_manifest: Path | str,
    target: Path | str,
) -> LegacyLinkManifest:
    """Build text-free legacy-link candidates from two consistent read-only inputs."""
    audit_path = _expand(audit_manifest)
    target_path = _expand(target)
    audit_payload, audit_fingerprint = _read_legacy_link_audit(audit_path)
    _require_snapshot_file(target_path)
    expected_target_fingerprint = str(audit_payload["target_fingerprint"])
    target_fingerprint = file_fingerprint(target_path)
    if target_fingerprint != expected_target_fingerprint:
        raise MigrationSourceError(
            "manifest_mismatch",
            "audit target fingerprint does not match the knowledge snapshot",
        )

    sidecars_before = _read_sidecars(target_path)
    connection: sqlite3.Connection | None = None
    try:
        connection = _connect_readonly(target_path)
        _require_integrity(connection, target_path)
        indexes = _read_legacy_link_indexes(connection, target_path)
        candidates = tuple(
            _legacy_link_candidate(row, indexes)
            for row in audit_payload["nodes"]
        )
    finally:
        if connection is not None:
            connection.close()
        _remove_read_sidecars(target_path, sidecars_before)

    if file_fingerprint(target_path) != expected_target_fingerprint:
        raise MigrationSourceError(
            "manifest_mismatch",
            "knowledge snapshot changed while candidates were being read",
        )

    source_status_counts = Counter(item.source_status for item in candidates)
    candidate_state_counts = Counter(item.candidate_state for item in candidates)
    reason_counts = Counter(
        reason for item in candidates for reason in item.reason_codes
    )
    disposition_counts = Counter(item.proposed_disposition for item in candidates)
    counts = {
        "nodes": len(candidates),
        "speaker_candidates": sum(len(item.speaker_candidates) for item in candidates),
        "requires_owner_review": sum(item.requires_owner_review for item in candidates),
    }
    return LegacyLinkManifest(
        manifest_version=MANIFEST_VERSION,
        audit_manifest_fingerprint=audit_fingerprint,
        target_fingerprint=target_fingerprint,
        counts=counts,
        source_status_counts=dict(sorted(source_status_counts.items())),
        candidate_state_counts=dict(sorted(candidate_state_counts.items())),
        reason_counts=dict(sorted(reason_counts.items())),
        disposition_counts=dict(sorted(disposition_counts.items())),
        candidates=candidates,
    )


def _read_legacy_link_audit(path: Path) -> tuple[dict[str, Any], str]:
    if not path.exists():
        raise MigrationSourceError("manifest_missing", f"audit manifest does not exist: {path}")
    if not path.is_file():
        raise MigrationSourceError("manifest_invalid", f"audit manifest is not a file: {path}")
    try:
        raw = path.read_bytes()
        payload = json.loads(raw)
    except (OSError, UnicodeError, ValueError) as exc:
        raise MigrationSourceError(
            "manifest_invalid", f"audit manifest is not readable JSON: {path}"
        ) from exc
    if not isinstance(payload, dict):
        raise MigrationSourceError("manifest_invalid", "audit manifest must be a JSON object")
    if payload.get("manifest_version") != MANIFEST_VERSION:
        raise MigrationSourceError(
            "manifest_invalid", "audit manifest version is not supported"
        )
    target_fingerprint = payload.get("target_fingerprint")
    if not isinstance(target_fingerprint, str) or not target_fingerprint:
        raise MigrationSourceError(
            "manifest_invalid", "audit manifest has no target fingerprint"
        )
    nodes = payload.get("nodes")
    if not isinstance(nodes, list):
        raise MigrationSourceError("manifest_invalid", "audit manifest has no node rows")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in nodes:
        if not isinstance(row, dict):
            raise MigrationSourceError("manifest_invalid", "audit node row is not an object")
        node_id = _audit_required_text(row, "legacy_node_id")
        if node_id in seen:
            raise MigrationSourceError(
                "manifest_invalid", "audit manifest contains duplicate legacy node ids"
            )
        seen.add(node_id)
        source_status = _audit_required_text(row, "source_status")
        if source_status not in _LEGACY_LINK_AUDIT_STATUSES:
            raise MigrationSourceError(
                "manifest_invalid", "audit node has an unsupported source status"
            )
        normalized.append(
            {
                "legacy_node_id": node_id,
                "kind": _audit_optional_text(row, "kind") or "",
                "sector": _audit_optional_text(row, "sector") or "",
                "scope_type": _audit_optional_text(row, "scope_type") or "",
                "scope_key": _audit_optional_text(row, "scope_key") or "",
                "channel": _audit_optional_text(row, "channel"),
                "chat_id": _audit_optional_text(row, "chat_id"),
                "sender_id": _audit_optional_text(row, "sender_id"),
                "contact_id": _audit_optional_text(row, "contact_id"),
                "source_message_id": _audit_optional_text(row, "source_message_id"),
                "source_role": _audit_optional_text(row, "source_role"),
                "source_status": source_status,
                "source_classes": _audit_text_list(row, "source_classes"),
                "source_event_ids": _audit_text_list(row, "source_event_ids"),
                "is_deleted": bool(row.get("is_deleted", False)),
                "has_fact_shell": bool(row.get("has_fact_shell", False)),
                "has_statement": bool(row.get("has_statement", False)),
                "quarantine_reasons": _audit_text_list(row, "quarantine_reasons"),
            }
        )
    normalized.sort(key=lambda item: item["legacy_node_id"])
    payload["nodes"] = normalized
    return payload, hashlib.sha256(raw).hexdigest()


def _audit_required_text(row: Mapping[str, Any], key: str) -> str:
    value = _audit_optional_text(row, key)
    if value is None:
        raise MigrationSourceError("manifest_invalid", f"audit node is missing {key}")
    return value


def _audit_optional_text(row: Mapping[str, Any], key: str) -> str | None:
    value = row.get(key)
    if value is None:
        return None
    if isinstance(value, (dict, list, tuple, set)):
        raise MigrationSourceError("manifest_invalid", f"audit field {key} is not scalar")
    text = str(value).strip()
    return text or None


def _audit_text_list(row: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = row.get(key, [])
    if value is None:
        return ()
    if not isinstance(value, list):
        raise MigrationSourceError("manifest_invalid", f"audit field {key} is not a list")
    return tuple(str(item).strip() for item in value if str(item).strip())


def _read_legacy_link_indexes(
    connection: sqlite3.Connection,
    path: Path,
) -> _LegacyLinkIndexes:
    tables = set(_table_names(connection))
    missing_tables = sorted(set(_LEGACY_LINK_REQUIRED_COLUMNS) - tables)
    if missing_tables:
        raise MigrationSourceError(
            "missing_required_table",
            f"candidate snapshot requires: {', '.join(missing_tables)}",
        )
    for table, required in _LEGACY_LINK_REQUIRED_COLUMNS.items():
        missing_columns = sorted(set(required) - set(_column_names(connection, table)))
        if missing_columns:
            raise MigrationSourceError(
                "missing_required_column",
                f"{table} missing: {', '.join(missing_columns)}",
            )

    contacts = {
        str(row["id"]): {
            "contact_id": str(row["id"]),
            "display_name": str(row["display_name"] or ""),
            "preferred_name": _text_or_none(row["preferred_name"]),
            "status": str(row["status"] or ""),
        }
        for row in connection.execute(
            "SELECT id, display_name, preferred_name, status FROM contacts ORDER BY id"
        )
    }
    identifiers: dict[str, list[dict[str, Any]]] = {}
    for row in connection.execute(
        "SELECT channel, kind, identifier, contact_id FROM contact_identifiers"
        " ORDER BY contact_id, channel, kind, identifier"
    ):
        contact_id = str(row["contact_id"])
        identifiers.setdefault(contact_id, []).append(
            {
                "channel": str(row["channel"] or ""),
                "kind": str(row["kind"] or ""),
                "identifier": str(row["identifier"] or ""),
            }
        )
    aliases: dict[str, list[dict[str, Any]]] = {}
    for row in connection.execute(
        "SELECT contact_id, alias, source, alias_kind, scope_key, status, evidence_ref"
        " FROM contact_aliases ORDER BY contact_id, alias, source, id"
    ):
        alias = _text_or_none(row["alias"])
        if alias is None:
            continue
        contact_id = str(row["contact_id"])
        aliases.setdefault(contact_id, []).append(
            {
                "value": alias,
                "source": str(row["source"] or ""),
                "alias_kind": str(row["alias_kind"] or ""),
                "scope_key": str(row["scope_key"] or ""),
                "status": str(row["status"] or ""),
                "evidence_ref": str(row["evidence_ref"] or ""),
            }
        )
    bindings: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for row in connection.execute(
        "SELECT binding_id, channel, kind, namespace, value, person_id, status,"
        " mapping_verified, evidence_ref, valid_from_ms, valid_until_ms"
        " FROM knowledge_identifier_bindings"
        " ORDER BY channel, kind, namespace, value, binding_id"
    ):
        channel = str(row["channel"] or "").strip().lower()
        kind = str(row["kind"] or "").strip().lower()
        value = str(row["value"] or "").strip()
        try:
            normalized_value = normalize_identifier_value(kind, value)
        except ValidationError:
            normalized_value = value
        key = (channel, kind, normalized_value)
        bindings.setdefault(key, []).append(
            {
                "binding_id": str(row["binding_id"]),
                "channel": channel,
                "kind": kind,
                "namespace": str(row["namespace"] or DEFAULT_NAMESPACE),
                "value": normalized_value,
                "person_id": str(row["person_id"]),
                "status": str(row["status"] or ""),
                "mapping_verified": row["mapping_verified"] == 1,
                "evidence_ref": str(row["evidence_ref"] or ""),
                "valid_from_ms": int(row["valid_from_ms"] or 0),
                "valid_until_ms": int(row["valid_until_ms"] or 0),
            }
        )
    return _LegacyLinkIndexes(
        contacts=contacts,
        identifiers={key: tuple(value) for key, value in identifiers.items()},
        aliases={key: tuple(value) for key, value in aliases.items()},
        bindings={key: tuple(value) for key, value in bindings.items()},
    )


def _legacy_link_candidate(
    row: Mapping[str, Any],
    indexes: _LegacyLinkIndexes,
) -> LegacyLinkCandidate:
    reasons: list[str] = []
    if row["source_status"] != "resolved":
        reasons.append("source_not_resolved")
    contact_context = _legacy_contact_context(row["contact_id"], indexes)
    contact = indexes.contacts.get(str(row["contact_id"] or ""))
    contact_active = contact is not None and contact["status"].lower() == "active"
    speaker_candidates: tuple[dict[str, Any], ...] = ()

    binding, binding_state, binding_reason = _legacy_sender_binding(row, indexes)
    if binding_reason:
        reasons.append(binding_reason)

    if (
        binding_state == "deterministic"
        and binding is not None
        and row["contact_id"]
        and contact is not None
        and not contact_active
    ):
        candidate_state = "conflict"
        disposition = "quarantined"
        reasons.append("contact_not_active")
    elif binding_state == "deterministic" and binding is not None:
        person = indexes.contacts.get(binding["person_id"])
        assert person is not None  # enforced by _legacy_sender_binding
        speaker_candidates = (
            {
                "person_id": binding["person_id"],
                "role": "speaker",
                "attribution": "transport",
                "evidence_kind": "verified_identifier_binding",
                "binding_id": binding["binding_id"],
                "evidence_ref": binding["evidence_ref"],
                "channel": binding["channel"],
                "kind": binding["kind"],
                "namespace": binding["namespace"],
                "identifier": binding["value"],
                "labels": {
                    "display_name": person["display_name"],
                    "preferred_name": person["preferred_name"],
                    "observed_aliases": list(indexes.aliases.get(binding["person_id"], ())),
                },
            },
        )
        candidate_state = "deterministic"
        disposition = "linked"
        if row["contact_id"] and row["contact_id"] != binding["person_id"]:
            reasons.append("contact_reference_only")
    elif binding_state == "conflict":
        candidate_state = "conflict"
        disposition = "quarantined"
    elif binding_state == "unsupported":
        candidate_state = "unresolved"
        disposition = "quarantined"
    elif binding_state == "unbound" and contact_active:
        candidate_state = "review"
        disposition = "raw_only"
        reasons.append("contact_reference_only")
    elif binding_state == "unbound" and contact is not None:
        candidate_state = "conflict"
        disposition = "quarantined"
        reasons.append("contact_not_active")
    else:
        candidate_state = "unresolved"
        disposition = "quarantined"

    return LegacyLinkCandidate(
        legacy_node_id=str(row["legacy_node_id"]),
        kind=str(row["kind"]),
        sector=str(row["sector"]),
        scope_type=str(row["scope_type"]),
        scope_key=str(row["scope_key"]),
        channel=row["channel"],
        chat_id=row["chat_id"],
        sender_id=row["sender_id"],
        contact_id=row["contact_id"],
        source_message_id=row["source_message_id"],
        source_role=row["source_role"],
        source_status=str(row["source_status"]),
        source_classes=tuple(row["source_classes"]),
        source_event_ids=tuple(row["source_event_ids"]),
        is_deleted=bool(row["is_deleted"]),
        has_fact_shell=bool(row["has_fact_shell"]),
        has_statement=bool(row["has_statement"]),
        quarantine_reasons=tuple(row["quarantine_reasons"]),
        speaker_candidates=speaker_candidates,
        contact_context=contact_context,
        candidate_state=candidate_state,
        reason_codes=tuple(dict.fromkeys(reasons)),
        proposed_disposition=disposition,
    )


def _legacy_contact_context(
    contact_id: str | None,
    indexes: _LegacyLinkIndexes,
) -> dict[str, Any] | None:
    if not contact_id:
        return None
    contact = indexes.contacts.get(contact_id)
    if contact is None:
        return {"contact_id": contact_id, "exists": False}
    return {
        "contact_id": contact_id,
        "exists": True,
        "status": contact["status"],
        "display_name": contact["display_name"],
        "preferred_name": contact["preferred_name"],
        "observed_identifiers": list(indexes.identifiers.get(contact_id, ())),
        "observed_aliases": list(indexes.aliases.get(contact_id, ())),
    }


def _legacy_sender_binding(
    row: Mapping[str, Any],
    indexes: _LegacyLinkIndexes,
) -> tuple[dict[str, Any] | None, str, str | None]:
    sender = str(row["sender_id"] or "").strip()
    channel = str(row["channel"] or "").strip().lower()
    if not sender:
        return None, "unbound", "sender_unbound"
    shape = _legacy_sender_shape(channel, sender)
    if shape is None:
        return None, "unsupported", "unsupported_identifier_context"
    kind, value = shape
    all_bindings = indexes.bindings.get((channel, kind, value), ())
    if not all_bindings:
        return None, "unbound", "sender_unbound"
    expected = tuple(
        item for item in all_bindings if item["namespace"] == DEFAULT_NAMESPACE
    )
    if len(expected) != len(all_bindings) or not expected:
        return None, "unsupported", "unsupported_identifier_context"
    valid = tuple(
        item
        for item in expected
        if (
            item["status"] == "active"
            and item["mapping_verified"]
            and item["valid_until_ms"] == 0
        )
    )
    if (
        len(expected) != 1
        or len(valid) != 1
        or valid[0]["person_id"] not in indexes.contacts
        or indexes.contacts[valid[0]["person_id"]]["status"].lower() != "active"
    ):
        return None, "conflict", "conflicting_bindings"
    return valid[0], "deterministic", "verified_sender_binding"


def _legacy_sender_shape(channel: str, sender: str) -> tuple[str, str] | None:
    if channel != "whatsapp":
        return None
    lowered = sender.lower()
    if "@" in lowered:
        domain = lowered.rsplit("@", 1)[1]
        if domain not in ("s.whatsapp.net", "lid"):
            return None
        kind = "lid" if domain == "lid" else "phone_jid"
        try:
            value = normalize_identifier_value(kind, lowered)
        except ValidationError:
            return None
        return kind, value
    try:
        value = normalize_identifier_value("phone_jid", sender)
    except ValidationError:
        return None
    return "phone_jid", f"{value}@s.whatsapp.net"


def _text_or_none(value: object) -> str | None:
    text = "" if value is None else str(value).strip()
    return text or None


@dataclass(frozen=True, slots=True)
class _LegacyResolution:
    """What the conservative legacy pass decided, counted by category and reason."""

    examined: int
    links: int
    quarantined: tuple[tuple[str, str, int], ...]
    unresolved_mentions: int
    kept_unproven: int
    notes: tuple[str, ...]

    def to_payload(self) -> dict[str, Any]:
        return {
            "examined": self.examined,
            "person_links": self.links,
            "quarantined": [
                {"table": table, "reason": reason, "count": count}
                for table, reason, count in self.quarantined
            ],
            "unresolved_mentions": self.unresolved_mentions,
            "kept_unproven": self.kept_unproven,
            "notes": list(self.notes),
        }


def _resolve_legacy_rows(store: KnowledgeStore, *, created_ms: int) -> _LegacyResolution:
    """Give every remaining legacy row an explicit disposition.

    Conservative by construction:

    * ``contact_fields`` text has no proven source and no ACL, so it is quarantined and
      never becomes a readable statement - it stays in its original table.
    * Legacy person columns (``sender_id``, ``contact_id``, ``about_sender``) are *not*
      translated into speakers or subjects.  A ``sender_id`` is recorded as an
      unresolved mention of the node's private metadata, which is exactly what it is:
      an unproven handle.  No contact is matched by name or by number shape.
    * A legacy node without a shared-fact shell keeps its text but never becomes
      person-readable; it is counted, not promoted.
    """
    reasons: Counter[tuple[str, str]] = Counter()
    notes: list[str] = []
    links = 0
    unresolved_mentions = 0
    kept_unproven = 0
    examined = 0

    field_rows = store.query(
        "SELECT rowid AS rid, contact_id, kind, value FROM contact_fields ORDER BY rowid"
    )
    for row in field_rows:
        examined += 1
        reasons[("contact_fields", LEGACY_PROFILE_REASON)] += 1
        _record_quarantine(
            store,
            table="contact_fields",
            source_pk=f"{row['contact_id']}:{row['kind']}:{row['rid']}",
            reason=LEGACY_PROFILE_REASON,
            created_ms=created_ms,
        )
    if field_rows:
        notes.append(
            "contact_fields text kept in place; quarantined because source and ACL are unproven"
        )

    node_rows = store.query(
        "SELECT n.id AS id, n.sender_id AS sender_id, n.contact_id AS contact_id,"
        " n.scope_key AS scope_key, n.content AS content,"
        " CASE WHEN f.fact_id IS NULL THEN 0 ELSE 1 END AS has_fact"
        " FROM memory2_nodes n LEFT JOIN memory2_facts f ON f.fact_id = n.id"
        " ORDER BY n.id"
    )
    for row in node_rows:
        statement_id = str(row["id"])
        has_shell = int(row["has_fact"]) == 1
        shells = store.query(
            "SELECT 1 FROM knowledge_statements WHERE statement_id = ?", (statement_id,)
        )
        already_person_readable = bool(shells)
        sender = "" if row["sender_id"] is None else str(row["sender_id"]).strip()
        contact = "" if row["contact_id"] is None else str(row["contact_id"]).strip()
        if contact:
            # A legacy contact link is *not* a proven speaker or subject: record it as
            # an unresolved mention so it is visible without granting any role.
            examined += 1
            _record_quarantine(
                store,
                table="memory2_nodes",
                source_pk=statement_id,
                reason=LEGACY_UNPROVEN_REASON,
                created_ms=created_ms,
            )
            reasons[("memory2_nodes", LEGACY_UNPROVEN_REASON)] += 1
        if sender and not already_person_readable:
            unresolved_mentions += 1
            store.execute(
                "INSERT INTO knowledge_meta (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (f"legacy_unresolved_mention:{statement_id}", sender),
            )
        if not has_shell and not already_person_readable:
            kept_unproven += 1
            examined += 1
            _record_quarantine(
                store,
                table="memory2_nodes",
                source_pk=statement_id,
                reason=LEGACY_NO_FACT_REASON,
                created_ms=created_ms,
            )
            reasons[("memory2_nodes", LEGACY_NO_FACT_REASON)] += 1

    fact_rows = store.query(
        "SELECT fs.fact_id AS fact_id, fs.author_principal AS author_principal,"
        " fs.occurred_ms AS occurred_ms FROM memory2_fact_sources fs ORDER BY fs.fact_id"
    )
    for row in fact_rows:
        statement_id = str(row["fact_id"])
        author = str(row["author_principal"] or "").strip()
        if not author:
            continue
        person_id = store.query_one(
            "SELECT person_id FROM knowledge_identifier_bindings"
            " WHERE status = 'active' AND value IN (?, ?) LIMIT 1",
            (author, author.split("@", 1)[0]),
        )
        if person_id is None:
            # The author is a proven transport principal without a person binding: the
            # speaker edge is left absent instead of guessing one.
            continue
        linked = store.execute(
            "INSERT INTO knowledge_statement_people (statement_id, person_id, role,"
            " evidence_source_id, evidence_revision, attribution, created_ms)"
            " VALUES (?, ?, 'speaker', ?, 1, 'transport', ?)"
            " ON CONFLICT DO NOTHING",
            (
                statement_id,
                str(person_id["person_id"]),
                f"legacy:{statement_id}",
                created_ms,
            ),
        )
        if linked.rowcount:
            links += 1
    if links:
        notes.append(
            "speaker edges reconstructed only from proven transport principals"
        )
    return _LegacyResolution(
        examined=examined,
        links=links,
        quarantined=tuple(
            sorted((table, reason, count) for (table, reason), count in reasons.items())
        ),
        unresolved_mentions=unresolved_mentions,
        kept_unproven=kept_unproven,
        notes=tuple(notes),
    )


@dataclass(frozen=True, slots=True)
class LegacyNodeRecord:
    """Private, text-free review row for one retained legacy node."""

    legacy_node_id: str
    kind: str
    sector: str
    scope_type: str
    scope_key: str
    channel: str | None
    chat_id: str | None
    sender_id: str | None
    contact_id: str | None
    source_message_id: str | None
    source_role: str | None
    is_deleted: bool
    has_fact_shell: bool
    has_statement: bool
    quarantine_reasons: tuple[str, ...]
    source_status: str
    source_classes: tuple[str, ...]
    source_event_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LegacyNodeInventory:
    """Aggregate plus private row metadata for the legacy-node audit."""

    target_path: Path
    target_fingerprint: str
    population_reason: str
    nodes: tuple[LegacyNodeRecord, ...]
    by_kind: tuple[tuple[str, int, int], ...]
    quarantine_reasons: tuple[tuple[str, int], ...]
    source_status_counts: tuple[tuple[str, int], ...]
    orphan_quarantine_rows: int = 0

    def counts(self) -> dict[str, int]:
        return {
            "nodes": len(self.nodes),
            "active": sum(not node.is_deleted for node in self.nodes),
            "with_source_message_id": sum(bool(node.source_message_id) for node in self.nodes),
            "with_sender_id": sum(bool(node.sender_id) for node in self.nodes),
            "with_contact_id": sum(bool(node.contact_id) for node in self.nodes),
            "with_fact_shell": sum(node.has_fact_shell for node in self.nodes),
            "with_statement": sum(node.has_statement for node in self.nodes),
            "orphan_quarantine_rows": self.orphan_quarantine_rows,
        }

    def to_json(self) -> str:
        return json.dumps(
            {
                "manifest_version": 1,
                "target_path": str(self.target_path),
                "target_fingerprint": self.target_fingerprint,
                "population_reason": self.population_reason,
                "counts": self.counts(),
                "by_kind": [list(item) for item in self.by_kind],
                "quarantine_reasons": [list(item) for item in self.quarantine_reasons],
                "source_status_counts": [list(item) for item in self.source_status_counts],
                "nodes": [_jsonable(node) for node in self.nodes],
            },
            indent=2,
            sort_keys=True,
        )


def _legacy_text_or_none(value: object) -> str | None:
    text = "" if value is None else str(value).strip()
    return text or None


def _legacy_source_index(
    *,
    inbound_dir: Path | None,
    processing_db: Path | None,
) -> tuple[
    dict[tuple[str, str], set[str]],
    dict[str, set[str]],
    dict[str, set[str]],
]:
    """Index source identities without carrying source content into the audit."""
    variants: dict[tuple[str, str], set[str]] = {}
    classes: dict[str, set[str]] = {}
    event_ids: dict[str, set[str]] = {}
    if processing_db is not None:
        sidecars_before = _read_sidecars(processing_db)
        connection: sqlite3.Connection | None = None
        try:
            connection = _connect_readonly(processing_db)
            _require_integrity(connection, processing_db)
            tables = set(_table_names(connection))
            if "events" not in tables:
                raise MigrationSourceError(
                    "missing_required_table", "processing source requires an events table"
                )
            columns = set(_columns(connection, "events"))
            required = {
                "event_id",
                "source_message_id",
                "kind",
                "revision",
                "chat_id",
                "principal",
                "channel",
                "occurred_ms",
            }
            missing = sorted(required - columns)
            if missing:
                raise MigrationSourceError(
                    "missing_required_column",
                    "processing events missing: " + ", ".join(missing),
                )
            try:
                rows = connection.execute(
                    "SELECT event_id, source_message_id, kind, revision, chat_id, principal, channel,"
                    " occurred_ms FROM events WHERE source_message_id IS NOT NULL"
                ).fetchall()
            except sqlite3.DatabaseError as exc:
                raise MigrationSourceError(
                    "unsupported_schema", "could not read processing source events"
                ) from exc
            for row in rows:
                source_ref = _legacy_text_or_none(row["source_message_id"])
                if source_ref is None:
                    continue
                source_class = "processing_events"
                variants.setdefault((source_class, source_ref), set()).add(
                    lineage_fingerprint(
                        source_class,
                        source_ref,
                        row["kind"],
                        row["revision"],
                        row["chat_id"],
                        row["principal"],
                        row["channel"],
                        row["occurred_ms"],
                    )
                )
                classes.setdefault(source_ref, set()).add(source_class)
                event_id = _legacy_text_or_none(row["event_id"])
                if event_id is not None:
                    event_ids.setdefault(source_ref, set()).add(event_id)
        finally:
            if connection is not None:
                connection.close()
            _remove_read_sidecars(processing_db, sidecars_before)

    if inbound_dir is not None:
        for path in sorted(inbound_dir.glob("*.jsonl")):
            chat_key = path.stem
            for index, record in enumerate(_iter_jsonl(path)):
                if index == 0 and "chat_id" in record:
                    continue
                source_ref = _legacy_text_or_none(
                    record.get("message_id") or record.get("id")
                )
                if source_ref is None:
                    continue
                source_class = "inbound_archive"
                variants.setdefault((source_class, source_ref), set()).add(
                    lineage_fingerprint(
                        source_class,
                        source_ref,
                        record.get("timestamp"),
                        chat_key,
                        record.get("from") or record.get("sender"),
                        record.get("role"),
                    )
                )
                classes.setdefault(source_ref, set()).add(source_class)
    return variants, classes, event_ids


def _legacy_source_status(
    source_message_id: str | None,
    *,
    source_checked: bool,
    variants: Mapping[tuple[str, str], set[str]],
    classes: Mapping[str, set[str]],
    event_ids: Mapping[str, set[str]],
) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    if not source_message_id:
        return "missing", (), ()
    if not source_checked:
        return "unverified", (), ()
    source_classes = tuple(sorted(classes.get(source_message_id, set())))
    source_event_ids = tuple(sorted(event_ids.get(source_message_id, set())))
    matching_variants = [
        fingerprints
        for (source_class, source_ref), fingerprints in variants.items()
        if source_ref == source_message_id
    ]
    if not matching_variants:
        return "missing", source_classes, source_event_ids
    if any(len(fingerprints) > 1 for fingerprints in matching_variants):
        return "conflict", source_classes, source_event_ids
    # An id-only match is deliberately not a resolved attribution.  Chat, sender,
    # timestamp and revision matching belongs to the next audit phase.
    return "partial", source_classes, source_event_ids


def inspect_legacy_nodes(
    target: Path | str,
    *,
    inbound_dir: Path | str | None = None,
    processing_db: Path | str | None = None,
) -> LegacyNodeInventory:
    """Inventory all nodes quarantined without a fact shell, read-only and text-free."""
    target_path = _expand(target)
    _require_snapshot_file(target_path)
    inbound_path = _expand(inbound_dir) if inbound_dir is not None else None
    processing_path = _expand(processing_db) if processing_db is not None else None
    if inbound_path is not None and not inbound_path.is_dir():
        raise MigrationSourceError(
            "missing_source", f"inbound source is not a directory: {inbound_path}"
        )
    if processing_path is not None:
        _require_snapshot_file(processing_path)

    source_checked = inbound_path is not None or processing_path is not None
    variants: dict[tuple[str, str], set[str]] = {}
    source_classes: dict[str, set[str]] = {}
    source_event_ids: dict[str, set[str]] = {}
    if source_checked:
        variants, source_classes, source_event_ids = _legacy_source_index(
            inbound_dir=inbound_path,
            processing_db=processing_path,
        )

    sidecars_before = _read_sidecars(target_path)
    connection: sqlite3.Connection | None = None
    try:
        connection = _connect_readonly(target_path)
        _require_integrity(connection, target_path)
        tables = set(_table_names(connection))
        missing = sorted(set(_LEGACY_NODE_REQUIRED_TABLES) - tables)
        if missing:
            raise MigrationSourceError(
                "missing_required_table",
                f"legacy node audit requires: {', '.join(missing)}",
            )
        for table, required_columns in _LEGACY_NODE_REQUIRED_COLUMNS.items():
            missing_columns = sorted(
                set(required_columns) - set(_columns(connection, table))
            )
            if missing_columns:
                raise MigrationSourceError(
                    "missing_required_column",
                    f"{table} missing: {', '.join(missing_columns)}",
                )

        try:
            quarantine_rows = connection.execute(
                "SELECT source_pk, reason FROM knowledge_quarantine"
                " WHERE source_table = ? ORDER BY source_pk, reason",
                ("memory2_nodes",),
            ).fetchall()
        except sqlite3.DatabaseError as exc:
            raise MigrationSourceError(
                "unsupported_schema", "could not read legacy node quarantine rows"
            ) from exc
        reasons_by_node: dict[str, list[str]] = {}
        for row in quarantine_rows:
            reasons_by_node.setdefault(str(row["source_pk"]), []).append(str(row["reason"]))
        legacy_ids = tuple(
            sorted(
                node_id
                for node_id, reasons in reasons_by_node.items()
                if LEGACY_NO_FACT_REASON in reasons
            )
        )
        if legacy_ids:
            placeholders = ", ".join("?" for _ in legacy_ids)
            try:
                rows = connection.execute(
                    "SELECT n.id, n.kind, n.sector, n.scope_type, n.scope_key, n.channel,"
                    " n.chat_id, n.sender_id, n.contact_id, n.source_message_id, n.source_role,"
                    " n.is_deleted, CASE WHEN f.fact_id IS NULL THEN 0 ELSE 1 END AS has_fact_shell,"
                    " CASE WHEN s.statement_id IS NULL THEN 0 ELSE 1 END AS has_statement"
                    " FROM memory2_nodes AS n"
                    " LEFT JOIN memory2_facts AS f ON f.fact_id = n.id"
                    " LEFT JOIN knowledge_statements AS s ON s.statement_id = n.id"
                    f" WHERE n.id IN ({placeholders}) ORDER BY n.id",
                    legacy_ids,
                ).fetchall()
            except sqlite3.DatabaseError as exc:
                raise MigrationSourceError(
                    "unsupported_schema", "could not read legacy node metadata"
                ) from exc
        else:
            rows = []
        if len(rows) != len(legacy_ids):
            raise MigrationSourceError(
                "orphan_legacy_node",
                f"quarantine references {len(legacy_ids) - len(rows)} missing node(s)",
            )

        records: list[LegacyNodeRecord] = []
        for row in rows:
            source_message_id = _legacy_text_or_none(row["source_message_id"])
            source_status, matched_classes, matched_event_ids = _legacy_source_status(
                source_message_id,
                source_checked=source_checked,
                variants=variants,
                classes=source_classes,
                event_ids=source_event_ids,
            )
            records.append(
                LegacyNodeRecord(
                    legacy_node_id=str(row["id"]),
                    kind=str(row["kind"]),
                    sector=str(row["sector"]),
                    scope_type=str(row["scope_type"]),
                    scope_key=str(row["scope_key"]),
                    channel=_legacy_text_or_none(row["channel"]),
                    chat_id=_legacy_text_or_none(row["chat_id"]),
                    sender_id=_legacy_text_or_none(row["sender_id"]),
                    contact_id=_legacy_text_or_none(row["contact_id"]),
                    source_message_id=source_message_id,
                    source_role=_legacy_text_or_none(row["source_role"]),
                    is_deleted=bool(row["is_deleted"]),
                    has_fact_shell=bool(row["has_fact_shell"]),
                    has_statement=bool(row["has_statement"]),
                    quarantine_reasons=tuple(reasons_by_node[str(row["id"])]),
                    source_status=source_status,
                    source_classes=matched_classes,
                    source_event_ids=matched_event_ids,
                )
            )
    finally:
        if connection is not None:
            connection.close()
        _remove_read_sidecars(target_path, sidecars_before)

    kind_counts: dict[str, list[int]] = {}
    reason_counts: Counter[str] = Counter()
    source_status_counts: Counter[str] = Counter()
    for node in records:
        counts = kind_counts.setdefault(node.kind, [0, 0])
        counts[0] += 1
        counts[1] += int(not node.is_deleted)
        reason_counts.update(node.quarantine_reasons)
        source_status_counts[node.source_status] += 1

    return LegacyNodeInventory(
        target_path=target_path,
        target_fingerprint=file_fingerprint(target_path),
        population_reason=LEGACY_NO_FACT_REASON,
        nodes=tuple(records),
        by_kind=tuple(
            (kind, counts[0], counts[1]) for kind, counts in sorted(kind_counts.items())
        ),
        quarantine_reasons=tuple(sorted(reason_counts.items())),
        source_status_counts=tuple(sorted(source_status_counts.items())),
    )


def _parse_legacy_scope(scope_key: str) -> tuple[str, str] | None:
    """Split the legacy ``channel:chat_id`` scope key.  Never guesses a shape."""
    match = _SCOPE_KEY_RE.match(str(scope_key or ""))
    if match is None:
        return None
    return match.group(1), match.group(2)


# ══════════════════════════════════════════════════════════════════════════════
# Phase 2 / Task 4: lineage inventory and idempotent legacy import
# ══════════════════════════════════════════════════════════════════════════════
#
# The inventory is metadata, counts and schema only.  It reads no message content into
# any output, makes no provider or model call, and never parses or OCRs a PDF.  Every
# inspected row gets exactly one decision with a reason and a stable fingerprint, so a
# second import is provably a no-op.

#: The closed decision vocabulary of the lineage inventory.
LINEAGE_DECISIONS: Final[tuple[str, ...]] = (
    "import",
    "link",
    "rebuild",
    "skip",
    "quarantine",
)

#: Source classes the lineage inventory covers.
LINEAGE_SOURCE_CLASSES: Final[tuple[str, ...]] = (
    "inbound_archive",
    "processing_events",
    "session_jsonl",
    "knowledge_nodes",
    "knowledge_statements",
    "knowledge_sources",
    "knowledge_jobs",
    "media_reference",
)


@dataclass(frozen=True, slots=True)
class LineageDecision:
    """One classified row.  ``source_ref`` is an identity, never content."""

    source_class: str
    source_ref: str
    decision: str
    reason: str
    fingerprint: str


@dataclass(frozen=True, slots=True)
class LineageInventory:
    """Aggregate-only result of a lineage inventory pass."""

    decisions: tuple[LineageDecision, ...] = ()
    schema: tuple[tuple[str, str], ...] = ()
    statements: tuple[str, ...] = ()
    eligible_model_jobs: int = 0

    def counts(self) -> dict[str, int]:
        out = {name: 0 for name in LINEAGE_DECISIONS}
        for item in self.decisions:
            out[item.decision] = out.get(item.decision, 0) + 1
        return out

    def reasons(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for item in self.decisions:
            out[item.reason] = out.get(item.reason, 0) + 1
        return out

    def to_json(self) -> str:
        return json.dumps(
            {
                "counts": self.counts(),
                "reasons": self.reasons(),
                "schema": [list(item) for item in self.schema],
                "statements": list(self.statements),
                "eligible_model_jobs": int(self.eligible_model_jobs),
            },
            sort_keys=True,
        )


@dataclass(frozen=True, slots=True)
class LineageImportReport:
    """What an apply pass did.  Idempotent by construction on a second run."""

    dry_run: bool = True
    imported: int = 0
    linked: int = 0
    rebuilt: int = 0
    skipped: int = 0
    quarantined: int = 0
    model_jobs_scheduled: int = 0
    eligible_model_jobs: int = 0
    statements: tuple[str, ...] = ()

    def to_json(self) -> str:
        return json.dumps(
            {
                "dry_run": bool(self.dry_run),
                "imported": int(self.imported),
                "linked": int(self.linked),
                "rebuilt": int(self.rebuilt),
                "skipped": int(self.skipped),
                "quarantined": int(self.quarantined),
                "model_jobs_scheduled": int(self.model_jobs_scheduled),
                "eligible_model_jobs": int(self.eligible_model_jobs),
            },
            sort_keys=True,
        )


def lineage_fingerprint(*parts: object) -> str:
    """Stable identity of one classified row.  Never contains row content."""
    raw = json.dumps([str(item) for item in parts], separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _decide_event(row: Mapping[str, Any]) -> tuple[str, str]:
    """Classify one ``ProcessingStore.events`` row without reading its payload text."""
    event_id = str(row.get("event_id") or "").strip()
    kind = str(row.get("kind") or "").strip().lower()
    revision = int(row.get("revision") or 0)
    if not event_id:
        return "quarantine", "missing_event_identity"
    if kind == "receipt":
        # A transport receipt is not a message and not proof that a human read anything.
        return "skip", "receipt_is_not_a_message"
    if kind == "reaction":
        return "link", "reaction_is_metadata_only"
    if kind == "delete":
        return "link", "revocation_projection"
    if kind != "message":
        return "skip", "unsupported_event_kind"
    if revision <= 0:
        return "quarantine", "missing_source_revision"
    if str(row.get("direction") or "in").lower() == "out":
        # A bot answer is not independent human evidence.
        return "link", "bot_answer_is_not_human_evidence"
    if not str(row.get("chat_id") or "").strip():
        return "quarantine", "missing_chat_scope"
    return "import", "canonical_forward_capture"


def _decide_medium(row: Mapping[str, Any]) -> tuple[str, str]:
    return "skip", "media_reference_only"


def _decide_archive_message(record: Mapping[str, Any], *, chat_key: str) -> tuple[str, str]:
    message_id = str(record.get("message_id") or record.get("id") or "").strip()
    timestamp = record.get("timestamp")
    sender = str(record.get("from") or record.get("sender") or "").strip()
    if not message_id:
        return "quarantine", "missing_provider_id"
    if timestamp in (None, ""):
        return "quarantine", "missing_source_revision"
    if not chat_key:
        return "quarantine", "missing_chat_scope"
    if str(record.get("role") or "").strip().lower() in ("assistant", "bot"):
        return "link", "bot_answer_is_not_human_evidence"
    if not sender:
        # Legacy rights with insufficient evidence fail closed.
        return "quarantine", "unproven_audience"
    return "import", "legacy_archive_message"


def _decide_session_record(record: Mapping[str, Any]) -> tuple[str, str]:
    if not str(record.get("timestamp") or record.get("ts") or "").strip():
        return "quarantine", "missing_source_revision"
    return "rebuild", "session_state_projection"


def _decide_canonical_row(table: str) -> tuple[str, str]:
    """Rows already inside the knowledge store are the target, not a source."""
    if table == "knowledge_jobs":
        return "skip", "already_canonical_job"
    return "skip", "already_canonical"


def _table_names(connection: sqlite3.Connection) -> tuple[str, ...]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
        " AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return tuple(str(row[0]) for row in rows)


def _columns(connection: sqlite3.Connection, table: str) -> tuple[str, ...]:
    try:
        return tuple(str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})"))
    except sqlite3.Error:  # pragma: no cover - defensive
        return ()


def _read_processing_lineage_rows(
    path: Path,
) -> tuple[tuple[str, ...], tuple[dict[str, Any], ...]]:
    """Read the lineage metadata contract without leaving read sidecars."""
    _require_snapshot_file(path)
    sidecars_before = _read_sidecars(path)
    connection: sqlite3.Connection | None = None
    try:
        connection = _connect_readonly(path)
        _require_integrity(connection, path)
        try:
            tables = _table_names(connection)
        except sqlite3.DatabaseError as exc:
            raise MigrationSourceError(
                "unsupported_schema", "could not inspect processing source tables"
            ) from exc
        if "events" not in tables:
            raise MigrationSourceError(
                "missing_required_table", "processing source requires an events table"
            )
        required = {"event_id", "kind", "revision", "direction", "chat_id", "account"}
        missing = sorted(required - set(_columns(connection, "events")))
        if missing:
            raise MigrationSourceError(
                "missing_required_column",
                "processing events missing: " + ", ".join(missing),
            )
        try:
            rows = connection.execute(
                "SELECT event_id, kind, revision, direction, chat_id, account FROM events"
            ).fetchall()
        except sqlite3.DatabaseError as exc:
            raise MigrationSourceError(
                "unsupported_schema", "could not read processing lineage events"
            ) from exc
        return tables, tuple({key: row[key] for key in row.keys()} for row in rows)
    finally:
        if connection is not None:
            connection.close()
        _remove_read_sidecars(path, sidecars_before)


def inspect_lineage_sources(
    *,
    inbound_dir: Path | str | None = None,
    processing_db: Path | str | None = None,
    session_state_dir: Path | str | None = None,
    knowledge_db: Path | str | None = None,
    media_root: Path | str | None = None,
) -> LineageInventory:
    """Classify every legacy row by metadata only: counts, schema, identities.

    No content leaves the machine, no provider is contacted and no PDF is opened.  The
    caller receives decisions, aggregate counts and a redacted schema summary.
    """
    decisions: list[LineageDecision] = []
    schema: list[tuple[str, str]] = []
    statements: list[str] = []
    eligible_jobs = 0

    # ── processing store ─────────────────────────────────────────────────────
    if processing_db is not None and Path(processing_db).exists():
        tables, rows = _read_processing_lineage_rows(_expand(processing_db))
        schema.append(("processing", ",".join(tables)))
        for record in rows:
            decision, reason = _decide_event(record)
            if decision == "import":
                eligible_jobs += 1
            decisions.append(
                LineageDecision(
                    source_class="processing_events",
                    source_ref=str(record.get("event_id") or ""),
                    decision=decision,
                    reason=reason,
                    fingerprint=lineage_fingerprint(
                        "processing_events",
                        record.get("event_id"),
                        record.get("kind"),
                        record.get("revision"),
                    ),
                )
            )
        statements.append(
            f"processing: {len(tables)} table(s), "
            f"{sum(1 for item in decisions if item.source_class == 'processing_events')} event(s)"
        )

    # ── inbound archives ─────────────────────────────────────────────────────
    if inbound_dir is not None and Path(inbound_dir).exists():
        for path in sorted(Path(inbound_dir).glob("*.jsonl")):
            chat_key = path.stem
            for index, record in enumerate(_iter_jsonl(path)):
                if index == 0 and "chat_id" in record:
                    continue  # archive metadata header, not a message
                decision, reason = _decide_archive_message(record, chat_key=chat_key)
                if decision == "import":
                    eligible_jobs += 1
                decisions.append(
                    LineageDecision(
                        source_class="inbound_archive",
                        source_ref=str(record.get("message_id") or record.get("id") or ""),
                        decision=decision,
                        reason=reason,
                        fingerprint=lineage_fingerprint(
                            "inbound_archive",
                            path.name,
                            record.get("message_id") or record.get("id"),
                            record.get("timestamp"),
                        ),
                    )
                )
        statements.append(
            f"inbound: {sum(1 for item in decisions if item.source_class == 'inbound_archive')} line(s)"
        )

    # ── session state JSONL ──────────────────────────────────────────────────
    if session_state_dir is not None and Path(session_state_dir).exists():
        for path in sorted(Path(session_state_dir).glob("*.jsonl")):
            for record in _iter_jsonl(path):
                decision, reason = _decide_session_record(record)
                decisions.append(
                    LineageDecision(
                        source_class="session_jsonl",
                        source_ref=str(record.get("session") or path.stem),
                        decision=decision,
                        reason=reason,
                        fingerprint=lineage_fingerprint(
                            "session_jsonl", path.name, record.get("timestamp") or record.get("ts")
                        ),
                    )
                )

    # ── existing knowledge store ─────────────────────────────────────────────
    if knowledge_db is not None and Path(knowledge_db).exists():
        connection = _connect_readonly(Path(knowledge_db))
        try:
            tables = _table_names(connection)
            schema.append(("knowledge", ",".join(tables)))
            for table, source_class in (
                ("memory2_nodes", "knowledge_nodes"),
                ("knowledge_statements", "knowledge_statements"),
                ("knowledge_statement_sources", "knowledge_sources"),
                ("knowledge_jobs", "knowledge_jobs"),
            ):
                if table not in tables:
                    continue
                decision, reason = _decide_canonical_row(table)
                columns = _columns(connection, table)
                id_column = "statement_id" if "statement_id" in columns else "id"
                rows = connection.execute(f"SELECT {id_column} AS row_id FROM {table}").fetchall()
                for row in rows:
                    decisions.append(
                        LineageDecision(
                            source_class=source_class,
                            source_ref=str(row["row_id"]),
                            decision=decision,
                            reason=reason,
                            fingerprint=lineage_fingerprint(table, row["row_id"]),
                        )
                    )
        finally:
            connection.close()

    # ── media references ─────────────────────────────────────────────────────
    if media_root is not None and Path(media_root).exists():
        for path in sorted(Path(media_root).rglob("*")):
            if not path.is_file():
                continue
            decision, reason = _decide_medium({})
            # A media file is never opened, parsed, OCRed or hashed again.
            decisions.append(
                LineageDecision(
                    source_class="media_reference",
                    source_ref=path.name,
                    decision=decision,
                    reason=reason,
                    fingerprint=lineage_fingerprint("media_reference", path.name, path.suffix),
                )
            )

    statements.append(
        "no content was read into this report; no provider or model call was made;"
        " no PDF was parsed or OCRed"
    )
    return LineageInventory(
        decisions=tuple(decisions),
        schema=tuple(schema),
        statements=tuple(statements),
        eligible_model_jobs=eligible_jobs,
    )


def _iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                text = line.strip()
                if not text:
                    continue
                try:
                    parsed = json.loads(text)
                except (TypeError, ValueError):
                    continue
                if isinstance(parsed, dict):
                    yield parsed
    except OSError:  # pragma: no cover - defensive
        return


def import_lineage(
    inventory: LineageInventory,
    *,
    apply: bool = False,
    target: Any | None = None,
    quarantine_sink: Any | None = None,
    allow_model_jobs: bool = False,
    now_ms: int = 0,
) -> LineageImportReport:
    """Apply an inventory idempotently, or report what an apply would do.

    ``--allow-model-jobs`` is the only way to schedule derived work: without it the exact
    eligible count is reported and nothing is scheduled.  PDFs are never parsed or OCRed,
    and no permanent legacy read path is created - the canonical log receives the rows and
    every other decision is recorded or skipped.
    """
    counts = inventory.counts()
    if not apply:
        return LineageImportReport(
            dry_run=True,
            imported=0,
            linked=counts.get("link", 0),
            rebuilt=counts.get("rebuild", 0),
            skipped=counts.get("skip", 0),
            quarantined=counts.get("quarantine", 0),
            model_jobs_scheduled=0,
            eligible_model_jobs=int(inventory.eligible_model_jobs),
            statements=inventory.statements,
        )

    imported = 0
    quarantined = 0
    for item in inventory.decisions:
        if item.decision == "import" and target is not None:
            append = getattr(target, "append_event", None)
            if callable(append):
                # The fingerprint is the deterministic event key: a second apply pass
                # resolves to the very same canonical row instead of a new one.
                append(
                    event_key=f"lineage:{item.fingerprint}",
                    event_id=f"lineage-{item.fingerprint}",
                    trace_id=f"lineage:{item.fingerprint}",
                    payload={"kind": "legacy_import", "source_class": item.source_class},
                    now_ms=int(now_ms),
                    origin="lineage_import",
                )
                imported += 1
        elif item.decision == "quarantine" and quarantine_sink is not None:
            record = getattr(quarantine_sink, "record_quarantine", None)
            if callable(record):
                record(
                    source_table=item.source_class,
                    source_pk=item.source_ref or item.fingerprint,
                    reason=item.reason,
                    detail={"fingerprint": item.fingerprint},
                    now_ms=int(now_ms),
                )
                quarantined += 1

    scheduled = 0
    if allow_model_jobs:
        # Only now may derived work be scheduled, and only for eligible rows.
        scheduled = int(inventory.eligible_model_jobs)

    return LineageImportReport(
        dry_run=False,
        imported=imported,
        linked=counts.get("link", 0),
        rebuilt=counts.get("rebuild", 0),
        skipped=counts.get("skip", 0),
        quarantined=quarantined or counts.get("quarantine", 0),
        model_jobs_scheduled=scheduled,
        eligible_model_jobs=int(inventory.eligible_model_jobs),
        statements=inventory.statements,
    )
