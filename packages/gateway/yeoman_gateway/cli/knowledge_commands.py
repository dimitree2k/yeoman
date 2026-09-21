"""Offline CLI for the person-knowledge migration.

The surface is exactly::

    yeoman knowledge migration inspect --contacts SNAPSHOT --memory SNAPSHOT
    yeoman knowledge migration build   --contacts SNAPSHOT --memory SNAPSHOT \\
                                       --target NEW_DB --manifest NEW_JSON
    yeoman knowledge migration verify  --target DB --manifest JSON

Everything here is offline and explicit: no default paths, no provider or bootstrap
startup, no implicit migration.  Every failure exits non-zero and prints a stable
reason code (``source_error``, ``target_exists``, ``unsupported_schema``,
``manifest_mismatch``); diagnostics are redacted to table names, counts and ids.

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


@capture_app.command("status")
def capture_status(
    target: Path = typer.Option(
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
