"""Persona evolution CLI commands."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import typer
from yeoman_shared.config.loader import load_config
from yeoman_shared.utils.helpers import ensure_dir, get_operational_data_path, safe_filename

from yeoman_gateway.consciousness.log import SpeakupLog
from yeoman_gateway.memory import MemoryService
from yeoman_gateway.persona_evolution import (
    apply_persona_evolution_proposal,
    build_persona_evolution_status,
    collect_persona_evolution_evidence,
    deny_persona_evolution_proposal,
    render_persona_evolution_proposal,
)
from yeoman_gateway.policy.loader import load_policy
from yeoman_gateway.storage.inbound_archive import InboundArchive

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
    policy = load_policy()
    memory = MemoryService(workspace=config.workspace_path, config=config.memory, root_config=config)
    speakup_log = SpeakupLog(get_operational_data_path() / "consciousness" / "speakups.db")
    inbound_archive = InboundArchive(get_operational_data_path() / "inbound" / "reply_context.db")
    try:
        evidence = _run_async(
            collect_persona_evolution_evidence(
                policy=policy,
                workspace=config.workspace_path,
                persona_file=persona_file,
                memory=memory,
                speakup_log=speakup_log,
                inbound_archive=inbound_archive,
                window_days=window_days,
                per_chat_limit=limit,
            )
        )
        rendered = render_persona_evolution_proposal(evidence)
        target = output or _default_output_path(persona_file)
        if not target.is_absolute():
            target = Path.cwd() / target
        ensure_dir(target.parent)
        target.write_text(rendered, encoding="utf-8")
    finally:
        memory.close()
        speakup_log.close()
        inbound_archive.close()

    console.print("[green]✓[/green] Wrote persona evolution proposal.")
    console.print(f"path: {target}")
    console.print(f"persona: {persona_file}")
    console.print(f"chats: {len(evidence.chats)}")


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
    from rich.table import Table

    config = load_config()
    policy = load_policy()
    memory = MemoryService(workspace=config.workspace_path, config=config.memory, root_config=config)
    speakup_log = SpeakupLog(get_operational_data_path() / "consciousness" / "speakups.db")
    try:
        status = _run_async(
            build_persona_evolution_status(
                policy=policy,
                workspace=config.workspace_path,
                memory=memory,
                speakup_log=speakup_log,
                state_db_path=_state_db_path(config.workspace_path),
                persona_file=persona_file,
                channel=channel,
                chat_id=chat_id,
                limit=limit,
            )
        )
    finally:
        memory.close()
        speakup_log.close()

    console.print("[bold]Persona Evolution Status[/bold]")
    console.print(f"persona: {status['persona_file']}")
    console.print(f"persona_path: {status['persona_path']}")
    cron_status = _persona_evolution_cron_status(persona_file)
    if cron_status:
        console.print(f"cron_job: {cron_status['id']} enabled={cron_status['enabled']}")
        console.print(f"next_run: {cron_status['next_run'] or '-'}")
        console.print(f"last_run: {cron_status['last_run'] or '-'}")
        console.print(f"last_status: {cron_status['last_status'] or '-'}")
    metrics = status["metrics"]
    console.print(f"proposal_metrics: {metrics['proposals']}")
    console.print(f"scan_metrics: {metrics['scans']}")

    pending_table = Table(title="Pending Proposals")
    pending_table.add_column("ID")
    pending_table.add_column("Created")
    pending_table.add_column("Messages", justify="right")
    pending_table.add_column("Score", justify="right")
    pending_table.add_column("Path")
    for proposal in status["pending_proposals"]:
        pending_table.add_row(
            str(proposal["proposal_id"]),
            str(proposal["created_at"]),
            str(proposal["total_message_count"]),
            f"{float(proposal['signal_score'] or 0.0):.2f}",
            str(proposal["proposal_path"]),
        )
    console.print(pending_table)

    scans_table = Table(title="Latest Scans")
    scans_table.add_column("Scanned")
    scans_table.add_column("Result")
    scans_table.add_column("Reason")
    scans_table.add_column("Messages", justify="right")
    scans_table.add_column("Score", justify="right")
    for scan in status["latest_scans"]:
        scans_table.add_row(
            str(scan["scanned_at"]),
            str(scan["result"]),
            str(scan["reason"] or "-"),
            str(scan["total_message_count"]),
            f"{float(scan['signal_score'] or 0.0):.2f}",
        )
    console.print(scans_table)

    chat = status.get("chat")
    if chat:
        console.print("[bold]Chat Learning[/bold]")
        console.print(f"channel: {chat['channel']}")
        console.print(f"chat_id: {chat['chat_id']}")
        console.print(f"sent_speakups: {chat['sent_speakups']}")
        console.print(f"labeled_outcomes: {chat['labeled_outcomes']}")
        console.print(f"taste_distillations: {chat['taste_distillations']}")
        console.print(f"last_learned_taste: {chat['last_learned_taste'] or '-'}")


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
        get_operational_data_path()
        / "persona-evolution"
        / "proposals"
        / f"{stamp}-{name}.md"
    )


def _state_db_path(workspace: Path) -> Path:
    return workspace / "persona-evolution" / "persona-evolution.db"


def _persona_evolution_cron_status(persona_file: str) -> dict[str, object] | None:
    from yeoman_gateway.cron.service import CronService

    service = CronService(store_path=get_operational_data_path() / "cron" / "jobs.json")
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
