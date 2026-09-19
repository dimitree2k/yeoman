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

Registration is intentionally *not* wired into the main application from this module,
because ``yeoman_gateway/cli/commands.py`` is owned by another change.  The intended
registration, next to the other command imports in that module, is::

    from . import knowledge_commands as _knowledge_commands  # noqa: F401
    from .core import app
    app.add_typer(_knowledge_commands.knowledge_app, name="knowledge")

Nothing is attached to the shared ``app`` at import time, so importing this module has
no side effect beyond defining the sub-app.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final, NoReturn

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

from .core import console

knowledge_app = typer.Typer(help="Person knowledge: offline migration inventory and build")
migration_app = typer.Typer(help="Inspect, build and verify offline legacy snapshots")
knowledge_app.add_typer(migration_app, name="migration")

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
        ) or "integrity, foreign keys or fingerprint check failed"
        _fail("manifest_mismatch", detail)


# ── output ───────────────────────────────────────────────────────────────────


def _print_inventory(inventory: MigrationInventory) -> None:
    for source in (inventory.contacts, inventory.memory):
        table = Table(title=Text(f"{source.path.name}  sha256:{source.fingerprint}"))
        table.add_column("table")
        table.add_column("rows", justify="right")
        for name, count in source.row_counts:
            table.add_row(name, str(count))
        console.print(table)
        console.print(
            Text(
                f"  schema_version: {source.schema_version or 'unknown'}"
                f"   tables: {len(source.tables)}"
                f"   unsupported: {len(source.unsupported)}"
                f"   identifier conflicts: {len(source.identifier_conflicts)}"
            )
        )
        if source.unsupported:
            console.print(
                Text(f"  unsupported objects: {', '.join(source.unsupported)}", style="yellow")
            )
    for statement in inventory.statements:
        console.print(Text(f"- {statement}"))


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
        console.print(Text(f"  quarantined {name}: {reason} x{count}", style="yellow"))
    console.print(Text(f"target: {report.target_path}  sha256:{report.target_fingerprint}"))
    console.print(Text(f"manifest: {report.manifest_path}  migration_complete=false"))
    console.print(
        Text(
            f"imported rows: {report.imported_rows}"
            f"   quarantined rows: {quarantined}"
            f"   unaccounted rows: {report.unaccounted_rows}"
        )
    )


def _print_verification(report: VerificationReport) -> None:
    console.print(Text(f"integrity_check: {'ok' if report.integrity_ok else 'failed'}"))
    console.print(
        Text(f"foreign_key_check: {'ok' if report.foreign_keys_ok else 'failed'}")
    )
    console.print(
        Text(
            "target fingerprint: "
            + ("matches manifest" if report.fingerprint_ok else "does not match manifest")
        )
    )
    console.print(
        Text(
            f"table counts: {'match manifest' if report.counts_match else 'differ'} "
            f"({len(report.mismatches)} mismatches)"
        )
    )
    console.print(Text(f"verdict: {report.verdict}", style="green" if report.verdict == "ok" else "red"))


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
