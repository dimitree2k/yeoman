"""Raw message archive CLI. Purge is owner-only and needs confirmation."""

from __future__ import annotations

import getpass
import json
import tempfile
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import typer

from .core import app, console

raw_app = typer.Typer(help="Append-only raw message archive")
app.add_typer(raw_app, name="raw")


@raw_app.command("status")
def raw_status(json_output: bool = typer.Option(False, "--json", help="Output JSON")) -> None:
    """Show writer state, file and line counts."""
    from yeoman_shared.raw_archive.paths import raw_root
    from yeoman_shared.raw_archive.records import archive_files, file_digest
    from yeoman_shared.raw_archive.writer import STATUS_FILE, read_start_ms
    from yeoman_shared.utils.helpers import get_run_path

    root = raw_root()
    files = archive_files(root)
    lines = sum(file_digest(path)[1] for path in files)
    try:
        writer = json.loads((get_run_path() / STATUS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        writer = {"state": "unknown"}
    data = {
        "root": str(root),
        "started_ms": read_start_ms(root) or 0,
        "files": len(files),
        "lines": lines,
        "writer": writer,
    }
    if json_output:
        typer.echo(json.dumps(data))
    else:
        console.print(data)


@raw_app.command("verify")
def raw_verify() -> None:
    """Seal finished months and check integrity. Exit code 1 on any problem."""
    from yeoman_shared.raw_archive.verify import verify_archive

    report = verify_archive()
    for problem in report.problems:
        typer.echo(f"PROBLEM {problem}")
    typer.echo(f"files={report.files_checked} lines={report.lines_total} closed={len(report.closed)}")
    raise typer.Exit(0 if report.ok else 1)


@raw_app.command("seed")
def raw_seed(dry_run: bool = typer.Option(False, "--dry-run", help="Count only")) -> None:
    """One-time import of history older than the archive START marker."""
    from yeoman_shared.raw_archive.paths import raw_root

    from yeoman_gateway.storage.raw_seed import SeedPaths, seed_raw_archive

    report = seed_raw_archive(raw_root(), SeedPaths.default(), dry_run=dry_run)
    typer.echo(json.dumps(asdict(report), indent=2, sort_keys=True))


@raw_app.command("rebuild-drill")
def raw_rebuild_drill(
    channel: str = typer.Option(..., "--channel"),
    chat: str = typer.Option(..., "--chat"),
    target: Path | None = typer.Option(None, "--target", help="Empty directory; default: a temp dir"),
    compare_live: bool = typer.Option(False, "--compare-live", help="Compare with the live journal"),
) -> None:
    """Replay one chat from the raw archive into an empty journal."""
    from yeoman_shared.raw_archive.paths import raw_root
    from yeoman_shared.utils.helpers import get_operational_data_path

    from yeoman_gateway.storage.raw_rebuild import rebuild_chat

    home = target or Path(tempfile.mkdtemp(prefix="yeoman-rebuild-"))
    live = get_operational_data_path() / "processing" / "processing.db" if compare_live else None
    report = rebuild_chat(
        raw_root(),
        channel=channel,
        chat_id=chat,
        target_home=home,
        live_processing_db=live,
    )
    typer.echo(json.dumps({**asdict(report), "target": str(home)}, indent=2))
    if compare_live and (report.missing_vs_live or report.extra_vs_live):
        raise typer.Exit(1)


@raw_app.command("purge")
def raw_purge(
    channel: str = typer.Option(..., "--channel"),
    chat: str | None = typer.Option(None, "--chat"),
    message: str | None = typer.Option(None, "--message"),
    before: str | None = typer.Option(None, "--before", help="ISO date, e.g. 2026-09-01"),
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation prompt"),
) -> None:
    """Physically delete lines from the raw archive. Owner only; writes an AUDIT record."""
    from yeoman_shared.raw_archive.paths import raw_root
    from yeoman_shared.raw_archive.purge import PurgeSelector, plan_purge, purge

    before_ms = int(datetime.fromisoformat(before).timestamp() * 1000) if before else None
    selector = PurgeSelector(channel=channel, chat_id=chat, native_id=message, before_ms=before_ms)
    try:
        plan = plan_purge(raw_root(), selector)
    except ValueError as exc:
        typer.echo(f"Refused: {exc}")
        raise typer.Exit(2) from exc
    typer.echo(
        f"Would remove {plan.removed_lines} line(s) from {len(plan.files)} file(s) "
        f"and {len(plan.media_removed)} media file(s)."
    )
    if plan.removed_lines == 0:
        return
    if not yes and not typer.confirm("Permanently delete these lines?", default=False):
        raise typer.Exit(1)
    result = purge(raw_root(), selector, operator=getpass.getuser())
    typer.echo(f"Removed {result.removed_lines} line(s); AUDIT updated.")
