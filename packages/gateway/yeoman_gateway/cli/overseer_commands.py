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
from yeoman_shared.utils.helpers import (
    get_logs_path,
    get_operational_store_path,
    get_run_path,
    get_yeoman_home,
)

from yeoman_gateway.cli.core import app

overseer_app = typer.Typer(help="Manage the yeoman overseer service.")
app.add_typer(overseer_app, name="overseer")


def _data_dir() -> Path:
    return get_operational_store_path("overseer")


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
    from yeoman_overseer.service import OverseerConfig, OverseerService

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
    from logging.handlers import RotatingFileHandler

    handlers: list[logging.Handler] = [
        RotatingFileHandler(log_path, maxBytes=10_000_000, backupCount=3),
    ]
    if foreground:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
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
def restart(
    foreground: bool = typer.Option(False, "--foreground", "-f", help="Run in foreground"),
) -> None:
    """Restart the overseer service (stop + start)."""
    import time

    pid_path = _pid_path()
    if pid_path.exists():
        try:
            pid = int(pid_path.read_text().strip())
            os.kill(pid, signal.SIGTERM)
            typer.echo(f"Stopping overseer (PID {pid})...")
            for _ in range(50):  # wait up to 5s
                time.sleep(0.1)
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
            else:
                typer.echo("Overseer did not stop in time, proceeding anyway")
            pid_path.unlink(missing_ok=True)
        except ProcessLookupError:
            pid_path.unlink(missing_ok=True)
        except ValueError:
            pid_path.unlink(missing_ok=True)

    start(foreground=foreground)


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
        except ProcessLookupError, ValueError:
            typer.echo("Overseer: not running (stale PID file)")
    else:
        typer.echo("Overseer: not running")

    if state_path.exists():
        state = json.loads(state_path.read_text())
        typer.echo(f"Last heartbeat: {state.get('heartbeat_ts', 'never')}")
        budget = state.get("budget", {})
        typer.echo(
            f"Budget: actions/hr={budget.get('actions_hour', 0)}, llm/day={budget.get('llm_daily', 0)}"
        )


_MANAGED_UNITS = ("yeoman-gateway", "yeoman-bridge", "yeoman-overseer")


