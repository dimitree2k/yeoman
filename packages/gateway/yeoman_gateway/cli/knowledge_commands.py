"""Offline CLI for the person-knowledge migration.

The surface is exactly::

    yeoman knowledge migration inspect     --contacts SNAPSHOT --memory SNAPSHOT
    yeoman knowledge migration build       --contacts SNAPSHOT --memory SNAPSHOT \\
                                           --target NEW_DB --manifest NEW_JSON
    yeoman knowledge migration inspect-legacy-nodes --target KNOWLEDGE_DB --out AUDIT_JSON
    yeoman knowledge migration verify      --target DB --manifest JSON
    yeoman knowledge migration inspect-v1  --source V1.db --processing PROCESSING.db
    yeoman knowledge migration upgrade-v1  --source V1.db --processing PROCESSING.db \\
                                           --target NEW-V2.db --manifest NEW.json
    yeoman knowledge migration verify-v1   --target NEW-V2.db --manifest NEW.json
    yeoman knowledge migration propose-bindings --source V1.db --processing PROCESSING.db \
                                           --out proposals.json
    yeoman knowledge migration propose-legacy-links --audit AUDIT_JSON \
                                           --target KNOWLEDGE_SNAPSHOT --out candidates.json
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

import os
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
    apply_legacy_link_decisions,
    inspect_legacy_nodes,
    inspect_sources,
    migrate_sources,
    reconstruct_legacy_statements,
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
from yeoman_gateway.knowledge.api import propose_legacy_links

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
    "apply_invalid": "approval_error",
    "apply_binding_missing": "semantics_error",
    "apply_binding_conflict": "semantics_error",
    "apply_contact_missing": "semantics_error",
    "apply_target_missing": "semantics_error",
    "apply_conflict": "semantics_error",
    "apply_checkpoint_failed": "semantics_error",
    "canonical_conflict": "semantics_error",
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


@migration_app.command("inspect-legacy-nodes")
def migration_inspect_legacy_nodes(
    target: Path = typer.Option(..., "--target", help="Knowledge database to read"),
    out: Path = typer.Option(..., "--out", help="New private, text-free audit manifest"),
    inbound_dir: Path | None = typer.Option(
        None, "--inbound-dir", help="Optional inbound JSONL source directory"
    ),
    processing_db: Path | None = typer.Option(
        None, "--processing-db", help="Optional ProcessingStore source database"
    ),
) -> None:
    """Inventory every legacy node without writing the database or reading node text."""
    try:
        inventory = inspect_legacy_nodes(
            target=target,
            inbound_dir=inbound_dir,
            processing_db=processing_db,
        )
    except MigrationSourceError as exc:
        _fail(_reason_code(exc.reason), exc.detail, exc.reason)

    output = out.expanduser()
    descriptor: int | None = None
    try:
        descriptor = os.open(
            output,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = None
            handle.write(inventory.to_json())
            handle.write("\n")
    except FileExistsError:
        _fail("target_exists", f"audit manifest already exists: {output}")
    except OSError as exc:
        _fail("source_error", f"cannot write audit manifest: {output}: {exc}")
    finally:
        if descriptor is not None:
            os.close(descriptor)

    counts = inventory.counts()
    _line(
        f"legacy nodes: {counts['nodes']}  active: {counts['active']}"
        f"  source ids: {counts['with_source_message_id']}"
        f"  senders: {counts['with_sender_id']}"
        f"  contacts: {counts['with_contact_id']}"
    )
    _line(
        f"fact shells: {counts['with_fact_shell']}"
        f"  statements: {counts['with_statement']}"
    )
    source_status = "  ".join(
        f"{status}={count}" for status, count in inventory.source_status_counts
    ) or "none"
    _line(
        "source status: " + source_status
    )
    _line(f"manifest: {output}  (private; node text omitted)")


@migration_app.command("propose-legacy-links")
def migration_propose_legacy_links(
    audit: Path = typer.Option(..., "--audit", help="Private legacy-node audit JSON"),
    target: Path = typer.Option(..., "--target", help="Knowledge snapshot to read"),
    out: Path = typer.Option(..., "--out", help="New private candidate manifest JSON"),
) -> None:
    """Write one text-free owner-review candidate row per audited legacy node."""
    try:
        manifest = propose_legacy_links(audit, target)
    except MigrationSourceError as exc:
        _fail(_reason_code(exc.reason), exc.detail, exc.reason)

    output = out.expanduser()
    descriptor: int | None = None
    try:
        descriptor = os.open(
            output,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = None
            handle.write(manifest.to_json())
            handle.write("\n")
    except FileExistsError:
        _fail("target_exists", f"candidate manifest already exists: {output}")
    except OSError as exc:
        _fail("source_error", f"cannot write candidate manifest: {output}: {exc}")
    finally:
        if descriptor is not None:
            os.close(descriptor)

    states = "  ".join(
        f"{state}={count}" for state, count in manifest.candidate_state_counts.items()
    ) or "none"
    dispositions = "  ".join(
        f"{disposition}={count}"
        for disposition, count in manifest.disposition_counts.items()
    ) or "none"
    _line(f"candidate nodes: {manifest.counts['nodes']}")
    _line("candidate states: " + states)
    _line("dispositions: " + dispositions)
    _line(f"manifest: {output}  (private; node text omitted)")


@migration_app.command("apply-legacy-link-decisions")
def migration_apply_legacy_link_decisions(
    candidates: Path = typer.Option(..., "--candidates", help="Private candidate manifest JSON"),
    target: Path = typer.Option(..., "--target", help="Knowledge database or snapshot to update"),
    out: Path = typer.Option(..., "--out", help="New private apply audit JSON"),
    approval_ref: str = typer.Option(..., "--approval-ref", help="Owner approval reference"),
) -> None:
    """Apply verified transport decisions to existing quarantine rows only."""
    output = out.expanduser()
    descriptor: int | None = None
    try:
        descriptor = os.open(
            output,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError:
        _fail("target_exists", f"apply audit already exists: {output}")
    except OSError as exc:
        _fail("source_error", f"cannot reserve apply audit: {output}: {exc}")

    try:
        report = apply_legacy_link_decisions(
            candidates,
            target,
            approval_ref=approval_ref,
        )
    except MigrationSourceError as exc:
        if descriptor is not None:
            os.close(descriptor)
            descriptor = None
        output.unlink(missing_ok=True)
        _fail(_reason_code(exc.reason), exc.detail, exc.reason)

    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = None
            handle.write(report.to_json())
            handle.write("\n")
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
            descriptor = None
        output.unlink(missing_ok=True)
        _fail("source_error", f"cannot write apply audit: {output}: {exc}")
    finally:
        if descriptor is not None:
            os.close(descriptor)

    _line(f"linked candidates: {report.linked_candidates}")
    _line(f"applied ledger decisions: {report.applied}")
    _line(f"already applied: {report.already_applied}")
    _line("statement rows changed: 0  speaker roles changed: 0")
    _line(f"apply audit: {output}  (private; node text omitted)")


@migration_app.command("reconstruct-legacy-statements")
def migration_reconstruct_legacy_statements(
    candidates: Path = typer.Option(..., "--candidates", help="Private candidate manifest JSON"),
    target: Path = typer.Option(..., "--target", help="Knowledge database or snapshot to update"),
    out: Path = typer.Option(..., "--out", help="New private reconstruction audit JSON"),
    approval_ref: str = typer.Option(..., "--approval-ref", help="Owner approval reference"),
) -> None:
    """Reconstruct canonical facts and verified speaker roles for linked candidates."""
    output = out.expanduser()
    descriptor: int | None = None
    try:
        descriptor = os.open(
            output,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError:
        _fail("target_exists", f"reconstruction audit already exists: {output}")
    except OSError as exc:
        _fail("source_error", f"cannot reserve reconstruction audit: {output}: {exc}")

    try:
        report = reconstruct_legacy_statements(
            candidates,
            target,
            approval_ref=approval_ref,
        )
    except MigrationSourceError as exc:
        if descriptor is not None:
            os.close(descriptor)
            descriptor = None
        output.unlink(missing_ok=True)
        _fail(_reason_code(exc.reason), exc.detail, exc.reason)

    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = None
            handle.write(report.to_json())
            handle.write("\n")
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
            descriptor = None
        output.unlink(missing_ok=True)
        _fail("source_error", f"cannot write reconstruction audit: {output}: {exc}")
    finally:
        if descriptor is not None:
            os.close(descriptor)

    _line(f"linked candidates: {report.linked_candidates}")
    _line(f"reconstructed statements: {report.reconstructed}")
    _line(f"already reconstructed: {report.already_reconstructed}")
    _line(f"fact rows created: {report.fact_rows_created}")
    _line(f"speaker roles created: {report.speaker_roles_created}")
    _line(f"other roles created: {report.other_roles_created}")
    _line(f"untouched candidates: {report.untouched_candidates}")
    _line(f"apply audit: {output}  (private; node text omitted)")


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
    _line(
        f"knowledge schema: {report.schema_version or 'unknown'}"
        + (
            "  (v1: attribute checks do not apply yet)"
            if report.schema_version == "1"
            else ""
        )
    )
    _line(f"cross-references: {'ok' if report.cross_references_ok else 'broken'}")
    _line(f"restore rehearsal: {'ok' if report.rehearsal_ok else 'failed'}")
    if report.locked_index_entries is None:
        _line("locked index entries: no index to check")
    elif report.locked_index_enforced:
        _line(
            "locked index entries: "
            + ("none" if report.locked_index_entries == 0 else f"present ({report.locked_index_entries})")
        )
    else:
        _line(
            f"locked index entries: {report.locked_index_entries} (v1 leftovers; the"
            " upgrade rebuilds the index without them, not a backup defect)"
        )
    _line(f"verdict: {report.verdict}", style="green" if report.ok else "red")
    if not report.ok:
        _fail("manifest_mismatch", report.reason)


@snapshot_app.command("collect")
def snapshot_collect(
    sources: Path = typer.Option(..., "--sources", help="JSON file containing explicit source descriptors"),
    target_dir: Path = typer.Option(..., "--target-dir", help="Private collection root"),
) -> None:
    """Collect explicitly named sources. No runtime source paths are inferred."""
    import json

    from yeoman_gateway.knowledge._snapshot import SnapshotError, collect_sources

    try:
        descriptors = json.loads(sources.read_text(encoding="utf-8"))
        report = collect_sources(sources=descriptors, target_dir=target_dir)
    except (OSError, ValueError, SnapshotError) as exc:
        message = exc.message if isinstance(exc, SnapshotError) else "cannot read source descriptor JSON"
        _fail("source_error", message, getattr(exc, "code", "sources_invalid"))
    _line(f"bundle: {report['bundle_dir']}")
    _line(f"manifest: {report['manifest_path']}")
    _line(f"sources: {report['source_count']}")
    _line(f"complete: {'yes' if report['complete'] else 'no'}")


@snapshot_app.command("verify-bundle")
def snapshot_verify_bundle(
    manifest: Path = typer.Option(..., "--manifest", help="Source bundle manifest"),
    restore_dir: Path | None = typer.Option(
        None, "--restore-dir", help="Optional directory for verified isolated copies"
    ),
) -> None:
    """Verify bundle hashes and SQLite structure without exposing row values."""
    from yeoman_gateway.knowledge._snapshot import SnapshotError, verify_source_bundle

    try:
        report = verify_source_bundle(manifest=manifest, restore_dir=restore_dir)
    except SnapshotError as exc:
        _fail("manifest_mismatch", exc.message, exc.code)
    _line(f"bundle verdict: {report['verdict']}", style="green" if report["verdict"] == "ok" else "red")
    _line(f"sources: {report['source_count']}")
    if report["verdict"] != "ok":
        _fail("manifest_mismatch", ", ".join(report["errors"]))


@snapshot_app.command("restore")
def snapshot_restore_bundle(
    manifest: Path = typer.Option(..., "--manifest", help="Source bundle manifest"),
    restore_dir: Path = typer.Option(..., "--restore-dir", help="New empty directory for isolated copies"),
) -> None:
    """Restore and verify a source bundle into an isolated directory."""
    from yeoman_gateway.knowledge._snapshot import SnapshotError, verify_source_bundle

    try:
        report = verify_source_bundle(manifest=manifest, restore_dir=restore_dir)
    except SnapshotError as exc:
        _fail("manifest_mismatch", exc.message, exc.code)
    _line(f"bundle verdict: {report['verdict']}", style="green" if report["verdict"] == "ok" else "red")
    _line(f"sources: {report['source_count']}")
    _line(f"restore: {restore_dir}")
    if report["verdict"] != "ok":
        _fail("manifest_mismatch", ", ".join(report["errors"]))


@snapshot_app.command("refresh")
def snapshot_refresh(
    sources: Path = typer.Option(..., "--sources", help="JSON file containing explicit source descriptors"),
    target_dir: Path = typer.Option(..., "--target-dir", help="Private collection root"),
) -> None:
    """Acquire changed explicit sources and update the local provenance catalog."""
    import json

    from yeoman_gateway.knowledge._snapshot import SnapshotError, refresh_collection

    try:
        descriptors = json.loads(sources.read_text(encoding="utf-8"))
        report = refresh_collection(sources=descriptors, target_dir=target_dir)
    except (OSError, ValueError, SnapshotError) as exc:
        message = exc.message if isinstance(exc, SnapshotError) else "cannot read source descriptor JSON"
        _fail("source_error", message, getattr(exc, "code", "sources_invalid"))
    _line(json.dumps(report, sort_keys=True))


@snapshot_app.command("query")
def snapshot_query(
    target_dir: Path = typer.Option(..., "--target-dir", help="Private collection root"),
    source_id: str | None = typer.Option(None, "--source-id"),
    chat: str | None = typer.Option(None, "--chat"),
    record_type: str | None = typer.Option(None, "--record-type"),
    native_id: str | None = typer.Option(None, "--native-id"),
    original_after: str | None = typer.Option(None, "--original-after"),
    original_before: str | None = typer.Option(None, "--original-before"),
    creation_after: str | None = typer.Option(None, "--creation-after"),
    creation_before: str | None = typer.Option(None, "--creation-before"),
    unknown_dates: bool = typer.Option(False, "--unknown-dates"),
) -> None:
    """Query owner-local provenance metadata and preserved-copy locators."""
    import json

    from yeoman_gateway.knowledge._snapshot import SnapshotError, query_catalog

    filters = {
        key: value
        for key, value in {
            "source_id": source_id,
            "chat": chat,
            "record_type": record_type,
            "native_id": native_id,
            "original_after": original_after,
            "original_before": original_before,
            "creation_after": creation_after,
            "creation_before": creation_before,
            "unknown_dates": True if unknown_dates else None,
        }.items()
        if value is not None
    }
    try:
        rows = query_catalog(target_dir=target_dir, filters=filters)
    except SnapshotError as exc:
        _fail("source_error", exc.message, exc.code)
    _line(json.dumps(rows, indent=2, sort_keys=True))


@snapshot_app.command("rebuild")
def snapshot_rebuild(
    target_dir: Path = typer.Option(..., "--target-dir", help="Private collection root"),
) -> None:
    """Rebuild the disposable catalog from immutable bundle manifests."""
    import json

    from yeoman_gateway.knowledge._snapshot import SnapshotError, rebuild_catalog

    try:
        report = rebuild_catalog(target_dir=target_dir)
    except SnapshotError as exc:
        _fail("source_error", exc.message, exc.code)
    _line(json.dumps(report, sort_keys=True))


@snapshot_app.command("purge")
def snapshot_purge(
    target_dir: Path = typer.Option(..., "--target-dir", help="Private collection root"),
    source_id: str = typer.Option(..., "--source-id"),
    yes: bool = typer.Option(False, "--yes", help="Apply the exact preview without prompting"),
) -> None:
    """Preview and confirm a local owner purge of every bundle containing a source."""
    import json
    import os

    from yeoman_gateway.knowledge._snapshot import SnapshotError, purge_collection

    operator = str(os.getuid()) if hasattr(os, "getuid") else ""
    try:
        preview = purge_collection(
            target_dir=target_dir,
            source_id=source_id,
            operator=operator,
            confirmed=False,
        )
        _line(json.dumps(preview, indent=2, sort_keys=True))
        if not yes and not typer.confirm("Permanently purge these source bundles?", default=False):
            raise typer.Exit(1)
        result = purge_collection(
            target_dir=target_dir,
            source_id=source_id,
            operator=operator,
            confirmed=True,
        )
    except SnapshotError as exc:
        _fail("source_error", exc.message, exc.code)
    _line(json.dumps(result, indent=2, sort_keys=True))


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


# ── owner identity maintenance ───────────────────────────────────────────────


def _person_summary(connection: Any, person_id: str) -> dict[str, Any] | None:
    """Counts and flags for one person, read from a read-only connection."""
    row = connection.execute(
        "SELECT id, is_owner, status FROM contacts WHERE id = ?", (str(person_id),)
    ).fetchone()
    if row is None:
        return None
    kinds = dict(
        connection.execute(
            "SELECT kind, COUNT(*) FROM knowledge_identifier_bindings"
            " WHERE person_id = ? AND status = 'active' GROUP BY kind",
            (str(person_id),),
        ).fetchall()
    )
    statements = connection.execute(
        "SELECT COUNT(DISTINCT statement_id) FROM knowledge_statement_people"
        " WHERE person_id = ?",
        (str(person_id),),
    ).fetchone()[0]
    redirected = connection.execute(
        "SELECT COUNT(*) FROM knowledge_identity_redirects WHERE source_id = ? AND active = 1",
        (str(person_id),),
    ).fetchone()[0]
    return {
        "owner": bool(int(row["is_owner"] or 0)),
        "status": str(row["status"]),
        "bindings": "  ".join(f"{kind}={count}" for kind, count in sorted(kinds.items())) or "none",
        "statements": int(statements),
        "redirected": bool(redirected),
    }


def _open_admin_knowledge(db: Path | None, policy_path: Path | None) -> Any:
    """Open the knowledge facade with the live Policy as admin authority.

    Admin authority comes from the policy file's owners, never from the command line.
    """
    from yeoman_shared.config.loader import load_config

    from yeoman_gateway.knowledge import open_knowledge_store, workspace_id_for
    from yeoman_gateway.knowledge.runtime import (
        RuntimeKnowledgePolicy,
        RuntimeKnowledgeSources,
    )
    from yeoman_gateway.policy.loader import load_policy

    config = load_config()
    policy = load_policy(policy_path)
    return open_knowledge_store(
        Path(db or config.knowledge.db_path).expanduser(),
        workspace_id=workspace_id_for(config.workspace_path),
        source_authority=RuntimeKnowledgeSources(),
        policy_authority=RuntimeKnowledgePolicy(
            engine=policy,
            admin_principals=frozenset(getattr(policy, "admin_principals", frozenset())),
        ),
        create=False,
    )


def _default_knowledge_path(db: Path | None) -> Path:
    if db is not None:
        return Path(db).expanduser()
    from yeoman_shared.config.loader import load_config

    return Path(load_config().knowledge.db_path).expanduser()


@knowledge_app.command("person-merge")
def knowledge_person_merge(
    target: str = typer.Option(..., "--target", help="Person id that stays canonical"),
    source: str = typer.Option(..., "--source", help="Person id redirected to the target"),
    apply: bool = typer.Option(
        False, "--apply", help="Write the merge; without it this is a dry run"
    ),
    db: Path | None = typer.Option(None, "--db", help="Knowledge database (default: config)"),
    policy: Path | None = typer.Option(None, "--policy", help="Policy file (default: live)"),
) -> None:
    """Merge two people by exact id as a reversible redirect (dry run by default).

    Both person rows, their bindings and statement edges stay as they are; reads follow
    the redirect.  An owner-flagged source is refused unless the target is owner-flagged
    too, because a merge never moves the owner flag.
    """
    target_id, source_id = str(target).strip(), str(source).strip()
    if not target_id or not source_id or target_id == source_id:
        _fail("invalid_input", "target and source must be two different person ids")
    connection = _open_readonly_connection(_default_knowledge_path(db))
    try:
        people = {
            "target": _person_summary(connection, target_id),
            "source": _person_summary(connection, source_id),
        }
    finally:
        connection.close()
    for role, summary in people.items():
        if summary is None:
            _fail("unresolved", f"unknown {role} person")
        if summary["status"] != "active":
            _fail("identity_conflict", f"the {role} is not active")
        if summary["redirected"]:
            _fail("identity_conflict", f"the {role} is already merged into another person")
    target_summary, source_summary = people["target"], people["source"]
    assert target_summary is not None and source_summary is not None
    if source_summary["owner"] and not target_summary["owner"]:
        _fail(
            "owner_record",
            "the source is owner-flagged and the target is not; a merge never moves the"
            " owner flag",
        )
    for role, person_id in (("target", target_id), ("source", source_id)):
        summary = people[role]
        assert summary is not None
        _line(
            f"  {role} {_redacted(person_id)}  bindings {summary['bindings']}"
            f"  statements={summary['statements']}"
            f"{'  owner' if summary['owner'] else ''}"
        )
    if not apply:
        _line(
            f"dry run: would merge source {_redacted(source_id)}"
            f" into target {_redacted(target_id)}; re-run with --apply to write"
        )
        return
    knowledge = _open_admin_knowledge(db, policy)
    try:
        receipt = knowledge.merge_people_with_policy(
            target_id, source_id, reason="cli_person_merge"
        )
    except Exception as exc:
        _fail("merge_failed", str(getattr(exc, "code", "") or type(exc).__name__))
    finally:
        knowledge.close()
    _line(
        f"merged source {_redacted(source_id)} into target {_redacted(target_id)}"
        f" (operation {receipt.operation_id})"
    )


@knowledge_app.command("person-merge-undo")
def knowledge_person_merge_undo(
    operation: str = typer.Option(..., "--operation", help="Merge operation id to undo"),
    apply: bool = typer.Option(
        False, "--apply", help="Write the undo; without it this is a dry run"
    ),
    db: Path | None = typer.Option(None, "--db", help="Knowledge database (default: config)"),
    policy: Path | None = typer.Option(None, "--policy", help="Policy file (default: live)"),
) -> None:
    """Undo one person merge by operation id (dry run by default)."""
    operation_id = str(operation).strip()
    connection = _open_readonly_connection(_default_knowledge_path(db))
    try:
        row = connection.execute(
            "SELECT source_id, target_id FROM knowledge_identity_redirects"
            " WHERE operation_id = ? AND active = 1",
            (operation_id,),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        _fail("unresolved", "no active merge with this operation id")
    if not apply:
        _line(
            f"dry run: would undo the merge of {_redacted(row['source_id'])}"
            f" into {_redacted(row['target_id'])}; re-run with --apply to write"
        )
        return
    knowledge = _open_admin_knowledge(db, policy)
    try:
        knowledge.undo_merge_with_policy(operation_id, reason="cli_person_merge_undo")
    except Exception as exc:
        _fail("undo_failed", str(getattr(exc, "code", "") or type(exc).__name__))
    finally:
        knowledge.close()
    _line(
        f"undone: {_redacted(row['source_id'])} is separate from"
        f" {_redacted(row['target_id'])} again"
    )


@knowledge_app.command("person-name")
def knowledge_person_name(
    person: str = typer.Option(..., "--person", help="Person id"),
    name: str = typer.Option(..., "--name", help="Owner-confirmed preferred name"),
    apply: bool = typer.Option(
        False, "--apply", help="Write the name; without it this is a dry run"
    ),
    db: Path | None = typer.Option(None, "--db", help="Knowledge database (default: config)"),
    policy: Path | None = typer.Option(None, "--policy", help="Policy file (default: live)"),
) -> None:
    """Set one person's owner-confirmed preferred name (dry run by default)."""
    person_id, clean = str(person).strip(), str(name).strip()
    if not clean:
        _fail("invalid_input", "the name must not be empty")
    connection = _open_readonly_connection(_default_knowledge_path(db))
    try:
        summary = _person_summary(connection, person_id)
    finally:
        connection.close()
    if summary is None:
        _fail("unresolved", "unknown person")
    if not apply:
        _line(
            f"dry run: would set the preferred name of {_redacted(person_id)};"
            " re-run with --apply to write"
        )
        return
    knowledge = _open_admin_knowledge(db, policy)
    try:
        knowledge.set_preferred_name_with_policy(person_id, clean, reason="cli_person_name")
    except Exception as exc:
        _fail("name_failed", str(getattr(exc, "code", "") or type(exc).__name__))
    finally:
        knowledge.close()
    _line(f"preferred name set for {_redacted(person_id)}")


