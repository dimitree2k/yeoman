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

    try:
        _emit(project([path.expanduser() for path in layer1], db.expanduser()))
    except (PermissionError, BlockingIOError):
        raise typer.BadParameter("history is owned; use history rebuild --confirm through Gateway") from None


@history_app.command("verify")
def history_verify(
    layer1: list[Path] = typer.Option(..., "--layer1", help="Layer 1 root (repeat for several)"),
    db: Path = typer.Option(..., "--db", help="Built history.db"),
    scratch: Path | None = typer.Option(None, "--scratch", help="On-disk folder for two rebuilds; not /tmp"),
    frozen: bool = typer.Option(False, "--frozen", help="Explicitly frozen DB and Layer 1 inputs"),
) -> None:
    from yeoman_gateway.history.verify import verify

    if scratch is not None:
        scratch = scratch.expanduser()
        if scratch.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve()):
            raise typer.BadParameter("--scratch must be on disk, not the temporary directory")
    if scratch is not None and not frozen:
        raise typer.BadParameter("--scratch requires --frozen")
    _emit(verify([path.expanduser() for path in layer1], db.expanduser(), scratch=scratch, frozen=frozen))


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
        import hashlib

        from yeoman_gateway.history.control import (
            _parse_owner_package,
            _read_package_bytes,
            cli_control,
            projection_owned,
        )
        path = file.expanduser().absolute()
        data = _read_package_bytes(path)
        digest = hashlib.sha256(data).hexdigest()
        records = _parse_owner_package(data)
        if not records:
            raise ValueError('empty owner package')
        root = raw_root()
        preflight_owner_paths(root)
        validate_owner_package(root, records)
        if confirm and projection_owned(root):
            _emit(cli_control("attest", {"confirm": True, "package_path": str(path), "package_sha256": digest}))
            return
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
        import hashlib

        from yeoman_gateway.history.control import (
            _load_pinned_bytes,
            _read_package_bytes,
            cli_control,
            projection_owned,
        )
        path = manifest.expanduser().absolute()
        data = _read_package_bytes(path)
        digest = hashlib.sha256(data).hexdigest()
        package = json.loads(_load_pinned_bytes(path, digest))
        source = staged.expanduser()
        if package != prepare_import_manifest(source):
            raise ValueError('manifest does not bind the validated staged records')
        if confirm and projection_owned(raw_root()):
            _emit(cli_control("import-backfill", {"confirm": True, "package_path": str(path), "package_sha256": digest, "staged_path": str(source.absolute())}))
            return
        result = (preview_import if dry_run else import_backfill)(raw_root(), source, package)
        _emit({'status': 'dry-run' if dry_run else result['status'], 'files': len(result['files']),
               'records': sum(f['lines'] for f in package['files'].values()),
               'suppressed': sum(f['suppressed'] for f in result['files'].values())})
    except (OSError, ValueError, TypeError, KeyError):
        raise typer.BadParameter('import package validation or publication failed') from None


@history_app.command('rebuild')
def history_rebuild(confirm: bool = typer.Option(False, '--confirm')) -> None:
    from yeoman_gateway.history.control import cli_control
    if not confirm:
        raise typer.BadParameter('--confirm required')
    _emit(cli_control('rebuild', {'confirm': True}))


@history_app.command('projection-status')
def history_projection_status() -> None:
    import asyncio

    from yeoman_shared.config.loader import load_config

    from yeoman_gateway.history.control import request_history_control
    try:
        _emit(asyncio.run(request_history_control(Path(load_config().ipc.gateway_socket_path).expanduser(), 'status', {})))
    except (OSError, ValueError, TimeoutError):
        raise typer.BadParameter('Gateway history control unavailable') from None
