"""Offline CLI for the person-knowledge migration.

The surface is exactly::

    yeoman knowledge migration inspect     --contacts SNAPSHOT --memory SNAPSHOT
    yeoman knowledge migration build       --contacts SNAPSHOT --memory SNAPSHOT \\
                                           --target NEW_DB --manifest NEW_JSON
    yeoman knowledge migration verify      --target DB --manifest JSON
    yeoman knowledge migration inspect-v1  --source V1.db --processing PROCESSING.db
    yeoman knowledge migration upgrade-v1  --source V1.db --processing PROCESSING.db \\
                                           --target NEW-V2.db --manifest NEW.json
    yeoman knowledge migration verify-v1   --target NEW-V2.db --manifest NEW.json
    yeoman knowledge migration propose-bindings --source V1.db --processing PROCESSING.db \
                                           --out proposals.json
    yeoman knowledge migration upgrade-v1  ... --binding-approvals proposals.json

Everything here is offline and explicit: no default paths, no provider or bootstrap
startup, no implicit migration, no gateway and no worker.  Every failure exits non-zero
and prints a stable reason code (``source_error``, ``target_exists``,
``unsupported_schema``, ``manifest_mismatch``); diagnostics are redacted to table names,
counts and ids.

Registration follows the existing convention: this module imports the shared ``app``
and attaches its sub-app at import time, and ``cli/commands.py`` imports the module
next to the other command modules.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final, NoReturn

import typer
from rich.table import Table
from rich.text import Text

from yeoman_gateway.knowledge._migration import (
    MigrationInventory,
    MigrationReport,
    MigrationSourceError,
    UnsupportedSchema,
    VerificationReport,
    inspect_sources,
    migrate_sources,
    verify_target,
)
from yeoman_gateway.knowledge._upgrade import (
    UpgradeError,
    UpgradeInventory,
    UpgradeReport,
    UpgradeVerification,
    inspect_v1,
    upgrade_v1,
    verify_upgrade,
)

from .core import app, console

knowledge_app = typer.Typer(help="Person knowledge: offline migration inventory and build")
app.add_typer(knowledge_app, name="knowledge")
migration_app = typer.Typer(help="Inspect, build and verify offline legacy snapshots")
knowledge_app.add_typer(migration_app, name="migration")
capture_app = typer.Typer(help="Statement promotion: read-only status")
knowledge_app.add_typer(capture_app, name="capture")

_FAILURE_EXIT: Final[int] = 2

#: Internal reason -> stable CLI reason code.  Everything else is a source problem.
_REASON_CODES: Final[dict[str, str]] = {
    "unsupported_schema": "unsupported_schema",
    "unsupported_source_objects": "unsupported_schema",
    "table_shape_unknown": "unsupported_schema",
    "target_exists": "target_exists",
    "target_is_source": "target_exists",
    "target_is_manifest": "target_exists",
    "manifest_exists": "target_exists",
    "manifest_missing": "manifest_mismatch",
    "manifest_invalid": "manifest_mismatch",
    "not_a_database": "source_error",
}

#: v1->v2 upgrade reason codes.  Kept separate so the two offline paths cannot be
#: confused by a shared code with a different meaning.
_UPGRADE_REASON_CODES: Final[dict[str, str]] = {
    "knowledge_missing": "source_error",
    "knowledge_not_a_file": "source_error",
    "knowledge_unreadable": "source_error",
    "knowledge_not_a_database": "source_error",
    "knowledge_integrity_failed": "source_error",
    "processing_missing": "source_error",
    "processing_not_a_file": "source_error",
    "processing_unreadable": "source_error",
    "processing_not_a_database": "source_error",
    "processing_integrity_failed": "source_error",
    "unsupported_source_schema": "unsupported_schema",
    "table_shape_unknown": "unsupported_schema",
    "target_is_source": "target_exists",
    "target_is_manifest": "target_exists",
    "target_exists": "target_exists",
    "target_is_a_source": "target_exists",
    "manifest_exists": "target_exists",
    "manifest_missing": "manifest_mismatch",
    "manifest_invalid": "manifest_mismatch",
    "target_missing": "manifest_mismatch",
    "target_not_a_database": "manifest_mismatch",
    "binding_overlap": "semantics_error",
    "staged_integrity_failed": "semantics_error",
    "staged_foreign_key_failed": "semantics_error",
    "statement_count_mismatch": "semantics_error",
    "role_count_mismatch": "semantics_error",
    "source_count_mismatch": "semantics_error",
    "binding_balance_broken": "semantics_error",
    "orphan_source_rows": "semantics_error",
    "injected_failure": "semantics_error",
    # An approval that does not match the snapshot is a decision error, not a source error.
    "approval_file_missing": "approval_error",
    "approval_file_invalid": "approval_error",
    "approval_unknown_identifier": "approval_error",
    "approval_unknown_person": "approval_error",
    "approval_mismatch": "approval_error",
    "approval_duplicate": "approval_error",
    "approval_overlaps_existing_binding": "approval_error",
    "approval_not_applied": "semantics_error",
}


# ── commands ─────────────────────────────────────────────────────────────────


@migration_app.command("inspect")
def migration_inspect(
    contacts: Path = typer.Option(..., "--contacts", help="Legacy contacts snapshot"),
    memory: Path = typer.Option(..., "--memory", help="Legacy memory snapshot"),
) -> None:
    """Read both snapshots read-only and print a redacted inventory."""
    try:
        inventory = inspect_sources(contacts, memory)
    except UnsupportedSchema as exc:  # pragma: no cover - inspect reports, never raises
        _fail("unsupported_schema", exc.detail, exc.reason)
    except MigrationSourceError as exc:
        _fail(_reason_code(exc.reason), exc.detail, exc.reason)
    _print_inventory(inventory)


@migration_app.command("build")
def migration_build(
    contacts: Path = typer.Option(..., "--contacts", help="Legacy contacts snapshot"),
    memory: Path = typer.Option(..., "--memory", help="Legacy memory snapshot"),
    target: Path = typer.Option(..., "--target", help="New knowledge database to create"),
    manifest: Path = typer.Option(..., "--manifest", help="New manifest JSON to create"),
) -> None:
    """Copy both snapshots into a fresh target database and write its manifest."""
    try:
        report = migrate_sources(
            contacts_path=contacts,
            memory_path=memory,
            target=target,
            manifest=manifest,
        )
    except UnsupportedSchema as exc:
        _fail("unsupported_schema", exc.detail, exc.reason)
    except MigrationSourceError as exc:
        _fail(_reason_code(exc.reason), exc.detail, exc.reason)
    _print_report(report)


@migration_app.command("verify")
def migration_verify(
    target: Path = typer.Option(..., "--target", help="Built knowledge database"),
    manifest: Path = typer.Option(..., "--manifest", help="Manifest written by build"),
) -> None:
    """Re-read the target read-only and compare it with the manifest."""
    try:
        report = verify_target(target=target, manifest=manifest)
    except MigrationSourceError as exc:
        _fail(_reason_code(exc.reason), exc.detail, exc.reason)
    _print_verification(report)
    if report.verdict != "ok":
        detail = ", ".join(
            f"{table} expected {expected} rows, found {actual}"
            for table, expected, actual in report.mismatches
        )
        if not detail:
            if not report.complete:
                # The database itself says it is not a finished migration, whatever the
                # external manifest claims.  This is the crash-recovery signal.
                detail = (
                    "target carries no complete migration marker; rebuild it from the"
                    " snapshots instead of using it"
                )
            elif not report.digest_ok:
                detail = "target content does not match the manifest digest"
            else:
                detail = "integrity, foreign keys or fingerprint check failed"
        _fail("manifest_mismatch", detail)


# ── v1 -> v2 snapshot upgrade ────────────────────────────────────────────────


@migration_app.command("inspect-v1")
def migration_inspect_v1(
    source: Path = typer.Option(..., "--source", help="Existing v1 knowledge snapshot"),
    processing: Path = typer.Option(..., "--processing", help="Processing journal snapshot"),
) -> None:
    """Read a v1 knowledge snapshot and its processing journal, read-only."""
    try:
        inventory = inspect_v1(source=source, processing=processing)
    except UpgradeError as exc:
        _fail(_upgrade_reason_code(exc.code), exc.message, exc.code)
    _print_upgrade_inventory(inventory)
    if inventory.verdict != "ok":
        _fail("unsupported_schema", inventory.reason)


@migration_app.command("propose-bindings")
def migration_propose_bindings(
    source: Path = typer.Option(..., "--source", help="Existing v1 knowledge snapshot"),
    processing: Path = typer.Option(..., "--processing", help="Processing journal snapshot"),
    out: Path = typer.Option(..., "--out", help="New proposal file for the owner to review"),
    namespace: str = typer.Option(
        "",
        "--namespace",
        help="Force one platform-account namespace instead of the observed per-channel one",
    ),
) -> None:
    """Write a reviewable, private proposal for every legacy identifier (read-only)."""
    from yeoman_gateway.knowledge._upgrade import propose_bindings

    try:
        report = propose_bindings(
            source=source, processing=processing, out=out, namespace=namespace
        )
    except UpgradeError as exc:
        _fail(_upgrade_reason_code(exc.code), exc.message, exc.code)
    _line(f"proposal: {report.out_path}  (private: it names people and identifiers)")
    _line(f"entries: {report.total}  with journal evidence: {report.with_journal_evidence}")
    if report.not_a_person:
        _line(
            f"not a person (group/broadcast JID, never proposed): {report.not_a_person}"
        )
    _line(f"stored role rows covered: {report.role_rows_covered}")
    _line("nothing was applied: mark entries as approved and pass the file to upgrade-v1")


@migration_app.command("upgrade-v1")
def migration_upgrade_v1(
    source: Path = typer.Option(..., "--source", help="Existing v1 knowledge snapshot"),
    processing: Path = typer.Option(..., "--processing", help="Processing journal snapshot"),
    target: Path = typer.Option(..., "--target", help="New v2 knowledge database to create"),
    manifest: Path = typer.Option(..., "--manifest", help="New upgrade manifest JSON"),
    binding_approvals: Path | None = typer.Option(
        None,
        "--binding-approvals",
        help="Owner-reviewed proposal file; only entries marked approved are applied",
    ),
) -> None:
    """Build a fresh v2 target from a v1 snapshot.  Never migrates in place."""
    try:
        report = upgrade_v1(
            source=source,
            processing=processing,
            target=target,
            manifest=manifest,
            binding_approvals=binding_approvals,
        )
    except UpgradeError as exc:
        _fail(_upgrade_reason_code(exc.code), exc.message, exc.code)
    _print_upgrade_report(report)


@migration_app.command("verify-v1")
def migration_verify_v1(
    target: Path = typer.Option(..., "--target", help="Upgraded v2 database"),
    manifest: Path = typer.Option(..., "--manifest", help="Manifest written by upgrade-v1"),
) -> None:
    """Re-read an upgraded target read-only and compare it with its manifest."""
    try:
        report = verify_upgrade(target=target, manifest=manifest)
    except UpgradeError as exc:
        _fail(_upgrade_reason_code(exc.code), exc.message, exc.code)
    _print_upgrade_verification(report)
    if report.verdict != "ok":
        detail = ", ".join(
            f"{table} expected {expected} rows, found {actual}"
            for table, expected, actual in report.mismatches
        )
        if not detail:
            if not report.complete:
                detail = (
                    "target carries no complete upgrade marker; rebuild it from the"
                    " v1 snapshot instead of using it"
                )
            elif not report.digest_ok:
                detail = "target content does not match the manifest digest"
            elif not report.balance_ok:
                detail = "the cutover balance does not explain every stored row"
            else:
                detail = "integrity, foreign keys or fingerprint check failed"
        _fail("manifest_mismatch", detail)


# ── offline snapshot and benchmark ───────────────────────────────────────────

snapshot_app = typer.Typer(help="Offline snapshot: copy, verify, benchmark")
knowledge_app.add_typer(snapshot_app, name="snapshot")


@snapshot_app.command("create")
def snapshot_create(
    processing: Path = typer.Option(..., "--processing", help="Processing journal database"),
    knowledge: Path = typer.Option(..., "--knowledge", help="Knowledge database"),
    target_dir: Path = typer.Option(..., "--target-dir", help="New directory for the copy"),
    quiesce_ref: Path = typer.Option(
        ..., "--quiesce-ref", help="Reference to the authorized quiesce boundary"
    ),
) -> None:
    """Copy both databases with the SQLite backup API, then describe the result."""
    from yeoman_gateway.knowledge._snapshot import SnapshotError, create_snapshot

    try:
        report = create_snapshot(
            processing=processing,
            knowledge=knowledge,
            target_dir=target_dir,
            quiesce_ref=str(quiesce_ref),
        )
    except SnapshotError as exc:
        _fail("source_error", exc.message, exc.code)
    _line(f"snapshot: {report.target_dir}")
    _line(f"manifest: {report.manifest_path}")
    _line(f"quiesce ref: {report.quiesce_ref}  (coherent live boundary: no)")
    _line(
        "knowledge: "
        f"{report.knowledge_path.name} sha256:{report.knowledge_fingerprint[:12]}"
        f" rows={sum(count for _table, count in report.knowledge_counts)}"
    )
    _line(
        "processing: "
        f"{report.processing_path.name} sha256:{report.processing_fingerprint[:12]}"
        f" rows={sum(count for _table, count in report.processing_counts)}"
    )
    _line(f"media entries: {len(report.media)}")


@snapshot_app.command("verify")
def snapshot_verify(
    manifest: Path = typer.Option(..., "--manifest", help="Manifest written by create"),
    restore_dir: Path = typer.Option(
        None, "--restore-dir", help="Directory for the isolated restore copies"
    ),
) -> None:
    """Verify a snapshot on isolated restore copies.  Starts no jobs and sends nothing."""
    from yeoman_gateway.knowledge._snapshot import SnapshotError, verify_snapshot

    try:
        report = verify_snapshot(manifest=manifest, restore_dir=restore_dir)
    except SnapshotError as exc:
        _fail("manifest_mismatch", exc.message, exc.code)
    _line(f"integrity_check: {'ok' if report.integrity_ok else 'failed'}")
    _line(f"manifest hashes: {'match' if report.hashes_match else 'differ'}")
    _line(f"cross-references: {'ok' if report.cross_references_ok else 'broken'}")
    _line(f"restore rehearsal: {'ok' if report.rehearsal_ok else 'failed'}")
    _line(f"locked index entries: {'none' if report.fts_locked_ok else 'present'}")
    _line(f"verdict: {report.verdict}", style="green" if report.ok else "red")
    if not report.ok:
        _fail("manifest_mismatch", report.reason)


@knowledge_app.command("benchmark")
def knowledge_benchmark(
    target: Path = typer.Option(..., "--target", help="Scratch database path to use"),
    people: int = typer.Option(1000, "--people", help="Synthetic people to build"),
    statements: int = typer.Option(10000, "--statements", help="Synthetic facets to build"),
    iterations: int = typer.Option(200, "--iterations", help="Timed read rounds"),
) -> None:
    """Measure local profile/alias reads on synthetic data.  No network, no model."""
    from yeoman_gateway.knowledge._snapshot import SnapshotError, benchmark_profiles

    try:
        report = benchmark_profiles(
            target=target, people=people, statements=statements, iterations=iterations
        )
    except SnapshotError as exc:
        _fail("source_error", exc.message, exc.code)
    _line(f"people={report.people} statements={report.statements} iterations={report.iterations}")
    _line(f"reads p50={report.p50_ms} ms  p95={report.p95_ms} ms  max={report.max_ms} ms")
    _line(f"peak RSS: {report.peak_rss_mib} MiB")
    _line(f"database bytes: {report.database_bytes}")
    _line(
        "p95 budget (<200 ms): "
        + ("met" if report.within_latency_budget else "exceeded - report as a deviation")
    )


# ── read-only person inspection ──────────────────────────────────────────────
#
# These commands open one explicitly named database read-only.  They never start a
# gateway, never open the live config, never write, and never print row content: counts,
# ids, statuses and reason codes only.  Content inspection stays behind the authorized
# runtime paths, because a CLI is not an authorization.


def _open_readonly_connection(target: Path):
    import sqlite3

    path = Path(target).expanduser()
    if not path.exists():
        _fail("source_error", f"database does not exist: {path}")
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except Exception:  # pragma: no cover - defensive
        _fail("source_error", f"not a readable SQLite database: {path}")
    connection.row_factory = sqlite3.Row
    return connection


def _redacted(value: object) -> str:
    """A stable, content-free token for a name or identifier."""
    import hashlib

    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]


@knowledge_app.command("inspect-bindings")
def knowledge_inspect_bindings(
    target: Path = typer.Option(..., "--target", help="Knowledge database to read"),
    status: str = typer.Option("", "--status", help="Only bindings in this status"),
) -> None:
    """List identifier bindings with their cutover status.  Values stay redacted."""
    connection = _open_readonly_connection(target)
    try:
        sql = (
            "SELECT binding_id, channel, kind, namespace, value, person_id, status,"
            " mapping_verified, valid_from_ms, valid_until_ms, evidence_ref"
            " FROM knowledge_identifier_bindings"
        )
        params: tuple = ()
        if status:
            sql += " WHERE status = ?"
            params = (str(status),)
        sql += " ORDER BY channel, kind, namespace, value, binding_id"
        rows = connection.execute(sql, params).fetchall()
    except Exception:
        connection.close()
        _fail("source_error", "the target carries no v2 binding table")
    finally:
        pass
    _line("identifier bindings (values and person ids redacted)")
    for row in rows:
        _line(
            f"  {row['channel']}/{row['kind']}/{row['namespace']}"
            f"  value={_redacted(row['value'])}"
            f"  person={_redacted(row['person_id'])}"
            f"  status={row['status']}"
            f"  verified={'yes' if int(row['mapping_verified'] or 0) else 'no'}"
            f"  valid={int(row['valid_from_ms'] or 0)}..{int(row['valid_until_ms'] or 0)}"
            f"  evidence={_redacted(row['evidence_ref'])}"
        )
    counts: dict[str, int] = {}
    for row in rows:
        counts[str(row["status"])] = counts.get(str(row["status"]), 0) + 1
    _line("counts: " + "  ".join(f"{key}={value}" for key, value in sorted(counts.items())))
    connection.close()


@knowledge_app.command("inspect-roles")
def knowledge_inspect_roles(
    target: Path = typer.Option(..., "--target", help="Knowledge database to read"),
    status: str = typer.Option("", "--status", help="Only roles in this status"),
) -> None:
    """List stored person roles with their cutover verdict.  No names, no text."""
    connection = _open_readonly_connection(target)
    try:
        sql = (
            "SELECT role, status, resolution_reason, COUNT(*) AS n"
            " FROM knowledge_statement_people"
        )
        params: tuple = ()
        if status:
            sql += " WHERE status = ?"
            params = (str(status),)
        sql += " GROUP BY role, status, resolution_reason ORDER BY role, status"
        rows = connection.execute(sql, params).fetchall()
    except Exception:
        connection.close()
        _fail("source_error", "the target carries no v2 role table")
    _line("person roles (counts only)")
    for row in rows:
        _line(
            f"  {row['role']}/{row['status']}"
            f"  reason={row['resolution_reason'] or '-'}"
            f"  count={int(row['n'])}"
        )
    connection.close()


@knowledge_app.command("inspect-unresolved")
def knowledge_inspect_unresolved(
    target: Path = typer.Option(..., "--target", help="Knowledge database to read"),
    limit: int = typer.Option(50, "--limit", help="Maximum rows to print"),
) -> None:
    """List quarantined and withheld cases: what could not be proven, and why."""
    connection = _open_readonly_connection(target)
    try:
        quarantine = connection.execute(
            "SELECT source_table, reason, COUNT(*) AS n FROM knowledge_quarantine"
            " GROUP BY source_table, reason ORDER BY source_table, reason"
        ).fetchall()
        withheld = connection.execute(
            "SELECT resolution_reason, COUNT(*) AS n FROM knowledge_statement_people"
            " WHERE status <> 'active' GROUP BY resolution_reason ORDER BY resolution_reason"
            " LIMIT ?",
            (int(max(1, limit)),),
        ).fetchall()
    except Exception:
        connection.close()
        _fail("source_error", "the target carries no v2 case tables")
    _line("unresolved and quarantined cases (counts only)")
    for row in quarantine:
        _line(f"  {row['source_table']}  reason={row['reason']}  count={int(row['n'])}")
    for row in withheld:
        _line(
            f"  knowledge_statement_people  reason={row['resolution_reason'] or '-'}"
            f"  count={int(row['n'])}"
        )
    connection.close()


@capture_app.command("status")
def capture_status(    target: Path = typer.Option(
        Path("~/.yeoman/data/knowledge/knowledge.db"),
        "--target",
        help="Knowledge database to read",
    ),
) -> None:
    """Read-only promotion counters: job states, refusal reasons, oldest wait.

    No statement content is printed, and nothing is written: the command opens the store
    read-only and reports what the promotion worker left behind.
    """
    from yeoman_gateway.knowledge._statements import capture_status as read_capture_status
    from yeoman_gateway.knowledge._store import KnowledgeStore

    path = target.expanduser()
    if not path.exists():
        _fail("source_error", f"knowledge database not found: {path}")
    store = KnowledgeStore(path, create=False)
    try:
        counters = read_capture_status(store, now_ms=store.now_ms())
    finally:
        store.close()
    table = Table(title="statement capture status", show_edge=False)
    table.add_column("state")
    table.add_column("jobs", justify="right")
    for state in ("queued", "running", "done", "skipped", "cancelled", "failed"):
        table.add_row(state, str(counters["states"].get(state, 0)))
    console.print(table)
    if counters["reasons"]:
        reasons = Table(title="recorded reasons", show_edge=False)
        reasons.add_column("reason")
        reasons.add_column("jobs", justify="right")
        for reason, count in sorted(counters["reasons"].items()):
            reasons.add_row(reason, str(count))
        console.print(reasons)
    oldest = int(counters["oldest_queued_age_ms"])
    _line(f"oldest queued job: {oldest // 1000}s")


def _open_capture_runtime() -> tuple[Any, Any, Any]:
    """Open the live stores for a bounded capture command.

    Offline by construction: config, the canonical journal and the knowledge store, and
    nothing else - no channels, no policy engine, no responder.  Both stores stay owned by
    this process and are closed by the caller.
    """
    from yeoman_shared.config.loader import load_config

    from yeoman_gateway.app.bootstrap import _processing_store_path
    from yeoman_gateway.knowledge import open_knowledge_store, workspace_id_for
    from yeoman_gateway.knowledge.runtime import (
        RuntimeKnowledgePolicy,
        RuntimeKnowledgeSources,
    )
    from yeoman_gateway.processing.store import ProcessingStore

    config = load_config()
    processing = ProcessingStore(_processing_store_path(config))
    sources = RuntimeKnowledgeSources(processing_store=processing)
    knowledge = open_knowledge_store(
        Path(config.knowledge.db_path).expanduser(),
        workspace_id=workspace_id_for(config.workspace_path),
        source_authority=sources,
        policy_authority=RuntimeKnowledgePolicy(engine=None, policy_revision=1),
        create=False,
    )
    return config, knowledge, processing


@capture_app.command("capture-audience")
def capture_audience(
    limit: int = typer.Option(500, "--limit", help="Maximum revisions to examine"),
    apply: bool = typer.Option(
        False, "--apply", help="Write the proof; without it this is a dry run"
    ),
) -> None:
    """Register the provable audience of historic sources (dry run by default).

    A historic revision has no source-time membership snapshot, so it is registered
    ``author_only``: the author is provable, the reader list is not.  Nothing is ever
    widened to today's group members.
    """
    from yeoman_gateway.knowledge._capture import HistoricAudienceRepair

    _config, knowledge, processing = _open_capture_runtime()
    try:
        repair = HistoricAudienceRepair(knowledge=knowledge, processing=processing)
        report = repair.run(limit=int(limit), apply=bool(apply))
    finally:
        knowledge.close()
        processing.close()
    _line(
        f"{'would register' if report.dry_run else 'registered'} "
        f"{report.registered} of {report.examined} examined revision(s) "
        f"({report.created} without a projection row yet)"
    )
    for reason, count in sorted(report.refused.items()):
        _line(f"  refused {reason}: {count}")


@capture_app.command("capture-backfill")
def capture_backfill(
    limit: int = typer.Option(20, "--limit", help="Maximum new jobs per run"),
    before_ms: int = typer.Option(
        0, "--before-ms", help="Window end (default: the forward boundary)"
    ),
    scan: int = typer.Option(2000, "--scan", help="Maximum journal events scanned"),
    apply: bool = typer.Option(
        False, "--apply", help="Queue the jobs; without it this is a dry run"
    ),
) -> None:
    """Queue bounded promotion jobs for observations *before* the forward boundary.

    The forward boundary never moves, so forward capture is unaffected.  Jobs are drained
    by the running promotion worker; batching and job keys are the same as forward
    capture, so a repeated run is idempotent.
    """
    from yeoman_gateway.knowledge._capture import StatementCaptureProducer

    _config, knowledge, processing = _open_capture_runtime()
    try:
        boundary_ms, _boundary_id = knowledge.capture_boundary() or (0, "")
        end_ms = int(before_ms) if int(before_ms) > 0 else int(boundary_ms)
        if end_ms <= 0:
            _fail("source_error", "no forward boundary yet; forward capture never started")
        producer = StatementCaptureProducer(knowledge=knowledge, processing=processing)
        report = producer.run_historical(
            before_ms=end_ms,
            max_batches=int(limit),
            scan_limit=int(scan),
            apply=bool(apply),
        )
    finally:
        knowledge.close()
        processing.close()
    _line(
        f"scanned {report.examined} event(s); "
        f"{'queued' if apply else 'would queue'} {report.jobs} job(s); "
        f"{report.promoted_sources} source(s); already queued {report.already_queued}"
    )
    for reason, count in sorted(report.refusals.items()):
        _line(f"  refused {reason}: {count}")


@capture_app.command("capture-rescreen")
def capture_rescreen(
    limit: int = typer.Option(5000, "--limit", help="Maximum statements to examine"),
    apply: bool = typer.Option(
        False, "--apply", help="Hide refused statements; without it this is a dry run"
    ),
) -> None:
    """Apply the current deterministic screens to already-published statements.

    A refused statement is set to ``superseded`` (unreadable, text retained, audited).
    Sources, observations and authority records are never touched; nothing is deleted.
    """
    from yeoman_gateway.knowledge._statements import rescreen_statements

    _config, knowledge, processing = _open_capture_runtime()
    try:
        report = rescreen_statements(
            knowledge._store,  # noqa: SLF001 - offline CLI over the store owner
            apply=bool(apply),
            limit=int(limit),
        )
    finally:
        knowledge.close()
        processing.close()
    for line in report.as_lines():
        _line(line)


# ── output ───────────────────────────────────────────────────────────────────


def _print_inventory(inventory: MigrationInventory) -> None:
    for label, source in (("contacts", inventory.contacts), ("memory", inventory.memory)):
        _line(f"{label} snapshot: {source.path}")
        _line(
            f"  sha256:{source.fingerprint}"
            f"  schema_version: {source.schema_version or 'unknown'}"
            f"  tables: {len(source.tables)}"
            f"  unsupported: {len(source.unsupported)}"
            f"  identifier conflicts: {len(source.identifier_conflicts)}"
        )
        table = Table(title=Text(f"{label} tables"))
        table.add_column("table")
        table.add_column("rows", justify="right")
        for name, count in source.row_counts:
            table.add_row(name, str(count))
        console.print(table)
        if source.unsupported:
            _line(f"  unsupported objects: {', '.join(source.unsupported)}", style="yellow")
    for statement in inventory.statements:
        _line(f"- {statement}")


def _print_report(report: MigrationReport) -> None:
    table = Table(title=Text(f"imported into {report.target_path.name}"))
    table.add_column("table")
    table.add_column("source rows", justify="right")
    table.add_column("imported rows", justify="right")
    for name, source_rows, imported_rows in report.tables:
        table.add_row(name, str(source_rows), str(imported_rows))
    console.print(table)
    quarantined = sum(count for _table, _reason, count in report.quarantined)
    for name, reason, count in report.quarantined:
        _line(f"  quarantined {name}: {reason} x{count}", style="yellow")
    _line(f"target: {report.target_path}  sha256:{report.target_fingerprint}")
    _line(f"manifest: {report.manifest_path}  migration_complete=false")
    _line(
        f"imported rows: {report.imported_rows}"
        f"   quarantined rows: {quarantined}"
        f"   unaccounted rows: {report.unaccounted_rows}"
    )


def _print_verification(report: VerificationReport) -> None:
    _line(f"integrity_check: {'ok' if report.integrity_ok else 'failed'}")
    _line(f"foreign_key_check: {'ok' if report.foreign_keys_ok else 'failed'}")
    _line(
        "target fingerprint: "
        + ("matches manifest" if report.fingerprint_ok else "does not match manifest")
    )
    _line(
        f"table counts: {'match manifest' if report.counts_match else 'differ'} "
        f"({len(report.mismatches)} mismatches)"
    )
    for table, expected, actual in report.mismatches:
        _line(f"  {table}: manifest says {expected} rows, target has {actual}")
    _line(f"verdict: {report.verdict}", style="green" if report.verdict == "ok" else "red")


def _print_upgrade_inventory(inventory: UpgradeInventory) -> None:
    _line(f"knowledge snapshot: {inventory.knowledge_path}")
    _line(f"  schema version: {inventory.knowledge_schema_version or 'none'}")
    _line(f"  tables: {len(inventory.knowledge_tables)}")
    _line(f"processing snapshot: {inventory.processing_path}")
    _line(f"  tables: {len(inventory.processing_tables)}")
    table = Table(title=Text("v1 knowledge rows (redacted counts)"))
    table.add_column("table")
    table.add_column("rows", justify="right")
    for name, count in inventory.counts:
        table.add_row(name, str(count))
    console.print(table)
    _line(f"identifier conflicts: {inventory.identifier_conflicts}")
    _line(f"orphaned source references: {inventory.orphan_sources}")
    _line(f"alias collisions: {inventory.alias_collisions}")
    if inventory.unknown_objects:
        # Object *names* only: an unknown table is unknown precisely because its content
        # was never interpreted, so nothing from it is printed.
        _line(f"unknown objects: {', '.join(inventory.unknown_objects)}", style="yellow")
    if inventory.missing_required_tables:
        _line(
            "missing required tables: " + ", ".join(inventory.missing_required_tables),
            style="red",
        )
    _line(f"verdict: {inventory.verdict}", style="green" if inventory.verdict == "ok" else "red")


def _print_upgrade_report(report: UpgradeReport) -> None:
    table = Table(title=Text(f"upgraded into {report.target_path.name}"))
    table.add_column("table")
    table.add_column("imported rows", justify="right")
    for name, count in report.counts:
        table.add_row(name, str(count))
    console.print(table)
    balance = report.balance.to_payload()
    for group in ("bindings", "person_roles", "supersessions", "approvals"):
        rendered = "  ".join(f"{key}={value}" for key, value in balance[group].items())
        _line(f"{group}: {rendered}")
    _line(f"target: {report.target_path}")
    _line(f"manifest: {report.manifest_path}")
    _line(f"semantic digest: {report.semantic_digest}")


def _print_upgrade_verification(report: UpgradeVerification) -> None:
    _line(f"integrity_check: {'ok' if report.integrity_ok else 'failed'}")
    _line(f"foreign_key_check: {'ok' if report.foreign_keys_ok else 'failed'}")
    _line(
        "target fingerprint: "
        + ("matches manifest" if report.fingerprint_ok else "does not match manifest")
    )
    _line(
        f"table counts: {'match manifest' if report.counts_match else 'differ'} "
        f"({len(report.mismatches)} mismatches)"
    )
    for table, expected, actual in report.mismatches:
        _line(f"  {table}: manifest says {expected} rows, target has {actual}")
    _line(f"semantic digest: {'matches manifest' if report.digest_ok else 'differs'}")
    _line(f"cutover balance: {'explains every row' if report.balance_ok else 'incomplete'}")
    _line(
        "owner approvals: "
        + (
            f"{report.approvals_found} of {report.approvals_declared} applied"
            if report.approvals_declared
            else "none declared"
        )
        + ("" if report.approvals_ok else "  (MISMATCH)")
    )
    _line(f"verdict: {report.verdict}", style="green" if report.verdict == "ok" else "red")


def _upgrade_reason_code(code: str) -> str:
    if code in _UPGRADE_REASON_CODES:
        return _UPGRADE_REASON_CODES[code]
    if code.startswith("manifest"):
        return "manifest_mismatch"
    if code.startswith("target"):
        return "target_exists"
    return "source_error"


def _line(message: str, *, style: str = "") -> None:
    """Print one diagnostic line without wrapping or cropping (paths stay intact)."""
    console.print(Text(message, style=style or None), soft_wrap=True)


def _reason_code(reason: str) -> str:
    if reason in _REASON_CODES:
        return _REASON_CODES[reason]
    if reason.startswith("manifest"):
        return "manifest_mismatch"
    if reason.startswith("target"):
        return "target_exists"
    return "source_error"


def _fail(code: str, detail: str, reason: str = "") -> NoReturn:
    label = code if not reason or reason == code else f"{code} [{reason}]"
    message = f"{label}: {detail}" if detail else label
    console.print(Text(message, style="red"))
    raise typer.Exit(code=_FAILURE_EXIT)
