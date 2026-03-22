"""CLI commands for yeoman-overseer management."""
from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import signal
from pathlib import Path

import typer

from yeoman_gateway.cli.core import app
from yeoman_shared.utils.helpers import get_logs_path, get_operational_data_path, get_run_path

overseer_app = typer.Typer(help="Manage the yeoman overseer service.")
app.add_typer(overseer_app, name="overseer")


def _data_dir() -> Path:
    return get_operational_data_path() / "overseer"


def _pid_path() -> Path:
    return get_run_path() / "overseer.pid"


def _sock_path() -> Path:
    return get_run_path() / "overseer.sock"


def _overseer_log_path() -> Path:
    return get_logs_path() / "overseer.log"


def _lock_path() -> Path:
    return get_run_path() / "overseer.lock"


@overseer_app.command()
def start(
    foreground: bool = typer.Option(False, "--foreground", "-f", help="Run in foreground"),
) -> None:
    """Start the overseer service."""
    from yeoman_overseer.service import OverseerService, OverseerConfig

    # Acquire exclusive flock — held for process lifetime, released on exit
    lock_file = _lock_path()
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = open(lock_file, "w")  # noqa: SIM115 — intentionally kept open
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        pid_path = _pid_path()
        pid_info = ""
        if pid_path.exists():
            try:
                pid_info = f" (PID {pid_path.read_text().strip()})"
            except Exception:
                pass
        typer.echo(f"Overseer already running{pid_info}")
        lock_fd.close()
        raise typer.Exit(1)

    pid_path = _pid_path()

    log_path = _overseer_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_path),
            *([] if not foreground else [logging.StreamHandler()]),
        ],
    )

    config = OverseerConfig()
    service = OverseerService(
        data_dir=_data_dir(),
        socket_path=_sock_path(),
        config=config,
    )

    async def _run() -> None:
        await service.init()
        pid_path.parent.mkdir(parents=True, exist_ok=True)
        pid_path.write_text(str(os.getpid()))
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, service.request_stop)
        try:
            await service.run()
        finally:
            pid_path.unlink(missing_ok=True)

    typer.echo("Starting overseer...")
    asyncio.run(_run())


@overseer_app.command()
def stop() -> None:
    """Stop the overseer service."""
    pid_path = _pid_path()
    if not pid_path.exists():
        typer.echo("Overseer is not running")
        raise typer.Exit(1)
    try:
        pid = int(pid_path.read_text().strip())
        os.kill(pid, signal.SIGTERM)
        typer.echo(f"Sent SIGTERM to overseer (PID {pid})")
    except ProcessLookupError:
        typer.echo("Overseer process not found, cleaning up PID file")
        pid_path.unlink(missing_ok=True)
    except ValueError:
        typer.echo("Invalid PID file")
        pid_path.unlink(missing_ok=True)


@overseer_app.command()
def status() -> None:
    """Show overseer status."""
    pid_path = _pid_path()
    state_path = _data_dir() / "state.json"

    if pid_path.exists():
        try:
            pid = int(pid_path.read_text().strip())
            os.kill(pid, 0)
            typer.echo(f"Overseer: running (PID {pid})")
        except (ProcessLookupError, ValueError):
            typer.echo("Overseer: not running (stale PID file)")
    else:
        typer.echo("Overseer: not running")

    if state_path.exists():
        state = json.loads(state_path.read_text())
        typer.echo(f"Last heartbeat: {state.get('heartbeat_ts', 'never')}")
        budget = state.get("budget", {})
        typer.echo(f"Budget: actions/hr={budget.get('actions_hour', 0)}, llm/day={budget.get('llm_daily', 0)}")


@overseer_app.command()
def runbooks() -> None:
    """List loaded runbooks."""
    from yeoman_overseer.runbook.parser import parse_runbook_dir

    runbook_dir = _data_dir() / "runbooks"
    if not runbook_dir.is_dir():
        typer.echo("No runbooks directory found")
        raise typer.Exit(1)

    rbs = parse_runbook_dir(runbook_dir)
    if not rbs:
        typer.echo("No runbooks found")
        return

    for rb in rbs:
        rb_status = "enabled" if rb.meta.enabled else "disabled"
        typer.echo(f"  {rb.meta.name:30s} {rb.meta.domain:12s} {rb.meta.trigger.kind:6s} {rb_status}")


@overseer_app.command()
def install_units() -> None:
    """Install systemd user units for overseer, gateway, and bridge."""
    import shutil
    from yeoman_overseer import __file__ as overseer_pkg
    src_dir = Path(overseer_pkg).parent / "systemd"
    dest_dir = Path.home() / ".config" / "systemd" / "user"
    dest_dir.mkdir(parents=True, exist_ok=True)
    for unit_file in src_dir.glob("*.service"):
        shutil.copy2(unit_file, dest_dir / unit_file.name)
        typer.echo(f"Installed {unit_file.name}")
    typer.echo("\nRun: systemctl --user daemon-reload")
    typer.echo("Then: systemctl --user enable --now yeoman-overseer")
