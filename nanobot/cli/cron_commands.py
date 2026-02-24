"""Cron/scheduled-task CLI commands."""

from __future__ import annotations

import asyncio

import typer
from rich.table import Table

from .core import app, console

cron_app = typer.Typer(help="Manage scheduled tasks")
app.add_typer(cron_app, name="cron")


@cron_app.command("list")
def cron_list(
    all: bool = typer.Option(False, "--all", "-a", help="Include disabled jobs"),
) -> None:
    """List scheduled jobs."""
    import time

    from nanobot.cron.service import CronService
    from nanobot.utils.helpers import get_operational_data_path

    store_path = get_operational_data_path() / "cron" / "jobs.json"
    service = CronService(store_path)

    jobs = service.list_jobs(include_disabled=all)

    if not jobs:
        console.print("No scheduled jobs.")
        return

    table = Table(title="Scheduled Jobs")
    table.add_column("ID", style="cyan")
    table.add_column("Name")
    table.add_column("Kind")
    table.add_column("Schedule")
    table.add_column("Status")
    table.add_column("Next Run")

    for job in jobs:
        if job.schedule.kind == "every":
            sched = f"every {(job.schedule.every_ms or 0) // 1000}s"
        elif job.schedule.kind == "cron":
            sched = job.schedule.expr or ""
        else:
            sched = "one-time"

        next_run = ""
        if job.state.next_run_at_ms:
            next_run = time.strftime(
                "%Y-%m-%d %H:%M", time.localtime(job.state.next_run_at_ms / 1000)
            )

        status = "[green]enabled[/green]" if job.enabled else "[dim]disabled[/dim]"

        table.add_row(job.id, job.name, job.payload.kind, sched, status, next_run)

    console.print(table)


@cron_app.command("add")
def cron_add(
    name: str = typer.Option(..., "--name", "-n", help="Job name"),
    message: str = typer.Option(..., "--message", "-m", help="Message for agent"),
    every: int = typer.Option(None, "--every", "-e", help="Run every N seconds"),
    cron_expr: str = typer.Option(None, "--cron", "-c", help="Cron expression (e.g. '0 9 * * *')"),
    at: str = typer.Option(None, "--at", help="Run once at time (ISO format)"),
    deliver: bool = typer.Option(False, "--deliver", "-d", help="Deliver response to channel"),
    to: str = typer.Option(None, "--to", help="Recipient for delivery"),
    channel: str = typer.Option(
        None, "--channel", help="Channel for delivery (e.g. 'telegram', 'whatsapp')"
    ),
) -> None:
    """Add a scheduled job."""
    from nanobot.cron.service import CronService
    from nanobot.cron.types import CronSchedule
    from nanobot.utils.helpers import get_operational_data_path

    if every:
        schedule = CronSchedule(kind="every", every_ms=every * 1000)
    elif cron_expr:
        schedule = CronSchedule(kind="cron", expr=cron_expr)
    elif at:
        import datetime

        dt = datetime.datetime.fromisoformat(at)
        schedule = CronSchedule(kind="at", at_ms=int(dt.timestamp() * 1000))
    else:
        console.print("[red]Error: Must specify --every, --cron, or --at[/red]")
        raise typer.Exit(1)

    store_path = get_operational_data_path() / "cron" / "jobs.json"
    service = CronService(store_path)

    job = service.add_job(
        name=name,
        schedule=schedule,
        message=message,
        deliver=deliver,
        to=to,
        channel=channel,
    )

    console.print(f"[green]✓[/green] Added job '{job.name}' ({job.id})")


