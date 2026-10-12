"""Persona evolution CLI commands."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import typer
from yeoman_shared.config.loader import load_config
from yeoman_shared.utils.helpers import (
    ensure_dir,
    get_operational_store_path,
    safe_filename,
)

from yeoman_gateway.persona_evolution import (
    apply_persona_evolution_proposal,
    deny_persona_evolution_proposal,
)

from .core import app, console

persona_evolution_app = typer.Typer(help="Propose persona evolution updates")
app.add_typer(persona_evolution_app, name="persona-evolution")


@persona_evolution_app.command("propose")
def persona_evolution_propose(
    persona_file: str = typer.Option(
        ...,
        "--persona-file",
        help="Workspace-relative persona file, e.g. personas/alpha-2.md",
    ),
    window_days: int = typer.Option(14, "--window-days", min=1, max=90),
    limit: int = typer.Option(20, "--limit", min=1, max=100),
    output: Path | None = typer.Option(
        None,
        "--output",
        help="Optional output path for the proposal markdown",
    ),
) -> None:
    """Generate a private persona evolution proposal from runtime evidence."""
    config = load_config()
    from yeoman_gateway.history.export import cli_secondary_request
    result = cli_secondary_request('persona_evolution_read', dict(persona_file=persona_file,
                                   window_days=window_days, limit=limit))
    path = output or config.workspace_path / 'persona-evolution' / 'proposals' / (safe_filename(persona_file) + '.md')
    ensure_dir(path.parent)
    path.write_text(result['report'], encoding='utf-8')
    console.print(f'proposal: {path}')
    return


@persona_evolution_app.command("status")
def persona_evolution_status(
    persona_file: str = typer.Option(
        "personas/alpha-2.md",
        "--persona-file",
        help="Workspace-relative persona file, e.g. personas/alpha-2.md",
    ),
    channel: str | None = typer.Option(None, "--channel", help="Optional chat channel"),
    chat_id: str | None = typer.Option(None, "--chat-id", help="Optional chat id"),
    limit: int = typer.Option(5, "--limit", "-n", min=1, max=50),
) -> None:
    """Show compact persona-evolution and learned-taste status."""

    from yeoman_gateway.history.export import cli_secondary_request
    result = cli_secondary_request('persona_evolution_read', dict(persona_file=persona_file, window_days=1, limit=limit))
    console.print(result['report'])
    return


@persona_evolution_app.command("approve")
def persona_evolution_approve(
    proposal_id: str = typer.Argument(..., help="Persona evolution proposal id"),
) -> None:
    """Approve and apply one pending persona-evolution proposal."""
    config = load_config()
    result = apply_persona_evolution_proposal(
        workspace=config.workspace_path,
        state_db_path=_state_db_path(config.workspace_path),
        proposal_id=proposal_id,
        approved_by_channel="cli",
        approved_by_chat_id="local",
    )
    _print_decision_result(result.status, result.message, result.persona_file)


@persona_evolution_app.command("deny")
def persona_evolution_deny(
    proposal_id: str = typer.Argument(..., help="Persona evolution proposal id"),
) -> None:
    """Deny one pending persona-evolution proposal without changing persona files."""
    config = load_config()
    result = deny_persona_evolution_proposal(
        state_db_path=_state_db_path(config.workspace_path),
        proposal_id=proposal_id,
        denied_by_channel="cli",
        denied_by_chat_id="local",
    )
    _print_decision_result(result.status, result.message, result.persona_file)


def _default_output_path(persona_file: str) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    name = safe_filename(persona_file.replace("/", "-").replace(".", "-"))
    return (
        get_operational_store_path("persona_evolution")
        / "proposals"
        / f"{stamp}-{name}.md"
    )


def _state_db_path(workspace: Path) -> Path:
    return workspace / "persona-evolution" / "persona-evolution.db"


def _persona_evolution_cron_status(persona_file: str) -> dict[str, object] | None:
    from yeoman_gateway.cron.service import CronService

    service = CronService(store_path=get_operational_store_path("cron"))
    for job in service.list_jobs(include_disabled=True):
        if job.payload.kind != "persona_evolution":
            continue
        if str(job.payload.persona_file or "") != persona_file:
            continue
        return {
            "id": job.id,
            "enabled": job.enabled,
            "next_run": _format_ms(job.state.next_run_at_ms),
            "last_run": _format_ms(job.state.last_run_at_ms),
            "last_status": job.state.last_status,
            "last_error": job.state.last_error,
        }
    return None


def _format_ms(value: int | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1000, UTC).isoformat()


def _print_decision_result(status: str, message: str, persona_file: str | None) -> None:
    target = f" persona={persona_file}" if persona_file else ""
    if status in {"applied", "denied"}:
        console.print(f"[green]✓[/green] {message}{target}")
        return
    console.print(f"[yellow]{status}[/yellow] {message}{target}")
    if status in {"not_found", "blocked"}:
        raise typer.Exit(1)


def _run_async(coro):
    import asyncio

    return asyncio.run(coro)
