"""Channel-related CLI commands."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import typer
from rich.table import Table

from yeoman import __logo__

from .core import app, console
from .gateway_commands import _find_gateway_pids, _start_gateway_daemon, _stop_gateway_processes

channels_app = typer.Typer(help="Manage channels")
app.add_typer(channels_app, name="channels")


@channels_app.command("status")
def channels_status() -> None:
    """Show channel status."""
    from yeoman.config.loader import load_config

    config = load_config()

    table = Table(title="Channel Status")
    table.add_column("Channel", style="cyan")
    table.add_column("Enabled", style="green")
    table.add_column("Configuration", style="yellow")

    wa = config.channels.whatsapp
    table.add_row(
        "WhatsApp",
        "✓" if wa.enabled else "✗",
        f"{wa.resolved_bridge_url} (host={wa.bridge_host}, port={wa.resolved_bridge_port})",
    )

    dc = config.channels.discord
    table.add_row("Discord", "✓" if dc.enabled else "✗", dc.gateway_url)

    tg = config.channels.telegram
    tg_config = f"token: {tg.token[:10]}..." if tg.token else "[dim]not configured[/dim]"
    table.add_row("Telegram", "✓" if tg.enabled else "✗", tg_config)

    console.print(table)


whatsapp_app = typer.Typer(help="Manage WhatsApp channel runtime")
channels_app.add_typer(whatsapp_app, name="whatsapp")


@whatsapp_app.command("ensure")
def whatsapp_ensure(
    no_auto_repair: bool = typer.Option(
        False,
        "--no-auto-repair",
        help="Disable one-shot auto-repair on health/protocol mismatch",
    ),
) -> None:
    """Ensure WhatsApp runtime, bridge process and protocol health are ready."""
    from yeoman.channels.whatsapp_runtime import WhatsAppRuntimeManager
    from yeoman.config.loader import load_config

    config = load_config()
    runtime = WhatsAppRuntimeManager(config=config)
    try:
        report = runtime.ensure_ready(
            auto_repair=False if no_auto_repair else config.channels.whatsapp.bridge_auto_repair,
            start_if_needed=True,
        )
    except Exception as e:
        console.print(f"[red]WhatsApp ensure failed:[/red] {e}")
        raise typer.Exit(1)

    status_bits = []
    if report.started:
        status_bits.append("started bridge")
    if report.repaired:
        status_bits.append("auto-repaired")
    status_suffix = f" ({', '.join(status_bits)})" if status_bits else ""
    console.print(
        f"[green]✓[/green] WhatsApp runtime ready{status_suffix}: "
        f"bridge pid {report.status.pids[0]} port {report.status.port}"
    )
    console.print(f"Runtime: {report.runtime_dir}")
    console.print(f"Log: {report.status.log_path}")


@whatsapp_app.command("repair-sender")
def whatsapp_repair_sender(
    sender_id: str = typer.Option(
        ..., "--sender-id", help="Sender numeric id (e.g. 34596062240904)"
    ),
    chat_id: str | None = typer.Option(
        None,
        "--chat-id",
        help="Optional chat id scope (e.g. 120363...@g.us). When set, sender-key cleanup is limited to this chat.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Preview matching auth files without deleting"
    ),
    restart_bridge: bool = typer.Option(
        True,
        "--restart-bridge/--no-restart-bridge",
        help="Restart WhatsApp bridge after cleanup",
    ),
    restart_gateway: bool = typer.Option(
        True,
        "--restart-gateway/--no-restart-gateway",
        help="Restart gateway if it is currently running",
    ),
) -> None:
    """Repair WhatsApp decrypt issues for one sender by resetting sender/session auth artifacts."""
    import shutil

    from yeoman.channels.whatsapp_runtime import WhatsAppRuntimeManager
    from yeoman.config.loader import load_config

    raw_sender = (sender_id or "").strip()
    sender_token = raw_sender.split("@", 1)[0].split(":", 1)[0].strip()
    if not sender_token.isdigit():
        console.print("[red]Invalid --sender-id. Use numeric sender id (digits only).[/red]")
        raise typer.Exit(1)

    chat_scope = (chat_id or "").strip()
    if chat_scope and "@" not in chat_scope:
        console.print("[red]Invalid --chat-id. Expected WhatsApp JID like ...@g.us[/red]")
        raise typer.Exit(1)

    config = load_config()
    auth_dir = Path(config.channels.whatsapp.auth_dir).expanduser()
    if not auth_dir.exists():
        console.print(f"[red]Auth dir not found:[/red] {auth_dir}")
        raise typer.Exit(1)

    names = [p.name for p in auth_dir.glob("*.json")]
    targets: list[str] = []
    for name in names:
        if name == f"device-list-{sender_token}.json":
            targets.append(name)
            continue
        if name.startswith(f"lid-mapping-{sender_token}") and name.endswith(".json"):
            targets.append(name)
            continue
        if name.startswith(f"session-{sender_token}_1.") and name.endswith(".json"):
            targets.append(name)
            continue
        if name.startswith("sender-key-") and name.endswith(".json"):
            if f"--{sender_token}_1--" not in name:
                continue
            if chat_scope and not name.startswith(f"sender-key-{chat_scope}--"):
                continue
            targets.append(name)

    if chat_scope:
        memory_name = f"sender-key-memory-{chat_scope}.json"
        if (auth_dir / memory_name).exists():
            targets.append(memory_name)

    targets = sorted(set(targets))
    if not targets:
        console.print("[yellow]No matching auth artifacts found for that sender.[/yellow]")
        return

    if dry_run:
        console.print(f"[yellow]Dry run:[/yellow] would remove {len(targets)} file(s):")
        for name in targets:
            console.print(f"  - {name}")
        return

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = auth_dir / f"repair-backup-sender-{sender_token}-{stamp}"
    backup_dir.mkdir(parents=True, exist_ok=True)

    removed = 0
    for name in targets:
        src = auth_dir / name
        if not src.exists():
            continue
        shutil.copy2(src, backup_dir / name)
        src.unlink(missing_ok=True)
        removed += 1

    console.print(f"[green]✓[/green] Backed up and removed {removed} file(s)")
    console.print(f"Backup: {backup_dir}")

    runtime = WhatsAppRuntimeManager(config=config)
    if restart_bridge:
        try:
            status = runtime.restart_bridge()
            console.print(
                f"[green]✓[/green] Bridge restarted (pid {status.pids[0]}, port {status.port})"
            )
        except Exception as e:
            console.print(f"[red]Bridge restart failed:[/red] {e}")
            raise typer.Exit(1)

    if restart_gateway:
        gateway_port = config.gateway.port
        if _find_gateway_pids(gateway_port):
            _stop_gateway_processes(gateway_port)
            _start_gateway_daemon(gateway_port, verbose=False)
        else:
            console.print(
                f"[dim]Gateway not running on port {gateway_port}; skipped restart.[/dim]"
            )


def _get_bridge_dir() -> Path:
    """Get the prepared user bridge runtime directory."""
    try:
        from yeoman.channels.whatsapp_runtime import WhatsAppRuntimeManager

        runtime = WhatsAppRuntimeManager()
        return runtime.ensure_runtime()
    except Exception as e:
        console.print(f"[red]Failed to prepare WhatsApp bridge runtime:[/red] {e}")
        raise typer.Exit(1)


def _ensure_whatsapp_bridge_token(config=None, *, quiet: bool = False) -> str:
    """Ensure channels.whatsapp.bridgeToken exists, generating and saving if missing."""
    try:
        from yeoman.channels.whatsapp_runtime import WhatsAppRuntimeManager

        runtime = WhatsAppRuntimeManager(config=config)
        return runtime.ensure_bridge_token(quiet=quiet)
    except Exception as e:
        console.print(f"[red]Failed to ensure generated bridge token:[/red] {e}")
        raise typer.Exit(1)


def _rotate_whatsapp_bridge_token(config=None) -> tuple[str, str]:
    """Rotate channels.whatsapp.bridgeToken and persist it."""
    try:
        from yeoman.channels.whatsapp_runtime import WhatsAppRuntimeManager

        runtime = WhatsAppRuntimeManager(config=config)
        old_token, new_token = runtime.rotate_bridge_token()
    except Exception as e:
        console.print(f"[red]Failed to save rotated bridge token:[/red] {e}")
        raise typer.Exit(1)

    from yeoman.config.loader import get_config_path

    console.print(
        f"[green]✓[/green] Rotated channels.whatsapp.bridgeToken and saved to {get_config_path()}"
    )
    return old_token, new_token


@channels_app.command("login")
def channels_login() -> None:
    """Link device via QR code."""
    import os
    import subprocess

    from yeoman.config.loader import load_config

    config = load_config()
    wa = config.channels.whatsapp
    token = _ensure_whatsapp_bridge_token(config=config)

    bridge_dir = _get_bridge_dir()

    console.print(f"{__logo__} Starting bridge...")
    console.print("Scan the QR code to connect.\n")

    try:
        env = dict(os.environ)
        env["BRIDGE_PORT"] = str(wa.bridge_port or wa.resolved_bridge_port)
        env["BRIDGE_HOST"] = wa.bridge_host
        env["BRIDGE_TOKEN"] = token
        env["AUTH_DIR"] = str(Path(wa.auth_dir).expanduser())
        subprocess.run(["node", "dist/index.js"], cwd=bridge_dir, check=True, env=env)
    except subprocess.CalledProcessError as e:
        console.print(f"[red]Bridge failed: {e}[/red]")
    except FileNotFoundError:
        console.print("[red]node not found. Please install Node.js >= 20.[/red]")


bridge_app = typer.Typer(help="Manage WhatsApp bridge process")
channels_app.add_typer(bridge_app, name="bridge")


def _bridge_runtime(port: int | None):
    from yeoman.channels.whatsapp_runtime import WhatsAppRuntimeManager
    from yeoman.config.loader import load_config

    config = load_config()
    runtime = WhatsAppRuntimeManager(config=config)
    resolved_port = config.channels.whatsapp.resolved_bridge_port if port is None else port
    return runtime, config, resolved_port


def _print_bridge_started(status, *, action: str) -> None:
    console.print(f"[green]✓[/green] Bridge {action} (pid {status.pids[0]}, port {status.port})")
    console.print(f"Log: {status.log_path}")


def _bridge_log_path() -> Path:
    from yeoman.utils.helpers import get_logs_path

    return get_logs_path() / "whatsapp-bridge.log"


@bridge_app.command("start")
def bridge_start(
    port: int = typer.Option(
        None, "--port", "-p", help="Bridge port (default: from config bridge_url)"
    ),
) -> None:
    """Start WhatsApp bridge in background."""
    runtime, _, resolved_port = _bridge_runtime(port)
    status_before = runtime.status_bridge(resolved_port)
    if status_before.running:
        console.print(f"[yellow]Bridge already running (pid {status_before.pids[0]})[/yellow]")
        console.print(f"Log: {status_before.log_path}")
        return

    try:
        status = runtime.start_bridge(resolved_port)
    except Exception as e:
        console.print(f"[red]Bridge failed to start:[/red] {e}")
        console.print(f"Log: {runtime.bridge_log_path}")
        raise typer.Exit(1)

    _print_bridge_started(status, action="started")


@bridge_app.command("stop")
def bridge_stop(
    port: int = typer.Option(
        None, "--port", "-p", help="Bridge port (default: from config bridge_url)"
    ),
) -> None:
    """Stop WhatsApp bridge."""
    runtime, _, resolved_port = _bridge_runtime(port)
    stopped = runtime.stop_bridge(resolved_port)
    if stopped == 0:
        console.print("[yellow]Bridge is not running[/yellow]")
        return
    console.print(
        f"[green]✓[/green] Bridge stopped ({stopped} process{'es' if stopped != 1 else ''})"
    )


@bridge_app.command("restart")
def bridge_restart(
    port: int = typer.Option(
        None, "--port", "-p", help="Bridge port (default: from config bridge_url)"
    ),
) -> None:
    """Restart WhatsApp bridge."""
    runtime, _, resolved_port = _bridge_runtime(port)
    try:
        status = runtime.restart_bridge(resolved_port)
    except Exception as e:
        console.print(f"[red]Bridge restart failed:[/red] {e}")
        raise typer.Exit(1)
    _print_bridge_started(status, action="restarted")


@bridge_app.command("status")
def bridge_status(
    port: int = typer.Option(
        None, "--port", "-p", help="Bridge port (default: from config bridge_url)"
    ),
) -> None:
    """Show WhatsApp bridge status."""
    runtime, _, resolved_port = _bridge_runtime(port)
    status = runtime.status_bridge(resolved_port)
    if not status.running:
        console.print(f"[yellow]Bridge not running on port {resolved_port}[/yellow]")
        return
    console.print(f"[green]Bridge running[/green] on port {status.port} (pid {status.pids[0]})")
    console.print(f"Log: {status.log_path}")


@bridge_app.command("rotate")
@bridge_app.command("rotate-token")
def bridge_rotate_token(
    port: int = typer.Option(
        None, "--port", "-p", help="Bridge port (default: from config bridge_url)"
    ),
    restart_bridge: bool = typer.Option(
        True,
        "--restart-bridge/--no-restart-bridge",
        help="Restart bridge if it is currently running",
    ),
    restart_gateway: bool = typer.Option(
        True,
        "--restart-gateway/--no-restart-gateway",
        help="Restart gateway if it is currently running",
    ),
    start_bridge_if_stopped: bool = typer.Option(
        False,
        "--start-bridge-if-stopped",
        help="Also start bridge when it is not currently running",
    ),
    start_gateway_if_stopped: bool = typer.Option(
        False,
        "--start-gateway-if-stopped",
        help="Also start gateway when it is not currently running",
    ),
) -> None:
    """Rotate WhatsApp bridge token and restart affected processes."""
    runtime, config, resolved_port = _bridge_runtime(port)
    gateway_port = config.gateway.port

    bridge_running = runtime.status_bridge(resolved_port).running
    gateway_running = bool(_find_gateway_pids(gateway_port))

    old_token, new_token = _rotate_whatsapp_bridge_token(config=config)
    if old_token and old_token == new_token:
        console.print("[yellow]Token value did not change unexpectedly; retry rotation.[/yellow]")
        raise typer.Exit(1)

    if restart_bridge:
        if bridge_running:
            runtime.restart_bridge(resolved_port)
            status = runtime.status_bridge(resolved_port)
            _print_bridge_started(status, action="restarted")
        elif start_bridge_if_stopped:
            status = runtime.start_bridge(resolved_port)
            _print_bridge_started(status, action="started")
        else:
            console.print(
                f"[dim]Bridge was not running on port {resolved_port}; skipped restart.[/dim]"
            )

    if restart_gateway:
        if gateway_running:
            _stop_gateway_processes(gateway_port)
            _start_gateway_daemon(gateway_port, verbose=False)
        elif start_gateway_if_stopped:
            _start_gateway_daemon(gateway_port, verbose=False)
        else:
            console.print(
                f"[dim]Gateway was not running on port {gateway_port}; skipped restart.[/dim]"
            )
