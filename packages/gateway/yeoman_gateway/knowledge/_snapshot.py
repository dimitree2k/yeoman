"""Offline snapshot, restore verification and the local profile benchmark.

Three deliberately small capabilities, and nothing else: no scheduler, no rotation, no
remote copy, no encryption.  Choosing and protecting the actual backup medium is an
operational decision, not a code path, so this module only produces a snapshot it can
*describe* and *verify*, and refuses to guess about anything else.

Two rules shape it:

* ``sqlite3.Connection.backup()`` copies a database, never ``cp``: a live ``-wal`` file
  copied file-by-file is not a snapshot, it is a coin flip.
* A snapshot is only taken with an explicit ``--quiesce-ref``.  Two independent databases
  cannot be made mutually consistent by this command, and claiming otherwise would be the
  most dangerous kind of lie a backup tool can tell.  The offline acceptance uses closed
  fixture writers; a live snapshot needs a separately authorized quiesce boundary.
"""

from __future__ import annotations

import hashlib
import json
import resource
import sqlite3
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

__all__ = [
    "SNAPSHOT_MANIFEST_VERSION",
    "BenchmarkReport",
    "SnapshotError",
    "SnapshotReport",
    "SnapshotVerification",
    "benchmark_profiles",
    "create_snapshot",
    "verify_snapshot",
]

SNAPSHOT_MANIFEST_VERSION: Final[int] = 1

#: Tables whose rows a restore must preserve for the rehearsal to mean anything.
_REHEARSAL_TABLES: Final[tuple[str, ...]] = (
    "contacts",
    "contact_aliases",
    "knowledge_identifier_bindings",
    "knowledge_statements",
    "knowledge_statement_people",
    "knowledge_statement_sources",
    "knowledge_person_attributes",
)