@cron_app.command("add-voice")
def cron_add_voice(
    name: str = typer.Option(..., "--name", "-n", help="Job name"),
    phrase: list[str] = typer.Option(
        None,
        "--phrase",
        "-p",
        help="Voice phrase candidate (repeat for multiple values)",
    ),
    phrases_file: str = typer.Option(
        None,
        "--phrases-file",
        help="Optional text file with one phrase per line",
    ),
    randomize: bool = typer.Option(
        True,
        "--random/--no-random",
        help="Pick a random phrase each run",
    ),
    group: str = typer.Option(None, "--group", help="WhatsApp group tag/alias/chat_id"),
    chat_id: str = typer.Option(None, "--chat-id", help="Explicit target chat id"),
    channel: str = typer.Option("whatsapp", "--channel", help="Target channel"),
    voice: str = typer.Option(None, "--voice", help="Optional TTS voice name/id"),
    tts_route: str = typer.Option(None, "--tts-route", help="Optional model route key"),
    verbatim: bool = typer.Option(
        True,
        "--verbatim/--normalize",
        help="Send text verbatim or apply normalization/truncation",
    ),
    max_sentences: int = typer.Option(None, "--max-sentences", help="Sentence cap when normalized"),
    max_chars: int = typer.Option(None, "--max-chars", help="Character cap when normalized"),
    every: int = typer.Option(None, "--every", "-e", help="Run every N seconds"),
    cron_expr: str = typer.Option(None, "--cron", "-c", help="Cron expression (e.g. '0 9 * * 1')"),
    at: str = typer.Option(None, "--at", help="Run once at time (ISO format)"),
) -> None:
    """Add a scheduled voice broadcast job."""
    import datetime
    from pathlib import Path

    from nanobot.cron.service import CronService
    from nanobot.cron.types import CronSchedule
    from nanobot.utils.helpers import get_operational_data_path

    chosen = [every is not None, bool(cron_expr), bool(at)]
    if sum(chosen) != 1:
        console.print("[red]Error: specify exactly one schedule: --every, --cron, or --at[/red]")
        raise typer.Exit(1)

    if every is not None:
        if every <= 0:
            console.print("[red]Error: --every must be > 0[/red]")
            raise typer.Exit(1)
        schedule = CronSchedule(kind="every", every_ms=every * 1000)
        delete_after_run = False
    elif cron_expr:
        schedule = CronSchedule(kind="cron", expr=cron_expr)
        delete_after_run = False
    else:
        dt = datetime.datetime.fromisoformat(str(at))
        schedule = CronSchedule(kind="at", at_ms=int(dt.timestamp() * 1000))
        delete_after_run = True

    phrases: list[str] = [str(v).strip() for v in list(phrase or []) if str(v).strip()]
    if phrases_file:
        path = Path(phrases_file).expanduser()
        if not path.exists():
            console.print(f"[red]Error: phrases file not found: {path}[/red]")
            raise typer.Exit(1)
        file_lines = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()]
        phrases.extend([ln for ln in file_lines if ln])

    if not phrases:
        console.print("[red]Error: provide at least one --phrase or --phrases-file[/red]")
        raise typer.Exit(1)
    if not (str(group or "").strip() or str(chat_id or "").strip()):
        console.print("[red]Error: provide --group or --chat-id[/red]")
        raise typer.Exit(1)

    store_path = get_operational_data_path() / "cron" / "jobs.json"
    service = CronService(store_path)
    job = service.add_voice_job(
        name=name,
        schedule=schedule,
        messages=phrases,
        randomize=randomize,
        group=group,
        chat_id=chat_id,
        channel=channel,
        voice=voice,
        tts_route=tts_route,
        verbatim=verbatim,
        max_sentences=max_sentences,
        max_chars=max_chars,
        delete_after_run=delete_after_run,
    )

    console.print(f"[green]✓[/green] Added voice job '{job.name}' ({job.id})")


@cron_app.command("remove")
def cron_remove(
    job_id: str = typer.Argument(..., help="Job ID to remove"),
) -> None:
    """Remove a scheduled job."""
    from nanobot.cron.service import CronService
    from nanobot.utils.helpers import get_operational_data_path

    store_path = get_operational_data_path() / "cron" / "jobs.json"
    service = CronService(store_path)

    if service.remove_job(job_id):
        console.print(f"[green]✓[/green] Removed job {job_id}")
    else:
        console.print(f"[red]Job {job_id} not found[/red]")


@cron_app.command("enable")
def cron_enable(
    job_id: str = typer.Argument(..., help="Job ID"),
    disable: bool = typer.Option(False, "--disable", help="Disable instead of enable"),
) -> None:
    """Enable or disable a job."""
    from nanobot.cron.service import CronService
    from nanobot.utils.helpers import get_operational_data_path

    store_path = get_operational_data_path() / "cron" / "jobs.json"
    service = CronService(store_path)

    job = service.enable_job(job_id, enabled=not disable)
    if job:
        status = "disabled" if disable else "enabled"
        console.print(f"[green]✓[/green] Job '{job.name}' {status}")
    else:
        console.print(f"[red]Job {job_id} not found[/red]")


@cron_app.command("run")
def cron_run(
    job_id: str = typer.Argument(..., help="Job ID to run"),
    force: bool = typer.Option(False, "--force", "-f", help="Run even if disabled"),
) -> None:
    """Manually run a job."""
    from nanobot.cron.service import CronService
    from nanobot.utils.helpers import get_operational_data_path

    store_path = get_operational_data_path() / "cron" / "jobs.json"
    service = CronService(store_path)

    async def run() -> bool:
        return await service.run_job(job_id, force=force)

    if asyncio.run(run()):
        console.print("[green]✓[/green] Job executed")
    else:
        console.print(f"[red]Failed to run job {job_id}[/red]")
