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
