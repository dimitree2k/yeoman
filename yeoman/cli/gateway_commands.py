"""Gateway process commands and helpers."""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer

from yeoman import __logo__
from yeoman.utils.process import command_for_pid, pid_alive, read_pid_file, signal_pid

from .core import app, console, make_policy_engine, make_provider


def _gateway_pid_path() -> Path:
    from yeoman.utils.helpers import get_run_path

    return get_run_path() / "gateway.pid"


def _gateway_log_path() -> Path:
    from yeoman.utils.helpers import get_logs_path

    return get_logs_path() / "gateway.log"


def _pid_has_env(pid: int, key: str, value: str | None = None) -> bool:
    env_path = Path(f"/proc/{pid}/environ")
    try:
        raw = env_path.read_bytes()
    except OSError:
        return False

    marker = f"{key}={value}" if value is not None else f"{key}="
    for item in raw.split(b"\x00"):
        if not item:
            continue
        text = item.decode(errors="ignore")
        if value is None:
            if text.startswith(marker):
                return True
        elif text == marker:
            return True
    return False


def _gateway_cmd_port(cmd: str) -> int | None:
    import shlex

    try:
        tokens = shlex.split(cmd)
    except ValueError:
        tokens = cmd.split()

    if "gateway" not in tokens:
        return None

    resolved = 18790
    for i, tok in enumerate(tokens):
        if tok == "--port" and i + 1 < len(tokens):
            try:
                resolved = int(tokens[i + 1])
            except ValueError:
                return None
        elif tok == "-p" and i + 1 < len(tokens):
            try:
                resolved = int(tokens[i + 1])
            except ValueError:
                return None
    return resolved


def _is_yeoman_gateway_command(cmd: str) -> bool:
    import shlex

    try:
        tokens = shlex.split(cmd)
    except ValueError:
        tokens = cmd.split()

    if "gateway" not in tokens:
        return False

    if "-m" in tokens:
        i = tokens.index("-m")
        if i + 1 < len(tokens) and tokens[i + 1] == "yeoman.cli.commands":
            return True

    exe = tokens[0] if tokens else ""
    if exe == "nanobot" or exe.endswith("/nanobot"):
        return True

    if "python" in exe and len(tokens) > 1:
        script = tokens[1]
        if script == "nanobot" or script.endswith("/nanobot"):
            return True

    return False


def _is_gateway_process_on_port(pid: int, port: int) -> bool:
    import os

    if pid == os.getpid():
        return False

    cmd = command_for_pid(pid)
    if not cmd:
        return False

    if _pid_has_env(pid, "NANOBOT_GATEWAY_DAEMON", "1"):
        return _gateway_cmd_port(cmd) == port

    if not _is_yeoman_gateway_command(cmd):
        return False

    return _gateway_cmd_port(cmd) == port