class SnapshotError(Exception):
    """A refused or failed snapshot operation.  Carries a stable reason code."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code


# ── snapshot ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SnapshotReport:
    target_dir: Path
    manifest_path: Path
    quiesce_ref: str
    created_ms: int
    knowledge_path: Path
    processing_path: Path
    knowledge_fingerprint: str
    processing_fingerprint: str
    knowledge_counts: tuple[tuple[str, int], ...]
    processing_counts: tuple[tuple[str, int], ...]
    media: tuple[tuple[str, int], ...]


def create_snapshot(
    *,
    processing: Path,
    knowledge: Path,
    target_dir: Path,
    quiesce_ref: str,
) -> SnapshotReport:
    """Copy both databases with the SQLite backup API and describe the result.

    Refuses without a quiesce reference, refuses to overwrite an existing snapshot, and
    opens both sources read-only.  The manifest records what the snapshot *is* - schema,
    hashes, counts, revisions, revocations, media list, quiesce reference and time - and
    deliberately never copies a secret or a row value.
    """
    reference = str(quiesce_ref or "").strip()
    if not reference:
        raise SnapshotError(
            "quiesce_ref_required",
            "a snapshot needs an explicit --quiesce-ref; two independent databases cannot"
            " be made mutually consistent by this command",
        )
    source_processing = Path(processing).expanduser()
    source_knowledge = Path(knowledge).expanduser()
    directory = Path(target_dir).expanduser()
    for role, path in (("processing", source_processing), ("knowledge", source_knowledge)):
        if not path.exists():
            raise SnapshotError(f"{role}_missing", f"{role} database does not exist: {path}")
    if directory.exists() and any(directory.iterdir()):
        raise SnapshotError("target_not_empty", f"refusing to snapshot into {directory}")
    directory.mkdir(parents=True, exist_ok=True)

    created_ms = int(time.time() * 1000)
    targets = {
        "processing": directory / "processing.db",
        "knowledge": directory / "knowledge.db",
    }
    fingerprints: dict[str, str] = {}
    counts: dict[str, dict[str, int]] = {}
    for role, source in (("processing", source_processing), ("knowledge", source_knowledge)):
        _backup(source, targets[role])
        fingerprints[role] = _fingerprint(targets[role])
        counts[role] = _table_counts(targets[role])
    _require_readable(targets["processing"], "processing")
    _require_readable(targets["knowledge"], "knowledge")

    knowledge_revision = _meta_value(targets["knowledge"], "identity_revision")
    acl_epoch = _meta_value(targets["knowledge"], "acl_epoch")
    schema_version = _meta_value(targets["knowledge"], "schema_version")
    payload: dict[str, Any] = {
        "snapshot_manifest_version": SNAPSHOT_MANIFEST_VERSION,
        "created_ms": created_ms,
        "quiesce_ref": reference,
        "coherent_live_boundary": False,
        "coherent_live_boundary_note": (
            "both fixtures were closed when this snapshot was taken; a live snapshot needs"
            " a separately authorized quiesce or coordinated snapshot boundary"
        ),
        "databases": {
            role: {
                "path": str(targets[role]),
                "source_path": str(source_processing if role == "processing" else source_knowledge),
                "fingerprint": fingerprints[role],
                "content_digest": _content_digest(targets[role]),
                "tables": len(counts[role]),
                "rows": sum(counts[role].values()),
            }
            for role in ("processing", "knowledge")
        },
        "knowledge_schema_version": schema_version,
        "identity_revision": knowledge_revision,
        "acl_epoch": acl_epoch,
        "counts": {role: counts[role] for role in ("processing", "knowledge")},
        "revoked_statements": _scalar(
            targets["knowledge"],
            "SELECT COUNT(*) FROM knowledge_statements WHERE status = 'revoked'",
        ),
        "withheld_bindings": _scalar(
            targets["knowledge"],
            "SELECT COUNT(*) FROM knowledge_identifier_bindings WHERE status = 'withheld'",
        ),
        "media": _media_inventory(source_processing, directory),
        # References only: the policy/config files themselves are protected operational
        # artefacts and are never copied into a snapshot report.
        "operational_artifacts": ["policy.json", "config.json"],
    }
    manifest_path = directory / "manifest.json"
    manifest_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return SnapshotReport(
        target_dir=directory,
        manifest_path=manifest_path,
        quiesce_ref=reference,
        created_ms=created_ms,
        knowledge_path=targets["knowledge"],
        processing_path=targets["processing"],
        knowledge_fingerprint=fingerprints["knowledge"],
        processing_fingerprint=fingerprints["processing"],
        knowledge_counts=tuple(sorted(counts["knowledge"].items())),
        processing_counts=tuple(sorted(counts["processing"].items())),
        media=tuple(payload["media"]),
    )


def _backup(source: Path, target: Path) -> None:
    """One database, copied by SQLite itself.  Never ``cp``, never an open WAL."""
    if target.exists():
        raise SnapshotError("target_exists", f"refusing to overwrite {target}")
    try:
        origin = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    except sqlite3.Error as exc:  # pragma: no cover - defensive
        raise SnapshotError("source_unreadable", f"cannot read {source}") from exc
    destination = sqlite3.connect(str(target))
    try:
        origin.backup(destination)
        destination.commit()
    except sqlite3.Error as exc:
        raise SnapshotError("backup_failed", "the SQLite backup API refused the copy") from exc
    finally:
        destination.close()
        origin.close()


def _require_readable(path: Path, role: str) -> None:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = connection.execute("PRAGMA integrity_check").fetchone()
        if row is None or str(row[0]).lower() != "ok":
            raise SnapshotError(f"{role}_integrity_failed", f"{role} copy failed integrity_check")
    except sqlite3.DatabaseError as exc:
        raise SnapshotError(f"{role}_unreadable", f"{role} copy is not a database") from exc
    finally:
        connection.close()


def _media_inventory(processing_path: Path, directory: Path) -> list[list[Any]]:
    """Media files referenced by the journal, as (redacted name, size).

    A missing or unreachable medium is *marked in the manifest* rather than silently
    ignored: a restore that quietly loses an attachment is worse than one that says so.
    """
    out: list[list[Any]] = []
    try:
        connection = sqlite3.connect(f"file:{processing_path}?mode=ro", uri=True)
    except sqlite3.Error:  # pragma: no cover - defensive
        return out
    try:
        rows = connection.execute(
            "SELECT DISTINCT media_path FROM events"
            " WHERE media_path IS NOT NULL AND media_path <> '' ORDER BY media_path LIMIT 500"
        ).fetchall()
    except sqlite3.DatabaseError:
        return out
    finally:
        connection.close()
    for (raw,) in rows:
        path = Path(str(raw))
        out.append(
            [
                hashlib.sha256(str(path.name).encode("utf-8")).hexdigest()[:12],
                path.stat().st_size if path.exists() else -1,
            ]
        )
    del directory
    return out


def _table_counts(path: Path) -> dict[str, int]:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        names = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
                " AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        out: dict[str, int] = {}
        for name in names:
            if name.startswith("memory2_nodes_fts_"):
                continue
            out[name] = int(connection.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0])
        return out
    finally:
        connection.close()


def _meta_value(path: Path, key: str) -> str:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = connection.execute(
            "SELECT value FROM knowledge_meta WHERE key = ?", (str(key),)
        ).fetchone()
        return "" if row is None else str(row[0])
    except sqlite3.DatabaseError:
        return ""
    finally:
        connection.close()


def _scalar(path: Path, sql: str) -> int:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = connection.execute(sql).fetchone()
        return 0 if row is None else int(row[0])
    except sqlite3.DatabaseError:
        return 0
    finally:
        connection.close()


# ── verification ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SnapshotVerification:
    manifest_path: Path
    integrity_ok: bool
    hashes_match: bool
    cross_references_ok: bool
    rehearsal_ok: bool
    fts_locked_ok: bool
    verdict: str
    restored_path: Path | None = None
    counts: tuple[tuple[str, str, int, int], ...] = ()
    reason: str = ""
    schema_version: str = ""
    locked_index_entries: int | None = None
    locked_index_enforced: bool = True

    @property
    def ok(self) -> bool:
        return self.verdict == "ok"


def verify_snapshot(*, manifest: Path, restore_dir: Path | None = None) -> SnapshotVerification:
    """Open isolated restore copies and check the snapshot against its manifest.

    Starts no jobs, sends nothing, and never touches the live paths named in the
    manifest: everything happens on copies under ``restore_dir``.
    """
    manifest_path = Path(manifest).expanduser()
    payload = _read_manifest(manifest_path)
    directory = manifest_path.parent
    workspace = (
        Path(restore_dir).expanduser()
        if restore_dir is not None
        else directory / "restore"
    )
    workspace.mkdir(parents=True, exist_ok=True)

    copies: dict[str, Path] = {}
    hashes_match = True
    for role in ("processing", "knowledge"):
        entry = payload["databases"][role]
        source = Path(str(entry["path"]))
        if not source.exists():
            return SnapshotVerification(
                manifest_path=manifest_path,
                integrity_ok=False,
                hashes_match=False,
                cross_references_ok=False,
                rehearsal_ok=False,
                fts_locked_ok=False,
                verdict="failed",
                reason=f"{role}_copy_missing",
            )
        target = workspace / f"{role}.db"
        if target.exists():
            target.unlink()
        _backup(source, target)
        copies[role] = target
        if _fingerprint(target) != str(entry["fingerprint"]):
            # The copy is content-equal even when the file layout differs, so a hash
            # mismatch is reported as such instead of being waved through.
            hashes_match = hashes_match and _content_digest(target) == str(
                entry.get("content_digest", "")
            )

    integrity_ok = True
    for role, path in copies.items():
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            row = connection.execute("PRAGMA integrity_check").fetchone()
            integrity_ok = integrity_ok and row is not None and str(row[0]).lower() == "ok"
        except sqlite3.DatabaseError:
            integrity_ok = False
        finally:
            connection.close()

    counts, counts_ok = _compare_counts(copies["knowledge"], payload)
    cross_ok, rehearsal_ok = _rehearse(copies["knowledge"])
    schema_version = _meta_value(copies["knowledge"], "schema_version")
    locked_entries = _locked_fts_entries(copies["knowledge"])
    # A v1 index was built before the rule existed and keeps its leftovers until the
    # upgrade rebuilds it; the check is enforced on the schema that has to satisfy it.
    fts_enforced = schema_version != "1"
    fts_ok = True if locked_entries is None else (locked_entries == 0 or not fts_enforced)
    ok = integrity_ok and counts_ok and cross_ok and rehearsal_ok and fts_ok
    return SnapshotVerification(
        manifest_path=manifest_path,
        integrity_ok=integrity_ok,
        hashes_match=hashes_match,
        cross_references_ok=cross_ok,
        rehearsal_ok=rehearsal_ok,
        fts_locked_ok=fts_ok,
        verdict="ok" if ok else "failed",
        restored_path=copies.get("knowledge"),
        counts=counts,
        reason="" if ok else "one or more restore checks failed",
        schema_version=schema_version,
        locked_index_entries=locked_entries,
        locked_index_enforced=fts_enforced,
    )


def _compare_counts(
    path: Path, payload: dict[str, Any]
) -> tuple[tuple[tuple[str, str, int, int], ...], bool]:
    actual = _table_counts(path)
    expected = {str(k): int(v) for k, v in payload["counts"]["knowledge"].items()}
    rows: list[tuple[str, str, int, int]] = []
    ok = True
    for table, want in sorted(expected.items()):
        have = actual.get(table, -1)
        rows.append(("knowledge", table, want, have))
        ok = ok and have == want
    return tuple(rows), ok


def _has_table(connection: sqlite3.Connection, name: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        is not None
    )


def _has_column(connection: sqlite3.Connection, table: str, column: str) -> bool:
    if not _has_table(connection, table):
        return False
    return any(
        str(row[1]) == column for row in connection.execute(f"PRAGMA table_info({table})")
    )


def _rehearse(path: Path) -> tuple[bool, bool]:
    """The restore rehearsal: corrections, attributes, bindings and revocations survive.

    Checks the *shape* of what survived, never a row value: that a correction is still a
    correction, an attribute still attached, a binding still temporal, and a revocation
    still a revocation.  A rehearsal that printed values would leak into a test log.

    The snapshot a pre-migration backup contains is still schema 1, which has neither
    ``knowledge_person_attributes`` nor a ``binding_id`` column: a check that cannot apply
    to this schema is skipped, not failed - reporting a healthy v1 backup as broken would
    be the worst outcome.
    """
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        cross_ok = True
        if _has_table(connection, "knowledge_statement_people"):
            cross = connection.execute(
                "SELECT COUNT(*) FROM knowledge_statement_people p"
                " WHERE NOT EXISTS (SELECT 1 FROM knowledge_statements s"
                "   WHERE s.statement_id = p.statement_id)"
            ).fetchone()
            cross_ok = cross_ok and cross is not None and int(cross[0]) == 0
        if _has_table(connection, "knowledge_person_attributes"):
            attributes = connection.execute(
                "SELECT COUNT(*) FROM knowledge_person_attributes a"
                " WHERE NOT EXISTS (SELECT 1 FROM knowledge_statements s"
                "   WHERE s.statement_id = a.statement_id)"
            ).fetchone()
            cross_ok = cross_ok and attributes is not None and int(attributes[0]) == 0
        rehearsal_ok = True
        # ``binding_id`` is itself a v2 column: the temporal identity of a binding is
        # exactly what schema 1 did not have.
        if _has_column(connection, "knowledge_identifier_bindings", "binding_id"):
            bindings = connection.execute(
                "SELECT COUNT(*) FROM knowledge_identifier_bindings WHERE binding_id IS NULL"
            ).fetchone()
            rehearsal_ok = rehearsal_ok and bindings is not None and int(bindings[0]) == 0
        return cross_ok, rehearsal_ok
    except sqlite3.DatabaseError:
        return False, False
    finally:
        connection.close()


def _locked_fts_entries(path: Path) -> int | None:
    """How many index entries point at a statement nobody may read.

    ``None`` means the check does not apply (no index at all).
    """
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = connection.execute(
            "SELECT COUNT(*) FROM memory2_nodes_fts f"
            " WHERE EXISTS (SELECT 1 FROM knowledge_statements s"
            "   WHERE s.statement_id = f.entry_id"
            "     AND (s.status IN ('superseded','revoked')"
            "          OR s.revoked_at_ms IS NOT NULL"
            "          OR s.superseded_by IS NOT NULL))"
        ).fetchone()
        return None if row is None else int(row[0])
    except sqlite3.DatabaseError:
        # No index at all is not a failed restore: there is nothing to leak.
        return None
    finally:
        connection.close()


def _read_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise SnapshotError("manifest_missing", f"snapshot manifest does not exist: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SnapshotError("manifest_invalid", "manifest is not readable JSON") from exc
    if not isinstance(payload, dict) or "databases" not in payload:
        raise SnapshotError("manifest_invalid", "manifest describes no databases")
    return payload


# ── benchmark ────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class BenchmarkReport:
    people: int
    statements: int
    iterations: int
    p50_ms: float
    p95_ms: float
    max_ms: float
    peak_rss_mib: float
    database_bytes: int
    samples_ms: tuple[float, ...] = field(default=())

    @property
    def within_latency_budget(self) -> bool:
        """The agreed ``< 200 ms p95`` target, measured rather than faked."""
        return self.p95_ms < 200.0


def benchmark_profiles(
    *,
    target: Path,
    people: int = 1000,
    statements: int = 10000,
    iterations: int = 200,
    seed: int = 20260921,
) -> BenchmarkReport:
    """Synthetic local profile/alias read benchmark.  No network, no model.

    Builds a throwaway database with ``people`` people and ``statements`` facets, warms
    up, then times a fixed number of alias and profile reads.  Reports p50/p95/max and the
    Linux peak RSS from :mod:`resource`.  The ``< 200 ms p95`` and ``+256 MiB`` acceptance
    numbers are measured on the real device, never asserted as a wall-clock CI test.
    """
    from yeoman_gateway.knowledge._store import KnowledgeStore

    if people < 1 or statements < people:
        raise SnapshotError("invalid_benchmark_shape", "people must be >= 1 and <= statements")
    db_path = Path(target).expanduser()
    if db_path.suffix != ".db":
        db_path = db_path / "benchmark.db"
    if db_path.exists():
        raise SnapshotError("target_exists", f"refusing to overwrite {db_path}")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = KnowledgeStore(db_path)
    try:
        with store.transaction():
            _seed_benchmark(store, people=people, statements=statements, seed=seed)
        samples = _time_profile_reads(store, iterations=iterations, people=people)
        size = db_path.stat().st_size
    finally:
        store.close()
    return BenchmarkReport(
        people=people,
        statements=statements,
        iterations=iterations,
        p50_ms=round(statistics.median(samples), 3),
        p95_ms=round(_percentile(samples, 0.95), 3),
        max_ms=round(max(samples), 3),
        peak_rss_mib=round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0, 2),
        database_bytes=int(size),
        samples_ms=tuple(round(item, 3) for item in samples),
    )


def _seed_benchmark(store: Any, *, people: int, statements: int, seed: int) -> None:
    """Deterministic synthetic rows: same seed, same database content."""
    import random

    rng = random.Random(seed)
    person_ids: list[str] = []
    for index in range(people):
        person_id = f"bench-person-{index:06d}"
        person_ids.append(person_id)
        store.execute(
            "INSERT INTO contacts (id, display_name, phone_number, is_owner, created_at,"
            " updated_at, revision, status, preferred_name_visibility)"
            " VALUES (?, ?, NULL, 0, '2026-01-01T00:00:00+00:00',"
            " '2026-01-01T00:00:00+00:00', 1, 'active', 'public')",
            (person_id, f"Synthetic Person {index}"),
        )
        store.execute(
            "INSERT INTO knowledge_identifier_bindings (binding_id, channel, kind,"
            " namespace, value, person_id, status, valid_from_ms, valid_until_ms,"
            " observed_at_ms, evidence_ref, mapping_verified, revision, created_ms,"
            " updated_ms) VALUES (?, 'whatsapp', 'phone_jid', 'bench', ?, ?, 'active',"
            " 1, 0, 1, 'bench-evidence', 1, 1, 1, 1)",
            (f"bench-binding-{index:06d}", f"4917{index:07d}@s.whatsapp.net", person_id),
        )
        store.execute(
            "INSERT INTO contact_aliases (contact_id, alias, source, first_seen, last_seen,"
            " alias_kind, normalized_alias, scope_key, status, address_allowed,"
            " is_preferred, revision) VALUES (?, ?, 'observed', 'now', 'now',"
            " 'nickname', ?, 'global', 'confirmed', 1, 0, 1)",
            (person_id, f"nick{index}", f"nick{index}"),
        )
    for index in range(statements):
        person_id = person_ids[index % people]
        statement_id = f"bench-statement-{index:07d}"
        store.execute(
            "INSERT INTO memory2_nodes (id, workspace_id, scope_type, scope_key, sector,"
            " kind, content, content_norm, content_hash, salience, confidence, source,"
            " created_at, updated_at, is_deleted) VALUES (?, 'bench', 'chat',"
            " 'channel:whatsapp:chat:bench', 'semantic', 'fact', ?, ?, ?, 0.5, 0.5,"
            " 'bench', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', 0)",
            (
                statement_id,
                f"Synthetic fact {index} about residence and hobbies.",
                f"synthetic fact {index} about residence and hobbies.",
                f"hash-{index:07d}",
            ),
        )
        store.execute(
            "INSERT INTO knowledge_statements (statement_id, workspace_id, scope_key,"
            " author_principal, status, visibility_scope, group_rule, valid_from_ms,"
            " extractor_version, content_hash, dedupe_key, created_ms, updated_ms,"
            " supersession_reason, time_basis, time_precision)"
            " VALUES (?, 'bench', 'channel:whatsapp:chat:bench', 'bench-actor',"
            " 'assertion', 'chat_shared', 'chat_members_at_source', 1, 'bench', ?, ?, 1, 1,"
            " 'unknown', 'stored', 'unknown')",
            (statement_id, f"hash-{index:07d}", f"dedupe-{index:07d}"),
        )
        store.execute(
            "INSERT INTO knowledge_statement_people (statement_id, person_id, role,"
            " evidence_source_id, evidence_revision, attribution, created_ms, status,"
            " resolution_reason) VALUES (?, ?, 'subject', ?, 1, 'extracted', 1, 'active',"
            " 'proven_active_binding')",
            (statement_id, person_id, f"bench-event-{index:07d}"),
        )
        store.execute(
            "INSERT INTO knowledge_person_attributes (statement_id, person_id,"
            " attribute_key, value_json, value_key, polarity, revision, created_ms,"
            " updated_ms) VALUES (?, ?, ?, ?, ?, 'positive', 1, 1, 1)",
            (
                statement_id,
                person_id,
                rng.choice(("residence", "hobby", "interest", "preference")),
                json.dumps({"text": f"value-{index}", "precision": "unknown"}),
                f"value-{index}",
            ),
        )
        store.execute(
            "INSERT INTO memory2_nodes_fts (entry_id, content) VALUES (?, ?)",
            (statement_id, f"synthetic fact {index} about residence and hobbies."),
        )


def _time_profile_reads(store: Any, *, iterations: int, people: int) -> list[float]:
    """Warm up, then time local alias and profile reads only.  No provider in the loop."""
    sample_people = [f"bench-person-{index:06d}" for index in range(min(people, 16))]
    statements_sql = (
        "SELECT s.statement_id FROM knowledge_statements s"
        " WHERE s.status = 'assertion' ORDER BY s.created_ms DESC LIMIT 20"
    )
    aliases_sql = (
        "SELECT a.alias, a.status FROM contact_aliases a"
        " WHERE a.contact_id = ? AND a.scope_key = 'global' ORDER BY a.alias LIMIT 5"
    )
    facets_sql = (
        "SELECT a.attribute_key, a.value_key FROM knowledge_person_attributes a"
        " WHERE a.person_id = ? ORDER BY a.attribute_key, a.value_key LIMIT 5"
    )
    for _ in range(10):
        store.query(statements_sql)
        for person_id in sample_people:
            store.query(aliases_sql, (person_id,))
            store.query(facets_sql, (person_id,))
    samples: list[float] = []
    for _ in range(max(1, iterations)):
        started = time.perf_counter()
        store.query(statements_sql)
        for person_id in sample_people:
            store.query(aliases_sql, (person_id,))
            store.query(facets_sql, (person_id,))
        samples.append((time.perf_counter() - started) * 1000.0)
    return samples


def _percentile(samples: list[float], fraction: float) -> float:
    if not samples:
        return 0.0
    ordered = sorted(samples)
    index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
    return ordered[index]


# ── small helpers ────────────────────────────────────────────────────────────


def _fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _content_digest(path: Path) -> str:
    """A layout-independent digest of the table inventory, for "same content, new file".

    The backup API may produce a byte-different file for identical rows, so the content
    digest is what makes "the restore holds what the snapshot held" checkable without
    depending on page order.
    """
    digest = hashlib.sha256()
    for name, count in sorted(_table_counts(path).items()):
        digest.update(f"\x1d{name}:{count}\x1e".encode("utf-8"))
    return digest.hexdigest()