@knowledge_app.command("person-alias-retire")
def knowledge_person_alias_retire(
    person: str = typer.Option(..., "--person", help="Person id the alias belongs to"),
    alias: str = typer.Option(..., "--alias", help="Exact alias text to retire"),
    apply: bool = typer.Option(
        False, "--apply", help="Write the retirement; without it this is a dry run"
    ),
    db: Path | None = typer.Option(None, "--db", help="Knowledge database (default: config)"),
    policy: Path | None = typer.Option(None, "--policy", help="Policy file (default: live)"),
) -> None:
    """Retire a wrong alias and retract its mapping (dry run by default).

    The rows stay as history; the name stops matching searches and addresses for this
    person.  Every observation source of that exact alias on the person is retired.
    """
    person_id, text = str(person).strip(), str(alias).strip()
    connection = _open_readonly_connection(_default_knowledge_path(db))
    try:
        rows = connection.execute(
            "SELECT id FROM contact_aliases WHERE contact_id = ? AND alias = ?"
            " AND status <> 'retired' ORDER BY id",
            (person_id, text),
        ).fetchall()
    finally:
        connection.close()
    if not rows:
        _fail("unresolved", "the person has no active alias with this exact text")
    if not apply:
        _line(
            f"dry run: would retire {len(rows)} alias row(s) of {_redacted(person_id)};"
            " re-run with --apply to write"
        )
        return
    knowledge = _open_admin_knowledge(db, policy)
    try:
        for row in rows:
            knowledge.retire_alias_with_policy(
                int(row["id"]), correct_mapping=True, reason="cli_person_alias_retire"
            )
    except Exception as exc:
        _fail("retire_failed", str(getattr(exc, "code", "") or type(exc).__name__))
    finally:
        knowledge.close()
    _line(f"retired {len(rows)} alias row(s) of {_redacted(person_id)}")


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