def _unit_state(unit: str) -> str:
    import subprocess

    try:
        proc = subprocess.run(
            ["systemctl", "--user", "is-active", f"{unit}.service"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"unknown ({exc.__class__.__name__})"
    return proc.stdout.strip() or "unknown"


def _describe_hold(record: object) -> str:
    from datetime import datetime

    until = getattr(record, "until", None)
    when = "no expiry" if until is None else f"until {datetime.fromtimestamp(until):%Y-%m-%d %H:%M}"
    reason = getattr(record, "reason", "") or "no reason"
    return f"{when}; {reason}; set by {getattr(record, 'set_by', '') or '?'}"


def _systemctl(verb: str, unit: str) -> None:
    import subprocess

    proc = subprocess.run(
        ["systemctl", "--user", verb, unit], capture_output=True, text=True, timeout=60, check=False
    )
    if proc.returncode != 0:
        typer.echo(f"systemctl {verb} {unit} failed: {proc.stderr.strip()}", err=True)
        raise typer.Exit(1)


def _hold(service: str, desired: str, *, for_: str | None, reason: str) -> None:
    import time

    from yeoman_shared.desired_state import (
        DesiredStateStore,
        canonical_service,
        parse_duration,
        systemd_unit,
    )

    key = canonical_service(service)
    until = None
    if for_:
        try:
            until = time.time() + parse_duration(for_)
        except ValueError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(2) from None
    record = DesiredStateStore().set(key, desired, until=until, reason=reason, set_by="cli")  # type: ignore[arg-type]
    unit = systemd_unit(key)
    if unit is not None:
        _systemctl("stop", unit)
    typer.echo(f"{key}: desired={desired} ({_describe_hold(record)})")


@overseer_app.command()
def pause(
    service: str = typer.Argument(..., help="bridge, gateway, telegram or a unit name"),
    for_: str | None = typer.Option(
        None, "--for", help="Duration such as 30m, 12h, 2d; default no expiry"
    ),
    reason: str = typer.Option("paused by owner", "--reason"),
) -> None:
    """Pause a service: stop it and keep the Overseer from restarting it."""
    _hold(service, "paused", for_=for_, reason=reason)


@overseer_app.command("stop-service")
def stop_service(
    service: str = typer.Argument(..., help="bridge, gateway, telegram or a unit name"),
    reason: str = typer.Option("stopped by owner", "--reason"),
) -> None:
    """Stop a service with no expiry; the Overseer never starts it."""
    _hold(service, "stopped", for_=None, reason=reason)


@overseer_app.command()
def resume(
    service: str = typer.Argument(..., help="bridge, gateway, telegram or a unit name"),
) -> None:
    """Hand a paused or stopped service back to the Overseer and start it."""
    from yeoman_shared.desired_state import DesiredStateStore, canonical_service, systemd_unit

    key = canonical_service(service)
    DesiredStateStore().set(key, "running", set_by="cli")
    unit = systemd_unit(key)
    if key == "yeoman-bridge":
        # Resume is the owner's go-ahead: clear the bridge's spent reconnect budget or
        # logout hold, then restart so the bridge reads it fresh.
        _reset_bridge_connect_budget()
        _systemctl("restart", unit or "yeoman-bridge.service")
    elif unit is not None:
        _systemctl("start", unit)
    typer.echo(f"{key}: desired=running")


def _reset_bridge_connect_budget() -> None:
    path = get_yeoman_home() / "data" / "ops" / "whatsapp-connect-budget.json"
    if path.exists():
        path.write_text(json.dumps({"attempts": []}), encoding="utf-8")


@overseer_app.command()
def services() -> None:
    """Show each managed unit's state and the WhatsApp link's last disconnect (read-only)."""
    from yeoman_shared.desired_state import DesiredStateStore
    from yeoman_shared.disconnect_class import classify_disconnect

    from yeoman_gateway.cli.bridge_client import bridge_command

    store = DesiredStateStore()
    for unit in (*_MANAGED_UNITS, "yeoman-gateway/telegram"):
        record = store.get(unit)
        actual = _unit_state(unit) if "/" not in unit else "-"
        line = f"{unit:24s} desired={record.desired:8s} actual={actual}"
        if record.desired != "running":
            line += f" ({_describe_hold(record)})"
            if actual == "active":
                line += " DRIFT: running although held"
        typer.echo(line)

    try:
        health = bridge_command("health", timeout_s=3.0)
    except Exception as exc:
        typer.echo(f"whatsapp                 bridge unreachable ({exc})")
        return
    wa = health.get("whatsapp") if isinstance(health.get("whatsapp"), dict) else {}
    line = (
        f"whatsapp                 connected={bool(wa.get('connected'))} "
        f"reconnectAttempts={wa.get('reconnectAttempts', 0)}"
    )
    code = wa.get("lastDisconnectStatus")
    if isinstance(code, int):
        reason, cls = classify_disconnect("whatsapp", code)
        line += f" lastDisconnect={code} ({reason}, {cls})"
    typer.echo(line)


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
        typer.echo(
            f"  {rb.meta.name:30s} {rb.meta.domain:12s} {rb.meta.trigger.kind:6s} {rb_status}"
        )


@overseer_app.command()
def trigger(
    name: str = typer.Argument(help="Runbook name (e.g. ops-source-cleanup)"),
    dry: bool = typer.Option(False, "--dry", help="Validate only, do not execute"),
) -> None:
    """Manually trigger a runbook outside of its cron schedule."""
    from yeoman_overseer.runbook.parser import parse_runbook

    runbook_dir = _data_dir() / "runbooks"
    path = runbook_dir / f"{name}.md"
    if not path.exists():
        typer.echo(f"Runbook not found: {path}")
        raise typer.Exit(1)

    runbook = parse_runbook(path)
    typer.echo(f"Runbook: {runbook.meta.name} (domain={runbook.meta.domain})")

    if dry:
        typer.echo("Dry-run: parse OK, trigger valid.")
        return

    async def _run() -> None:
        import json as _json

        from yeoman_overseer.agent.budget import BudgetTracker
        from yeoman_overseer.agent.loop import AgentLoop, BudgetExhaustedError
        from yeoman_overseer.agent.tools import ToolContext
        from yeoman_overseer.audit.git import InternalGit
        from yeoman_overseer.audit.logger import AuditLogger
        from yeoman_overseer.comms.cascading import CascadingComms
        from yeoman_overseer.service import OverseerConfig, OverseerService
        from yeoman_overseer.state import OverseerState

        yeoman_home = get_yeoman_home()
        data_dir = _data_dir()

        # Load .env
        OverseerService._load_dotenv()

        config_path = yeoman_home / "config.json"
        raw_config = _json.loads(config_path.read_text()) if config_path.exists() else {}
        policy_path = yeoman_home / "policy.json"
        raw_policy = _json.loads(policy_path.read_text()) if policy_path.exists() else {}

        git = InternalGit(data_dir)
        git.init()
        audit = AuditLogger(data_dir / "audit")
        state = OverseerState.load(data_dir / "state.json")

        channels = OverseerService._build_comms_channels(raw_config, raw_policy)
        comms = CascadingComms(channels=channels, local_log=True)

        sandbox = OverseerService._create_sandbox()

        tool_ctx = ToolContext(
            yeoman_home=yeoman_home,
            source_dir=Path.home() / "Documents" / "yeoman",
            audit=audit,
            comms=comms,
            data_dir=data_dir,
            sandbox=sandbox,
            memory_db=Path(
                raw_config.get("memory", {}).get("dbPath")
                or raw_config.get("memory", {}).get("db_path")
                or yeoman_home / "data" / "memory" / "memory.db"
            ).expanduser(),
            git=git,
            runbook_name=runbook.meta.name,
            domain=runbook.meta.domain,
            shell_timeout_s=runbook.meta.safety.shell_timeout_s,
        )

        config = OverseerConfig()
        budget = BudgetTracker(
            state, calls_per_day=config.llm_calls_per_day, tokens_per_day=config.llm_tokens_per_day
        )
        agent = AgentLoop(tool_ctx=tool_ctx, budget=budget, config=raw_config)

        typer.echo(f"Triggering {runbook.meta.name}...")
        try:
            result = await agent.run(runbook, {"check": True, "message": "manual trigger"})
            typer.echo(f"\n{'=' * 60}")
            typer.echo(f"Result: {runbook.meta.name}")
            typer.echo(f"  Tokens: {result.tokens_used}")
            typer.echo(f"  Tool calls: {result.tool_calls_made}")
            typer.echo(f"  Profile: {result.llm_profile}")
            typer.echo(f"  Summary:\n{result.summary}")
            budget.consume(0, 0)  # ensure state is saved
            state.save(data_dir / "state.json")
        except BudgetExhaustedError as e:
            typer.echo(f"Budget exhausted: {e}")
            raise typer.Exit(1)
        except Exception as e:
            typer.echo(f"Error: {e}")
            raise typer.Exit(1)

    asyncio.run(_run())


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