def _find_gateway_pids(port: int) -> list[int]:
    import subprocess

    pids: set[int] = set()

    stored = read_pid_file(_gateway_pid_path())
    if stored is not None and pid_alive(stored) and _is_gateway_process_on_port(stored, port):
        pids.add(stored)

    result = subprocess.run(
        ["ps", "-eo", "pid=,command="],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return sorted(pids)

    for line in result.stdout.splitlines():
        text = line.strip()
        if not text:
            continue
        parts = text.split(maxsplit=1)
        if not parts:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        if pid_alive(pid) and _is_gateway_process_on_port(pid, port):
            pids.add(pid)

    return sorted(pids)


def _stop_gateway_processes(port: int, timeout_s: float = 10.0) -> int:
    import signal
    import time

    pids = _find_gateway_pids(port)
    if not pids:
        _gateway_pid_path().unlink(missing_ok=True)
        return 0

    for pid in pids:
        signal_pid(pid, signal.SIGTERM)

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        remaining = [pid for pid in pids if pid_alive(pid)]
        listeners = _find_gateway_pids(port)
        if not remaining and not listeners:
            break
        time.sleep(0.2)
    else:
        for pid in [pid for pid in pids if pid_alive(pid)]:
            signal_pid(pid, signal.SIGKILL)
        for pid in _find_gateway_pids(port):
            signal_pid(pid, signal.SIGKILL)

    _gateway_pid_path().unlink(missing_ok=True)
    return len(pids)


def _start_gateway_daemon(port: int, verbose: bool, ensure_whatsapp: bool = True) -> None:
    import os
    import subprocess
    import sys
    import time

    from yeoman.channels.whatsapp_runtime import WhatsAppRuntimeManager
    from yeoman.config.loader import load_config

    config = load_config()
    if ensure_whatsapp and config.channels.whatsapp.enabled:
        runtime = WhatsAppRuntimeManager(config=config)
        try:
            runtime.ensure_ready(
                auto_repair=config.channels.whatsapp.bridge_auto_repair,
                start_if_needed=True,
            )
        except Exception as e:
            console.print(f"[red]WhatsApp ensure failed:[/red] {e}")
            raise typer.Exit(1)

    running = _find_gateway_pids(port)
    if running:
        console.print(f"[yellow]Gateway already running (pid {running[0]})[/yellow]")
        console.print(f"Log: {_gateway_log_path()}")
        return

    log_path = _gateway_log_path()
    cmd = [sys.executable, "-m", "yeoman.cli.commands", "gateway", "--port", str(port)]
    if verbose:
        cmd.append("--verbose")

    with open(log_path, "a") as log_file:
        env = dict(os.environ)
        env["NANOBOT_GATEWAY_DAEMON"] = "1"
        proc = subprocess.Popen(
            cmd,
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )

    started = False
    for _ in range(20):
        if proc.poll() is not None:
            break
        if proc.pid in _find_gateway_pids(port):
            started = True
            break
        time.sleep(0.2)

    if not started:
        console.print("[red]Gateway failed to start. Check log:[/red]")
        console.print(log_path)
        raise typer.Exit(1)

    _gateway_pid_path().write_text(str(proc.pid))
    console.print(f"[green]✓[/green] Gateway started (pid {proc.pid}, port {port})")
    console.print(f"Log: {log_path}")


def _run_gateway_foreground(port: int, verbose: bool, ensure_whatsapp: bool = True) -> None:
    """Start the yeoman gateway in foreground."""
    from yeoman.app.bootstrap import build_gateway_runtime
    from yeoman.bus.queue import MessageBus
    from yeoman.channels.whatsapp_runtime import WhatsAppRuntimeManager
    from yeoman.config.loader import load_config

    if verbose:
        import logging

        logging.basicConfig(level=logging.DEBUG)

    console.print(f"{__logo__} Starting yeoman gateway on port {port}...")

    config = load_config()
    if ensure_whatsapp and config.channels.whatsapp.enabled:
        runtime = WhatsAppRuntimeManager(config=config)
        runtime.ensure_ready(
            auto_repair=config.channels.whatsapp.bridge_auto_repair,
            start_if_needed=True,
        )
    bus = MessageBus(
        inbound_maxsize=config.bus.inbound_maxsize,
        outbound_maxsize=config.bus.outbound_maxsize,
    )
    provider = make_provider(config)
    policy_engine, policy_path = make_policy_engine(config)
    runtime = build_gateway_runtime(
        config=config,
        provider=provider,
        policy_engine=policy_engine,
        policy_path=policy_path,
        workspace=config.workspace_path,
        bus=bus,
    )

    if runtime.channels.enabled_channels:
        console.print(
            f"[green]✓[/green] Channels enabled: {', '.join(runtime.channels.enabled_channels)}"
        )
    else:
        console.print("[yellow]Warning: No channels enabled[/yellow]")

    cron_status = runtime.cron.status()
    if cron_status["jobs"] > 0:
        console.print(f"[green]✓[/green] Cron: {cron_status['jobs']} scheduled jobs")

    console.print("[green]✓[/green] Heartbeat: every 30m")

    async def run() -> None:
        try:
            await runtime.run()
        except KeyboardInterrupt:
            console.print("\nShutting down...")
            runtime.heartbeat.stop()
            runtime.cron.stop()
            runtime.orchestrator.stop()
            await runtime.channels.stop_all()

    asyncio.run(run())


@app.command()
def gateway(
    action: str | None = typer.Argument(
        None,
        help="Action: start|stop|restart|status (default: run in foreground)",
    ),
    port: int = typer.Option(18790, "--port", "-p", help="Gateway port"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
    daemon: bool = typer.Option(False, "--daemon", help="Run gateway in background"),
    stop: bool = typer.Option(False, "--stop", help="Stop gateway"),
    restart: bool = typer.Option(False, "--restart", help="Restart gateway in background"),
    status: bool = typer.Option(False, "--status", help="Show gateway status"),
    ensure_whatsapp: bool = typer.Option(
        True,
        "--ensure-whatsapp/--no-ensure-whatsapp",
        help="Ensure WhatsApp runtime/health before starting gateway",
    ),
) -> None:
    """Start or control the yeoman gateway."""
    action_mode: str | None = None
    if action:
        normalized = action.strip().lower()
        if normalized in {"start", "stop", "restart", "status"}:
            action_mode = normalized
        else:
            console.print("[red]Invalid action. Use: start, stop, restart, status[/red]")
            raise typer.Exit(1)

    selected_modes: list[str] = []
    if daemon:
        selected_modes.append("start")
    if stop:
        selected_modes.append("stop")
    if restart:
        selected_modes.append("restart")
    if status:
        selected_modes.append("status")
    if action_mode:
        selected_modes.append(action_mode)

    resolved_modes = sorted(set(selected_modes))
    if len(resolved_modes) > 1:
        console.print("[red]Use one control mode only (start/stop/restart/status)[/red]")
        raise typer.Exit(1)
    mode = resolved_modes[0] if resolved_modes else None

    if mode == "status":
        running = _find_gateway_pids(port)
        if not running:
            console.print(f"[yellow]Gateway not running on port {port}[/yellow]")
            return
        console.print(f"[green]Gateway running[/green] on port {port} (pid {running[0]})")
        console.print(f"Log: {_gateway_log_path()}")
        return

    if mode == "stop":
        stopped = _stop_gateway_processes(port)
        if stopped == 0:
            console.print("[yellow]Gateway is not running[/yellow]")
            return
        console.print(
            f"[green]✓[/green] Gateway stopped ({stopped} process{'es' if stopped != 1 else ''})"
        )
        return

    if mode == "restart":
        import time

        _stop_gateway_processes(port)
        time.sleep(0.2)
        if _find_gateway_pids(port):
            console.print(f"[red]Gateway restart failed: port {port} is still in use[/red]")
            raise typer.Exit(1)
        _start_gateway_daemon(port, verbose, ensure_whatsapp=ensure_whatsapp)
        return

    if mode == "start":
        _start_gateway_daemon(port, verbose, ensure_whatsapp=ensure_whatsapp)
        return

    _run_gateway_foreground(port, verbose, ensure_whatsapp=ensure_whatsapp)
