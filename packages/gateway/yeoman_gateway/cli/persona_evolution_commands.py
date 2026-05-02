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
    collect_persona_evolution_evidence,
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


def _default_output_path(persona_file: str) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    name = safe_filename(persona_file.replace("/", "-").replace(".", "-"))
    return (
        get_operational_data_path()
        / "persona-evolution"
        / "proposals"
        / f"{stamp}-{name}.md"
    )


def _run_async(coro):
    import asyncio

    return asyncio.run(coro)
