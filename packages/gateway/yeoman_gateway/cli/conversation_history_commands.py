"""Conversation history: convert legacy stores, seed owner attestations, project and verify."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import typer

from .core import app

history_app = typer.Typer(help="Conversation history: preserved originals and the four-table history")
app.add_typer(history_app, name="history")


def _emit(report: Any) -> None:
    typer.echo(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))


@history_app.command("convert")
def history_convert(
    source_home: Path = typer.Option(..., "--source-home", help="Snapshot directory that contains data/"),
    out: Path = typer.Option(..., "--out", help="New Layer 1 root for backfill/ and derived/ (not data/raw)"),
    bridge_package_dir: Path | None = typer.Option(None, "--bridge-package-dir",
                                                   help="Bridge runtime with node_modules, for decoding"),
    extra_bridge_refs: list[Path] | None = typer.Option(None, "--extra-bridge-refs",
                                                        help="More folders of bridge reference copies"),
) -> None:
    from yeoman_gateway.history.convert.bridge_refs import node_batch_decoder
    from yeoman_gateway.history.convert.run import run_conversion

    decode = node_batch_decoder(bridge_package_dir) if bridge_package_dir else None
    _emit(run_conversion(source_home.expanduser(), out.expanduser(), decode=decode,
                         extra_bridge_dirs=[p.expanduser() for p in extra_bridge_refs or []]))


@history_app.command("seed-attestations")
def history_seed_attestations(
    out: Path = typer.Option(..., "--out", help="Layer 1 root that receives owner/attestations.jsonl"),
) -> None:
    from yeoman_shared.raw_archive.paths import is_protected

    from yeoman_gateway.history.attestations import write_seed

    if is_protected(out.expanduser()):
        raise typer.BadParameter("refusing to write into the protected raw archive")
    _emit({"written": str(write_seed(out.expanduser()))})


@history_app.command("project")
def history_project(
    layer1: list[Path] = typer.Option(..., "--layer1", help="Layer 1 root (repeat for several)"),
    db: Path = typer.Option(..., "--db", help="history.db to build (replaced atomically)"),
) -> None:
    from yeoman_gateway.history.project import project

    _emit(project([path.expanduser() for path in layer1], db.expanduser()))


@history_app.command("verify")
def history_verify(
    layer1: list[Path] = typer.Option(..., "--layer1", help="Layer 1 root (repeat for several)"),
    db: Path = typer.Option(..., "--db", help="Built history.db"),
    scratch: Path | None = typer.Option(None, "--scratch", help="On-disk folder for two rebuilds; not /tmp"),
) -> None:
    from yeoman_gateway.history.verify import verify

    if scratch is not None:
        scratch = scratch.expanduser()
        if scratch.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve()):
            raise typer.BadParameter("--scratch must be on disk, not the temporary directory")
    _emit(verify([path.expanduser() for path in layer1], db.expanduser(), scratch=scratch))


def _owner_mode(dry_run: bool, confirm: bool) -> None:
    if dry_run == confirm:
        raise typer.BadParameter('choose exactly one of --dry-run or --confirm')


@history_app.command('attest')
def history_attest(
    file: Path = typer.Option(..., '--file', help='Validated local owner records (JSONL)'),
    dry_run: bool = typer.Option(False, '--dry-run'),
    confirm: bool = typer.Option(False, '--confirm'),
) -> None:
    import os

    from yeoman_shared.raw_archive.paths import raw_root
    from yeoman_shared.raw_archive.records import (
        PURGE_DISPOSITION_LOCK,
        lock_file,
        preflight_owner_paths,
    )

    from yeoman_gateway.history.attestations import validate_owner_package

    _owner_mode(dry_run, confirm)
    try:
        records = [json.loads(line) for line in file.expanduser().read_text().splitlines()]
        if not records:
            raise ValueError('empty owner package')
        root = raw_root()
        preflight_owner_paths(root)
        validate_owner_package(root, records)
        if dry_run:
            _emit({'validated': len(records), 'committed': 0, 'suppressed': 0})
            return
        # Serialize package validation too; append_owner_record otherwise takes this same lock.
        # flock is not recursive across separate descriptors: use the protected locked variant.
        from yeoman_shared.raw_archive.records import append_owner_record_locked

        fd = lock_file(root / PURGE_DISPOSITION_LOCK, create=True)
        try:
            validate_owner_package(root, records)
            committed = sum(append_owner_record_locked(root, record) is not None for record in records)
        finally:
            os.close(fd)
        _emit({'validated': len(records), 'committed': committed, 'suppressed': len(records) - committed})
    except (OSError, ValueError, TypeError, KeyError):
        raise typer.BadParameter('owner package validation or publication failed') from None


@history_app.command('import-backfill')
def history_import_backfill(
    staged: Path = typer.Option(..., '--staged', help='Isolated converted Layer 1 root'),
    manifest: Path = typer.Option(..., '--manifest', help='Reviewed import manifest'),
    dry_run: bool = typer.Option(False, '--dry-run'),
    confirm: bool = typer.Option(False, '--confirm'),
) -> None:
    from yeoman_shared.raw_archive.paths import raw_root
    from yeoman_shared.raw_archive.records import import_backfill, preview_import

    from yeoman_gateway.history.convert.run import prepare_import_manifest

    _owner_mode(dry_run, confirm)
    try:
        package = json.loads(manifest.expanduser().read_text())
        source = staged.expanduser()
        if package != prepare_import_manifest(source):
            raise ValueError('manifest does not bind the validated staged records')
        result = (preview_import if dry_run else import_backfill)(raw_root(), source, package)
        _emit({'status': 'dry-run' if dry_run else result['status'], 'files': len(result['files']),
               'records': sum(f['lines'] for f in package['files'].values()),
               'suppressed': sum(f['suppressed'] for f in result['files'].values())})
    except (OSError, ValueError, TypeError, KeyError):
        raise typer.BadParameter('import package validation or publication failed') from None
