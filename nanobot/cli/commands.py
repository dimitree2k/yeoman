"""CLI commands for nanobot."""

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Literal

import typer
from rich.console import Console
from rich.table import Table

from nanobot import __logo__, __version__
from nanobot.utils.process import (
    command_for_pid,
    is_bridge_dir,
    is_bridge_process,
    listener_pids_for_port,
    pid_alive,
    read_pid_file,
    signal_pid,
    signal_process_group,
)

app = typer.Typer(
    name="nanobot",
    help=f"{__logo__} nanobot-stack (compat: nanobot) - Personal AI Assistant",
    no_args_is_help=True,
)

console = Console()


def version_callback(value: bool):
    if value:
        console.print(f"{__logo__} nanobot-stack v{__version__} (compat command: nanobot)")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(None, "--version", "-v", callback=version_callback, is_eager=True),
):
    """nanobot-stack (compat: nanobot) - Personal AI Assistant."""
    pass


# ============================================================================
# Onboard / Setup
# ============================================================================


@app.command()
def onboard():
    """Initialize nanobot-stack configuration and workspace."""
    from nanobot.config.loader import get_config_path, save_config
    from nanobot.config.schema import Config
    from nanobot.policy.loader import ensure_policy_file
    from nanobot.utils.helpers import get_workspace_path

    config_path = get_config_path()

    if config_path.exists():
        console.print(f"[yellow]Config already exists at {config_path}[/yellow]")
        if not typer.confirm("Overwrite?"):
            raise typer.Exit()

    # Create default config
    config = Config()
    save_config(config)
    console.print(f"[green]✓[/green] Created config at {config_path}")
    policy_path = ensure_policy_file()
    console.print(f"[green]✓[/green] Created policy at {policy_path}")

    # Create workspace
    workspace = get_workspace_path()
    console.print(f"[green]✓[/green] Created workspace at {workspace}")

    # Create default bootstrap files
    _create_workspace_templates(workspace)

    console.print(f"\n{__logo__} nanobot-stack is ready!")
    console.print("\nNext steps:")
    console.print("  1. Add your API key to [cyan]~/.nanobot/config.json[/cyan]")
    console.print("     Get one at: https://openrouter.ai/keys")
    console.print('  2. Chat: [cyan]nanobot agent -m "Hello!"[/cyan]')
    console.print("\n[dim]Want Telegram/WhatsApp? See project README > Chat Apps[/dim]")


def _create_workspace_templates(workspace: Path):
    """Create default workspace template files."""
    templates = {
        "AGENTS.md": """# Agent Instructions

You are a helpful AI assistant. Be concise, accurate, and friendly.

## Guidelines

- Always explain what you're doing before taking actions
- Ask for clarification when the request is ambiguous
- Use tools to help accomplish tasks
- Remember important information in your memory files
""",
        "SOUL.md": """# Soul

I am nanobot-stack, a lightweight AI assistant.

## Personality

- Helpful and friendly
- Concise and to the point
- Curious and eager to learn

## Values

- Accuracy over speed
- User privacy and safety
- Transparency in actions
""",
        "USER.md": """# User

Information about the user goes here.

## Preferences

- Communication style: (casual/formal)
- Timezone: (your timezone)
- Language: (your preferred language)
""",
    }

    for filename, content in templates.items():
        file_path = workspace / filename
        if not file_path.exists():
            file_path.write_text(content)
            console.print(f"  [dim]Created {filename}[/dim]")


def _make_provider(config):
    """Create LiteLLMProvider from config. Exits if no API key found."""
    from nanobot.media.router import ModelRouter
    from nanobot.providers.litellm_provider import LiteLLMProvider

    model = config.agents.defaults.model
    try:
        profile = ModelRouter(config.models).resolve("assistant.reply")
        if profile.model:
            model = profile.model
    except KeyError:
        pass

    p = config.get_provider(model)
    if not (p and p.api_key) and not model.startswith("bedrock/"):
        console.print("[red]Error: No API key configured.[/red]")
        console.print("Set one in ~/.nanobot/config.json under providers section")
        raise typer.Exit(1)
    return LiteLLMProvider(
        api_key=p.api_key if p else None,
        api_base=p.api_base if p else None,
        default_model=model,
        extra_headers=p.extra_headers if p else None,
    )


def _make_memory_service(config):
    """Create memory service from config/workspace."""
    from nanobot.memory import MemoryService

    return MemoryService(workspace=config.workspace_path, config=config.memory, root_config=config)


def _make_policy_engine(config):
    """Create policy engine + path from ~/.nanobot/policy.json."""
    from nanobot.policy.engine import PolicyEngine
    from nanobot.policy.loader import get_policy_path, load_policy

    try:
        policy_path = get_policy_path()
        policy = load_policy(policy_path)
        apply_channels: set[str] = set()
        if getattr(config.channels.telegram, "enabled", False):
            apply_channels.add("telegram")
        if getattr(config.channels.whatsapp, "enabled", False):
            apply_channels.add("whatsapp")
        engine = PolicyEngine(
            policy=policy,
            workspace=config.workspace_path,
            apply_channels=apply_channels,
        )
        return engine, policy_path
    except ValueError as e:
        console.print(f"[red]Policy validation error:[/red] {e}")
        raise typer.Exit(1)


# ============================================================================
# Gateway / Server
# ============================================================================


def _gateway_pid_path() -> Path:
    from nanobot.utils.helpers import get_run_path
    return get_run_path() / "gateway.pid"


def _gateway_log_path() -> Path:
    from nanobot.utils.helpers import get_logs_path
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

    # Default CLI port if omitted.
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


def _is_nanobot_gateway_command(cmd: str) -> bool:
    import shlex

    try:
        tokens = shlex.split(cmd)
    except ValueError:
        tokens = cmd.split()

    if "gateway" not in tokens:
        return False

    if "-m" in tokens:
        i = tokens.index("-m")
        if i + 1 < len(tokens) and tokens[i + 1] == "nanobot.cli.commands":
            return True

    exe = tokens[0] if tokens else ""
    if exe == "nanobot" or exe.endswith("/nanobot"):
        return True

    # Script entrypoint style: python /path/to/nanobot gateway ...
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

    if not _is_nanobot_gateway_command(cmd):
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
            p = int(parts[0])
        except ValueError:
            continue
        if pid_alive(p) and _is_gateway_process_on_port(p, port):
            pids.add(p)

    return sorted(pids)


def _stop_gateway_processes(port: int, timeout_s: float = 10.0) -> int:
    import signal
    import time

    pids = _find_gateway_pids(port)
    if not pids:
        _gateway_pid_path().unlink(missing_ok=True)
        return 0

    for p in pids:
        signal_pid(p, signal.SIGTERM)

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        remaining = [p for p in pids if pid_alive(p)]
        listeners = _find_gateway_pids(port)
        if not remaining and not listeners:
            break
        time.sleep(0.2)
    else:
        for p in [p for p in pids if pid_alive(p)]:
            signal_pid(p, signal.SIGKILL)
        for p in _find_gateway_pids(port):
            signal_pid(p, signal.SIGKILL)

    _gateway_pid_path().unlink(missing_ok=True)
    return len(pids)


def _start_gateway_daemon(port: int, verbose: bool, ensure_whatsapp: bool = True) -> None:
    import os
    import subprocess
    import sys
    import time

    from nanobot.channels.whatsapp_runtime import WhatsAppRuntimeManager
    from nanobot.config.loader import load_config
    from nanobot.utils.helpers import migrate_runtime_layout

    # Migrate old flat layout to new subdirectory layout before anything else
    # creates new-layout directories (which would block the rename-based migration).
    moved = migrate_runtime_layout()
    if moved:
        from loguru import logger
        for old in moved:
            logger.info("runtime layout migration: moved {}", old)

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
    cmd = [sys.executable, "-m", "nanobot.cli.commands", "gateway", "--port", str(port)]
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

    # Wait briefly for either process failure or process registration.
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
    """Start the nanobot gateway in foreground."""
    from nanobot.app.bootstrap import build_gateway_runtime
    from nanobot.bus.queue import MessageBus
    from nanobot.channels.whatsapp_runtime import WhatsAppRuntimeManager
    from nanobot.config.loader import load_config
    from nanobot.utils.helpers import migrate_runtime_layout

    if verbose:
        import logging

        logging.basicConfig(level=logging.DEBUG)

    # Migrate old flat layout to new subdirectory layout (one-shot, idempotent)
    moved = migrate_runtime_layout()
    if moved:
        from loguru import logger
        for old in moved:
            logger.info("runtime layout migration: moved {}", old)

    console.print(f"{__logo__} Starting nanobot gateway on port {port}...")

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
    provider = _make_provider(config)
    policy_engine, policy_path = _make_policy_engine(config)
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

    async def run():
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
):
    """Start or control the nanobot gateway."""
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


# ============================================================================
# Config Commands
# ============================================================================

config_app = typer.Typer(help="Manage nanobot configuration")
app.add_typer(config_app, name="config")


@config_app.command("migrate-to-env")
def config_migrate_to_env(
    force: bool = typer.Option(False, "--force", help="Overwrite existing .env file"),
):
    """Migrate API keys and tokens from config.json to .env file.

    Reads all secret values from config.json, writes them to ~/.nanobot/.env
    (mode 0600), then zeros out those fields in config.json so the file
    becomes safe to commit to version control.
    """
    from nanobot.config.loader import get_config_path, load_config, save_config
    from nanobot.providers.registry import PROVIDERS
    from nanobot.utils.helpers import get_data_path

    env_path = get_data_path() / ".env"
    if env_path.exists() and not force:
        console.print(f"[yellow].env already exists at {env_path}[/yellow]")
        console.print("Use --force to overwrite it.")
        raise typer.Exit(1)

    # Load config without env override so we see what's currently in the JSON.
    # We call load_config() which also loads .env, but provider keys already in
    # config.json will be present; we collect everything that is non-empty.
    config = load_config()

    lines: list[str] = [
        "# nanobot secrets — generated by: nanobot config migrate-to-env",
        "# DO NOT COMMIT this file.",
        "",
        "# LLM Provider API Keys",
    ]
    migrated: list[str] = []

    # Standard providers from registry
    for spec in PROVIDERS:
        provider = getattr(config.providers, spec.name, None)
        if provider is None:
            continue
        key = (provider.api_key or "").strip()
        if key:
            lines.append(f"{spec.env_key}={key}")
            migrated.append(spec.env_key)
            provider.api_key = ""

    # ElevenLabs (not in PROVIDERS)
    el = config.providers.elevenlabs
    el_key = (el.api_key or "").strip()
    if el_key:
        lines.append(f"ELEVENLABS_API_KEY={el_key}")
        migrated.append("ELEVENLABS_API_KEY")
        el.api_key = ""

    lines += ["", "# Channel Tokens"]

    tg_token = (config.channels.telegram.token or "").strip()
    if tg_token:
        lines.append(f"TELEGRAM_BOT_TOKEN={tg_token}")
        migrated.append("TELEGRAM_BOT_TOKEN")
        config.channels.telegram.token = ""

    wa_token = (config.channels.whatsapp.bridge_token or "").strip()
    if wa_token:
        lines.append(f"WHATSAPP_BRIDGE_TOKEN={wa_token}")
        migrated.append("WHATSAPP_BRIDGE_TOKEN")
        config.channels.whatsapp.bridge_token = ""

    lines += ["", "# Tool API Keys"]

    search_key = (config.tools.web.search.tavily_api_key or "").strip()
    if search_key:
        lines.append(f"TAVILY_API_KEY={search_key}")
        migrated.append("TAVILY_API_KEY")
        config.tools.web.search.tavily_api_key = ""

    if not migrated:
        console.print("[yellow]No non-empty secrets found in config.json to migrate.[/yellow]")
        return

    env_content = "\n".join(lines) + "\n"
    env_path.write_text(env_content, encoding="utf-8")
    try:
        env_path.chmod(0o600)
    except OSError:
        pass

    save_config(config)

    console.print(f"[green]✓[/green] Created {env_path}")
    console.print(f"[green]✓[/green] Migrated {len(migrated)} secret(s): {', '.join(migrated)}")
    console.print(f"[green]✓[/green] Zeroed out secrets in {get_config_path()}")
    console.print("\n[dim]Add .env to your .gitignore. It is already excluded by the default .gitignore.[/dim]")


# ============================================================================
# Agent Commands
# ============================================================================


@app.command()
def agent(
    message: str = typer.Option(None, "--message", "-m", help="Message to send to the agent"),
    session_id: str = typer.Option("cli:default", "--session", "-s", help="Session ID"),
):
    """Interact with the agent directly."""
    from nanobot.adapters.responder_llm import LLMResponder
    from nanobot.adapters.telemetry import InMemoryTelemetry
    from nanobot.agent.tools.file_access import build_file_access_resolver
    from nanobot.bus.queue import MessageBus
    from nanobot.config.loader import load_config
    from nanobot.policy.loader import load_policy
    from nanobot.security import NoopSecurity, SecurityEngine

    config = load_config()
    memory_service = _make_memory_service(config)
    telemetry = InMemoryTelemetry()

    bus = MessageBus(
        inbound_maxsize=config.bus.inbound_maxsize,
        outbound_maxsize=config.bus.outbound_maxsize,
    )
    provider = _make_provider(config)
    security = SecurityEngine(config.security) if config.security.enabled else NoopSecurity()
    restrict_to_workspace = bool(config.tools.restrict_to_workspace)
    exec_config = config.tools.exec.model_copy(deep=True)
    if config.security.strict_profile:
        restrict_to_workspace = True
        exec_config.isolation.enabled = True
        exec_config.isolation.fail_closed = True
        exec_config.allow_host_execution = False

    policy = load_policy()
    file_access_resolver = build_file_access_resolver(
        workspace=config.workspace_path,
        policy=policy,
    )

    responder = LLMResponder(
        bus=bus,
        provider=provider,
        workspace=config.workspace_path,
        model=config.agents.defaults.model,
        max_iterations=config.agents.defaults.max_tool_iterations,
        tavily_api_key=config.tools.web.search.tavily_api_key or None,
        exec_config=exec_config,
        restrict_to_workspace=restrict_to_workspace,
        memory_service=memory_service,
        telemetry=telemetry,
        security=security,
        file_access_resolver=file_access_resolver,
    )

    if message:
        # Single message mode
        async def run_once():
            response = await responder.process_direct(message, session_key=session_id)
            console.print(f"\n{__logo__} {response}")

        try:
            asyncio.run(run_once())
        finally:
            responder.close()
            memory_service.close()
    else:
        # Interactive mode
        console.print(f"{__logo__} Interactive mode (Ctrl+C to exit)\n")

        async def run_interactive():
            while True:
                try:
                    user_input = console.input("[bold blue]You:[/bold blue] ")
                    if not user_input.strip():
                        continue

                    response = await responder.process_direct(user_input, session_key=session_id)
                    console.print(f"\n{__logo__} {response}\n")
                except KeyboardInterrupt:
                    console.print("\nGoodbye!")
                    break

        try:
            asyncio.run(run_interactive())
        finally:
            responder.close()
            memory_service.close()


# ============================================================================
# Channel Commands
# ============================================================================


channels_app = typer.Typer(help="Manage channels")
app.add_typer(channels_app, name="channels")


@channels_app.command("status")
def channels_status():
    """Show channel status."""
    from nanobot.config.loader import load_config

    config = load_config()

    table = Table(title="Channel Status")
    table.add_column("Channel", style="cyan")
    table.add_column("Enabled", style="green")
    table.add_column("Configuration", style="yellow")

    # WhatsApp
    wa = config.channels.whatsapp
    table.add_row(
        "WhatsApp",
        "✓" if wa.enabled else "✗",
        f"{wa.resolved_bridge_url} (host={wa.bridge_host}, port={wa.resolved_bridge_port})",
    )

    dc = config.channels.discord
    table.add_row("Discord", "✓" if dc.enabled else "✗", dc.gateway_url)

    # Telegram
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
):
    """Ensure WhatsApp runtime, bridge process and protocol health are ready."""
    from nanobot.channels.whatsapp_runtime import WhatsAppRuntimeManager
    from nanobot.config.loader import load_config

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
):
    """Repair WhatsApp decrypt issues for one sender by resetting sender/session auth artifacts."""
    import shutil

    from nanobot.channels.whatsapp_runtime import WhatsAppRuntimeManager
    from nanobot.config.loader import load_config

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

    # Sender-key-memory caches mapping for a chat; clear only when chat scope is explicit.
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
        from nanobot.channels.whatsapp_runtime import WhatsAppRuntimeManager

        runtime = WhatsAppRuntimeManager()
        bridge_dir = runtime.ensure_runtime()
        return bridge_dir
    except Exception as e:
        console.print(f"[red]Failed to prepare WhatsApp bridge runtime:[/red] {e}")
        raise typer.Exit(1)


def _ensure_whatsapp_bridge_token(config=None, *, quiet: bool = False) -> str:
    """Ensure channels.whatsapp.bridgeToken exists, generating and saving if missing."""
    try:
        from nanobot.channels.whatsapp_runtime import WhatsAppRuntimeManager

        runtime = WhatsAppRuntimeManager(config=config)
        return runtime.ensure_bridge_token(quiet=quiet)
    except Exception as e:
        console.print(f"[red]Failed to ensure generated bridge token:[/red] {e}")
        raise typer.Exit(1)


def _rotate_whatsapp_bridge_token(config=None) -> tuple[str, str]:
    """Rotate channels.whatsapp.bridgeToken and persist it."""
    try:
        from nanobot.channels.whatsapp_runtime import WhatsAppRuntimeManager

        runtime = WhatsAppRuntimeManager(config=config)
        old_token, new_token = runtime.rotate_bridge_token()
    except Exception as e:
        console.print(f"[red]Failed to save rotated bridge token:[/red] {e}")
        raise typer.Exit(1)

    from nanobot.config.loader import get_config_path

    console.print(
        f"[green]✓[/green] Rotated channels.whatsapp.bridgeToken and saved to {get_config_path()}"
    )
    return old_token, new_token


@channels_app.command("login")
def channels_login():
    """Link device via QR code."""
    import os
    import subprocess

    from nanobot.config.loader import load_config

    config = load_config()
    wa = config.channels.whatsapp
    token = _ensure_whatsapp_bridge_token(config=config)

    bridge_dir = _get_bridge_dir()

    console.print(f"{__logo__} Starting bridge...")
    console.print("Scan the QR code to connect.\n")

    try:
        env = dict(os.environ)
        env["BRIDGE_PORT"] = str(wa.bridge_port or _bridge_port_from_config())
        env["BRIDGE_HOST"] = wa.bridge_host
        env["BRIDGE_TOKEN"] = token
        env["AUTH_DIR"] = str(Path(wa.auth_dir).expanduser())
        subprocess.run(["node", "dist/index.js"], cwd=bridge_dir, check=True, env=env)
    except subprocess.CalledProcessError as e:
        console.print(f"[red]Bridge failed: {e}[/red]")
    except FileNotFoundError:
        console.print("[red]node not found. Please install Node.js >= 20.[/red]")


# WhatsApp bridge process manager
bridge_app = typer.Typer(help="Manage WhatsApp bridge process")
channels_app.add_typer(bridge_app, name="bridge")


def _bridge_pid_path() -> Path:
    from nanobot.utils.helpers import get_run_path
    return get_run_path() / "whatsapp-bridge.pid"


def _bridge_log_path() -> Path:
    from nanobot.utils.helpers import get_logs_path
    return get_logs_path() / "whatsapp-bridge.log"


def _bridge_port_from_config() -> int:
    from nanobot.config.loader import load_config

    wa = load_config().channels.whatsapp
    return wa.resolved_bridge_port


def _find_bridge_pids(port: int) -> list[int]:
    pids: set[int] = set(listener_pids_for_port(port))

    stored = read_pid_file(_bridge_pid_path())
    if stored is not None and pid_alive(stored) and (stored in pids or is_bridge_process(stored)):
        pids.add(stored)

    return sorted(p for p in pids if pid_alive(p))


def _stop_bridge_processes(port: int, timeout_s: float = 8.0) -> int:
    import signal
    import time

    pids = _find_bridge_pids(port)
    if not pids:
        _bridge_pid_path().unlink(missing_ok=True)
        return 0

    # Prefer process-group signaling so npm wrappers + child node both stop.
    for p in pids:
        signal_process_group(p, signal.SIGTERM)

    stopped = 0
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        remaining = [p for p in pids if pid_alive(p)]
        listeners = _find_bridge_pids(port)
        if not remaining and not listeners:
            stopped = len(pids)
            break
        time.sleep(0.2)
    else:
        for p in [p for p in pids if pid_alive(p)]:
            signal_process_group(p, signal.SIGKILL)
        for p in _find_bridge_pids(port):
            signal_process_group(p, signal.SIGKILL)
        stopped = len(pids)

    _bridge_pid_path().unlink(missing_ok=True)
    return stopped


@bridge_app.command("start")
def bridge_start(
    port: int = typer.Option(
        None, "--port", "-p", help="Bridge port (default: from config bridge_url)"
    ),
):
    """Start WhatsApp bridge in background."""
    from nanobot.channels.whatsapp_runtime import WhatsAppRuntimeManager
    from nanobot.config.loader import load_config

    runtime = WhatsAppRuntimeManager(config=load_config())
    resolved_port = _bridge_port_from_config() if port is None else port
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

    console.print(f"[green]✓[/green] Bridge started (pid {status.pids[0]}, port {status.port})")
    console.print(f"Log: {status.log_path}")


@bridge_app.command("stop")
def bridge_stop(
    port: int = typer.Option(
        None, "--port", "-p", help="Bridge port (default: from config bridge_url)"
    ),
):
    """Stop WhatsApp bridge."""
    from nanobot.channels.whatsapp_runtime import WhatsAppRuntimeManager
    from nanobot.config.loader import load_config

    runtime = WhatsAppRuntimeManager(config=load_config())
    resolved_port = _bridge_port_from_config() if port is None else port
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
):
    """Restart WhatsApp bridge."""
    from nanobot.channels.whatsapp_runtime import WhatsAppRuntimeManager
    from nanobot.config.loader import load_config

    runtime = WhatsAppRuntimeManager(config=load_config())
    resolved_port = _bridge_port_from_config() if port is None else port
    try:
        status = runtime.restart_bridge(resolved_port)
    except Exception as e:
        console.print(f"[red]Bridge restart failed:[/red] {e}")
        raise typer.Exit(1)
    console.print(f"[green]✓[/green] Bridge started (pid {status.pids[0]}, port {status.port})")
    console.print(f"Log: {status.log_path}")


@bridge_app.command("status")
def bridge_status(
    port: int = typer.Option(
        None, "--port", "-p", help="Bridge port (default: from config bridge_url)"
    ),
):
    """Show WhatsApp bridge status."""
    from nanobot.channels.whatsapp_runtime import WhatsAppRuntimeManager
    from nanobot.config.loader import load_config

    runtime = WhatsAppRuntimeManager(config=load_config())
    resolved_port = _bridge_port_from_config() if port is None else port
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
):
    """Rotate WhatsApp bridge token and restart affected processes."""
    from nanobot.channels.whatsapp_runtime import WhatsAppRuntimeManager
    from nanobot.config.loader import load_config

    config = load_config()
    runtime = WhatsAppRuntimeManager(config=config)
    resolved_port = _bridge_port_from_config() if port is None else port
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
            console.print(
                f"[green]✓[/green] Bridge restarted (pid {status.pids[0]}, port {status.port})"
            )
            console.print(f"Log: {status.log_path}")
        elif start_bridge_if_stopped:
            status = runtime.start_bridge(resolved_port)
            console.print(
                f"[green]✓[/green] Bridge started (pid {status.pids[0]}, port {status.port})"
            )
            console.print(f"Log: {status.log_path}")
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


# ============================================================================
# Policy Commands
# ============================================================================

policy_app = typer.Typer(help="Manage chat policy")
app.add_typer(policy_app, name="policy")


def _policy_known_tools() -> set[str]:
    """Known top-level tools for policy diagnostics."""
    return {
        "read_file",
        "write_file",
        "edit_file",
        "list_dir",
        "exec",
        "pi_stats",
        "web_search",
        "web_fetch",
        "deep_research",
        "message",
        "spawn",
        "cron",
    }


@policy_app.command("path")
def policy_path_cmd():
    """Show policy file location."""
    from nanobot.policy.loader import get_policy_path

    console.print(get_policy_path())


@policy_app.command("explain")
def policy_explain(
    channel: str = typer.Option(..., "--channel", "-c", help="Channel: telegram or whatsapp"),
    chat_id: str = typer.Option(..., "--chat", help="Chat ID"),
    sender_id: str = typer.Option(..., "--sender", "-s", help="Sender ID"),
    is_group: bool = typer.Option(False, "--group", help="Treat as group chat"),
    mentioned_bot: bool = typer.Option(False, "--mentioned", help="Message mentions the bot"),
    reply_to_bot: bool = typer.Option(False, "--reply-to-bot", help="Message is a reply to bot"),
):
    """Explain merged policy + decision for one actor/chat."""
    from nanobot.adapters.policy_engine import EnginePolicyAdapter
    from nanobot.config.loader import load_config

    config = load_config()
    policy_engine, policy_path = _make_policy_engine(config)
    policy_adapter = EnginePolicyAdapter(
        engine=policy_engine,
        known_tools=_policy_known_tools(),
        policy_path=policy_path,
        reload_on_change=False,
    )
    report = policy_adapter.explain(
        channel=channel,
        chat_id=chat_id,
        sender_id=sender_id,
        is_group=is_group,
        mentioned_bot=mentioned_bot,
        reply_to_bot=reply_to_bot,
    )
    console.print_json(json.dumps(report, ensure_ascii=False, indent=2))


@policy_app.command("cmd")
def policy_cmd(
    command: str = typer.Argument(..., help='Canonical slash command, e.g. "/policy list-groups"'),
    dry_run: bool = typer.Option(False, "--dry-run", help="Run command in dry-run mode"),
    confirm: bool = typer.Option(False, "--confirm", help="Confirm risky command execution"),
):
    """Execute one shared policy admin command via CLI."""
    import getpass

    from nanobot.config.loader import load_config
    from nanobot.policy.admin.contracts import PolicyActorContext, PolicyExecutionOptions
    from nanobot.policy.admin.service import PolicyAdminService

    config = load_config()
    policy_engine, policy_path = _make_policy_engine(config)
    if policy_path is None:
        console.print("[red]Policy path unavailable[/red]")
        raise typer.Exit(1)

    apply_channels = (
        policy_engine.apply_channels if policy_engine is not None else {"telegram", "whatsapp"}
    )
    service = PolicyAdminService(
        policy_path=policy_path,
        workspace=config.workspace_path,
        known_tools=_policy_known_tools(),
        apply_channels=apply_channels,
        on_policy_applied=None,
    )
    result = service.execute_from_text(
        command,
        actor=PolicyActorContext(
            source="cli",
            channel="cli",
            chat_id="local",
            sender_id=getpass.getuser(),
            is_group=False,
            is_owner=True,
        ),
        options=PolicyExecutionOptions(dry_run=dry_run, confirm=confirm),
    )
    if result.message:
        console.print(result.message)
    if result.outcome in {"invalid", "error", "denied"}:
        raise typer.Exit(1)


@policy_app.command("annotate-whatsapp-comments")
def policy_annotate_whatsapp_comments(
    overwrite: bool = typer.Option(False, "--overwrite", help="Overwrite existing comment fields"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show changes without writing policy.json"
    ),
    bridge_url: str | None = typer.Option(
        None, "--bridge-url", help="WhatsApp bridge ws:// URL (default: from config)"
    ),
):
    """Fill WhatsApp group chat comments in policy.json using the running bridge."""
    import asyncio
    import shutil
    import time
    import uuid

    import websockets

    from nanobot.config.loader import load_config
    from nanobot.policy.loader import get_policy_path, load_policy, save_policy

    async def _list_groups(url: str, ids: list[str], token: str) -> dict[str, str]:
        request_id = uuid.uuid4().hex
        payload = {
            "version": 2,
            "type": "list_groups",
            "token": token,
            "requestId": request_id,
            "accountId": "default",
            "payload": {"ids": ids},
        }
        async with websockets.connect(url) as ws:
            await ws.send(json.dumps(payload))
            deadline = time.monotonic() + 10.0
            while True:
                timeout = deadline - time.monotonic()
                if timeout <= 0:
                    raise TimeoutError("bridge did not reply in time")
                raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                data = json.loads(raw)
                if data.get("version") != 2:
                    continue
                if data.get("type") != "response":
                    continue
                if data.get("requestId") != request_id:
                    continue
                response_payload = data.get("payload")
                if not isinstance(response_payload, dict):
                    raise RuntimeError("bridge response payload malformed")
                if not bool(response_payload.get("ok")):
                    error = response_payload.get("error")
                    raise RuntimeError(f"bridge returned error: {error}")
                result = response_payload.get("result")
                if not isinstance(result, dict):
                    raise RuntimeError("bridge response result malformed")
                groups = result.get("groups", [])
                out: dict[str, str] = {}
                if isinstance(groups, list):
                    for item in groups:
                        if not isinstance(item, dict):
                            continue
                        gid = str(item.get("chatJid", "")).strip()
                        subj = str(item.get("subject", "")).strip()
                        if gid and subj:
                            out[gid] = subj
                return out

    config = load_config()
    resolved_bridge_url = bridge_url or config.channels.whatsapp.resolved_bridge_url
    bridge_token = _ensure_whatsapp_bridge_token(config=config)

    policy_path = get_policy_path()
    policy = load_policy(policy_path)

    wa = policy.channels.get("whatsapp")
    if not wa or not wa.chats:
        console.print("[yellow]No WhatsApp chats found in policy.json[/yellow]")
        return

    chat_ids = [cid for cid in wa.chats.keys() if isinstance(cid, str) and cid.endswith("@g.us")]
    if not chat_ids:
        console.print("[yellow]No WhatsApp group chat IDs (*@g.us) found in policy.json[/yellow]")
        return

    targets: list[str] = []
    for chat_id in chat_ids:
        ov = wa.chats.get(chat_id)
        current = getattr(ov, "comment", None) if ov else None
        if overwrite or not (isinstance(current, str) and current.strip()):
            targets.append(chat_id)

    if not targets:
        console.print("[green]✓[/green] Nothing to do (all groups already have comments).")
        return

    try:
        names = asyncio.run(_list_groups(resolved_bridge_url, targets, bridge_token))
    except Exception as e:
        console.print(f"[red]Failed to fetch group names from bridge:[/red] {e}")
        console.print(
            "[dim]Tip: ensure the WhatsApp bridge is running and connected (nanobot channels bridge status).[/dim]"
        )
        raise typer.Exit(1)

    updated = 0
    missing: list[str] = []
    for chat_id in targets:
        name = names.get(chat_id)
        if not name:
            missing.append(chat_id)
            continue
        wa.chats[chat_id].comment = name
        updated += 1

    if dry_run:
        console.print(f"[yellow]Dry run.[/yellow] Would update {updated} group(s).")
        if missing:
            console.print(f"[dim]No name returned for {len(missing)} group(s).[/dim]")
        return

    if policy_path.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = policy_path.with_name(f"{policy_path.name}.bak-{stamp}")
        shutil.copy2(policy_path, backup_path)
        console.print(f"[green]✓[/green] Backup written: {backup_path}")

    save_policy(policy, policy_path)
    console.print(f"[green]✓[/green] Updated policy comments for {updated} group(s): {policy_path}")
    if missing:
        console.print(f"[yellow]Warning:[/yellow] no name returned for {len(missing)} group(s).")


# ============================================================================
# Cron Commands
# ============================================================================

cron_app = typer.Typer(help="Manage scheduled tasks")
app.add_typer(cron_app, name="cron")


@cron_app.command("list")
def cron_list(
    all: bool = typer.Option(False, "--all", "-a", help="Include disabled jobs"),
):
    """List scheduled jobs."""
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

    import time

    for job in jobs:
        # Format schedule
        if job.schedule.kind == "every":
            sched = f"every {(job.schedule.every_ms or 0) // 1000}s"
        elif job.schedule.kind == "cron":
            sched = job.schedule.expr or ""
        else:
            sched = "one-time"

        # Format next run
        next_run = ""
        if job.state.next_run_at_ms:
            next_time = time.strftime(
                "%Y-%m-%d %H:%M", time.localtime(job.state.next_run_at_ms / 1000)
            )
            next_run = next_time

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
):
    """Add a scheduled job."""
    from nanobot.cron.service import CronService
    from nanobot.cron.types import CronSchedule
    from nanobot.utils.helpers import get_operational_data_path

    # Determine schedule type
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
):
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
):
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
):
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
):
    """Manually run a job."""
    from nanobot.cron.service import CronService
    from nanobot.utils.helpers import get_operational_data_path

    store_path = get_operational_data_path() / "cron" / "jobs.json"
    service = CronService(store_path)

    async def run():
        return await service.run_job(job_id, force=force)

    if asyncio.run(run()):
        console.print("[green]✓[/green] Job executed")
    else:
        console.print(f"[red]Failed to run job {job_id}[/red]")


# ============================================================================
# Memory Commands
# ============================================================================


memory_app = typer.Typer(help="Manage long-term memory")
app.add_typer(memory_app, name="memory")
notes_app = typer.Typer(help="Manage background group notes capture")
memory_app.add_typer(notes_app, name="notes")


def _memory_scope_keys(
    service,
    *,
    scope: str,
    channel: str | None,
    chat_id: str | None,
    sender_id: str | None,
) -> list[str]:
    keys: list[str] = []
    if scope in {"chat", "all"} and channel and chat_id:
        keys.append(service.chat_scope_key(channel, chat_id))
    if scope in {"user", "all"} and channel and (sender_id or chat_id):
        keys.append(service.user_scope_key(channel, (sender_id or chat_id or "").strip()))
    if scope in {"global", "all"}:
        keys.append(service.global_scope_key())
    return keys


def _notes_channel_guard(channel: str) -> str:
    value = channel.strip().lower()
    if value not in {"whatsapp", "telegram"}:
        console.print("[red]Only whatsapp/telegram are supported for memory notes in v1.[/red]")
        raise typer.Exit(1)
    return value


def _notes_parse_optional_bool(raw: str) -> bool | None:
    value = raw.strip().lower()
    if value == "inherit":
        return None
    if value == "on":
        return True
    if value == "off":
        return False
    raise ValueError("must be one of: on, off, inherit")


def _notes_parse_optional_mode(raw: str) -> Literal["adaptive", "heuristic", "hybrid"] | None:
    value = raw.strip().lower()
    if value == "inherit":
        return None
    if value in {"adaptive", "heuristic", "hybrid"}:
        return value
    raise ValueError("must be one of: adaptive, heuristic, hybrid, inherit")


@notes_app.command("status")
def memory_notes_status(
    channel: str = typer.Option(..., "--channel", help="Channel name"),
    chat_id: str = typer.Option(..., "--chat-id", help="Chat id"),
    is_group: bool = typer.Option(True, "--is-group/--is-dm", help="Resolve as group or DM"),
):
    """Show effective background memory-notes settings for one chat."""
    from nanobot.config.loader import load_config
    from nanobot.policy.engine import PolicyEngine
    from nanobot.policy.loader import load_policy

    resolved_channel = _notes_channel_guard(channel)
    config = load_config()
    policy = load_policy()
    engine = PolicyEngine(
        policy=policy,
        workspace=config.workspace_path,
        apply_channels={"telegram", "whatsapp"},
    )
    resolved = engine.resolve_memory_notes(
        channel=resolved_channel,
        chat_id=chat_id,
        is_group=is_group,
    )
    console.print("[bold]Memory Notes Status[/bold]")
    console.print(f"channel: {resolved_channel}")
    console.print(f"chat_id: {chat_id}")
    console.print(f"is_group: {is_group}")
    console.print(f"enabled: {resolved.enabled}")
    console.print(f"mode: {resolved.mode}")
    console.print(f"allow_blocked_senders: {resolved.allow_blocked_senders}")
    console.print(f"batch_interval_seconds: {resolved.batch_interval_seconds}")
    console.print(f"batch_max_messages: {resolved.batch_max_messages}")
    source_table = Table(title="Resolution Source")
    source_table.add_column("Field")
    source_table.add_column("Source")
    for key in ("enabled", "mode", "allowBlockedSenders"):
        source_table.add_row(key, str(resolved.source.get(key, "-")))
    console.print(source_table)


@notes_app.command("set")
def memory_notes_set(
    channel: str = typer.Option(..., "--channel", help="Channel name"),
    chat_id: str = typer.Option(..., "--chat-id", help="Chat id"),
    enabled: str = typer.Option("inherit", "--enabled", help="on|off|inherit"),
    mode: str = typer.Option("inherit", "--mode", help="adaptive|hybrid|heuristic|inherit"),
    allow_blocked: str = typer.Option(
        "inherit",
        "--allow-blocked",
        help="on|off|inherit",
    ),
):
    """Set per-chat memory-notes override in policy.json."""
    from nanobot.policy.loader import load_policy, save_policy
    from nanobot.policy.schema import MemoryNotesChannelPolicy, MemoryNotesOverride

    resolved_channel = _notes_channel_guard(channel)
    try:
        enabled_value = _notes_parse_optional_bool(enabled)
        mode_value = _notes_parse_optional_mode(mode)
        allow_blocked_value = _notes_parse_optional_bool(allow_blocked)
    except ValueError as e:
        console.print(f"[red]Invalid value:[/red] {e}")
        raise typer.Exit(1)

    policy = load_policy()
    channel_cfg = policy.memory_notes.channels.get(resolved_channel)
    if channel_cfg is None:
        channel_cfg = MemoryNotesChannelPolicy()
        policy.memory_notes.channels[resolved_channel] = channel_cfg

    override = channel_cfg.chats.get(chat_id)
    if override is None:
        override = MemoryNotesOverride()
        channel_cfg.chats[chat_id] = override

    override.enabled = enabled_value
    override.mode = mode_value
    override.allow_blocked_senders = allow_blocked_value

    if (
        override.enabled is None
        and override.mode is None
        and override.allow_blocked_senders is None
    ):
        channel_cfg.chats.pop(chat_id, None)

    save_policy(policy)
    console.print("[green]✓[/green] Updated memory notes policy override.")
    console.print(f"channel={resolved_channel} chat_id={chat_id}")


@memory_app.command("status")
def memory_status():
    """Show long-term memory status and counters."""
    from nanobot.config.loader import load_config

    config = load_config()
    service = _make_memory_service(config)
    try:
        stats = service.stats()
    finally:
        service.close()

    console.print("[bold]Memory Status[/bold]")
    console.print(f"enabled: {stats.get('enabled')}")
    console.print(f"backend: {stats.get('backend')}")
    console.print(f"wal_enabled: {stats.get('wal_enabled')}")
    console.print(f"db_path: {stats.get('db_path')}")
    console.print(f"state_dir: {stats.get('state_dir')}")
    console.print(f"total_active: {stats.get('total_active')}")
    console.print(f"total_deleted: {stats.get('total_deleted')}")
    console.print(f"wal_files: {stats.get('wal_files')}")
    marker = str(stats.get("backfill_marker") or "")
    console.print(f"backfill_marker: {marker or '(not set)'}")

    kind_table = Table(title="By Kind")
    kind_table.add_column("Kind")
    kind_table.add_column("Count", justify="right")
    for kind, count in sorted((stats.get("by_kind") or {}).items()):
        kind_table.add_row(str(kind), str(count))
    console.print(kind_table)

    scope_table = Table(title="By Scope")
    scope_table.add_column("Scope")
    scope_table.add_column("Count", justify="right")
    for scope_name, count in sorted((stats.get("by_scope") or {}).items()):
        scope_table.add_row(str(scope_name), str(count))
    console.print(scope_table)


@memory_app.command("search")
def memory_search(
    query: str = typer.Option(..., "--query", "-q", help="Search query"),
    channel: str | None = typer.Option(None, "--channel", help="Channel for scoped search"),
    chat_id: str | None = typer.Option(None, "--chat-id", help="Chat id for scoped search"),
    sender_id: str | None = typer.Option(None, "--sender-id", help="Sender id for user scope"),
    scope: str = typer.Option("all", "--scope", help="chat|user|global|all"),
    limit: int = typer.Option(8, "--limit", "-n", min=1, max=100),
):
    """Search long-term memory with scope filters."""
    from nanobot.config.loader import load_config

    scope_value = scope.strip().lower()
    if scope_value not in {"chat", "user", "global", "all"}:
        console.print("[red]Invalid --scope. Use: chat|user|global|all[/red]")
        raise typer.Exit(1)

    config = load_config()
    service = _make_memory_service(config)
    try:
        hits = service.search(
            query=query,
            channel=channel,
            chat_id=chat_id,
            sender_id=sender_id,
            scope=scope_value,
            limit=limit,
        )
    finally:
        service.close()

    if not hits:
        console.print("No memory hits.")
        return

    table = Table(title="Memory Search Results")
    table.add_column("Score", justify="right")
    table.add_column("Kind")
    table.add_column("Scope")
    table.add_column("Updated")
    table.add_column("Content")
    for hit in hits:
        content = " ".join(hit.entry.content.split())
        if len(content) > 120:
            content = content[:117] + "..."
        table.add_row(
            f"{hit.final_score:.2f}",
            hit.entry.kind,
            hit.entry.scope_type,
            hit.entry.updated_at[:19],
            content,
        )
    console.print(table)


@memory_app.command("add")
def memory_add(
    text: str = typer.Option(..., "--text", "-t", help="Memory text"),
    kind: str = typer.Option(..., "--kind", "-k", help="preference|decision|fact|episodic"),
    scope: str = typer.Option("chat", "--scope", help="chat|user|global"),
    channel: str = typer.Option("cli", "--channel", help="Channel for chat/user scope"),
    chat_id: str = typer.Option("direct", "--chat-id", help="Chat id for chat/user scope"),
    sender_id: str | None = typer.Option(None, "--sender-id", help="Sender id for user scope"),
    importance: float = typer.Option(0.8, "--importance", min=0.0, max=1.0),
    confidence: float = typer.Option(1.0, "--confidence", min=0.0, max=1.0),
):
    """Add one manual memory entry."""
    from nanobot.config.loader import load_config

    kind_value = kind.strip().lower()
    if kind_value not in {"preference", "decision", "fact", "episodic"}:
        console.print("[red]Invalid --kind. Use: preference|decision|fact|episodic[/red]")
        raise typer.Exit(1)
    scope_value = scope.strip().lower()
    if scope_value not in {"chat", "user", "global"}:
        console.print("[red]Invalid --scope. Use: chat|user|global[/red]")
        raise typer.Exit(1)

    config = load_config()
    service = _make_memory_service(config)
    try:
        entry, inserted = service.record_manual(
            channel=channel,
            chat_id=chat_id,
            sender_id=sender_id,
            scope_type=scope_value,
            kind=kind_value,
            text=text,
            importance=importance,
            confidence=confidence,
        )
    finally:
        service.close()

    action = "Inserted" if inserted else "Merged"
    console.print(f"[green]✓[/green] {action} memory entry: {entry.id}")
    console.print(f"scope={entry.scope_type}:{entry.scope_key}")


@memory_app.command("prune")
def memory_prune(
    older_than_days: int | None = typer.Option(
        None,
        "--older-than-days",
        help="Prune entries older than N days by updated_at",
    ),
    kind: str | None = typer.Option(None, "--kind", help="Optional kind filter"),
    scope: str = typer.Option("all", "--scope", help="chat|user|global|all"),
    channel: str | None = typer.Option(None, "--channel", help="Channel for scope filter"),
    chat_id: str | None = typer.Option(None, "--chat-id", help="Chat id for scope filter"),
    sender_id: str | None = typer.Option(None, "--sender-id", help="Sender id for user scope"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview only"),
):
    """Prune long-term memory entries safely."""
    from nanobot.config.loader import load_config

    scope_value = scope.strip().lower()
    if scope_value not in {"chat", "user", "global", "all"}:
        console.print("[red]Invalid --scope. Use: chat|user|global|all[/red]")
        raise typer.Exit(1)

    kinds: set[str] | None = None
    if kind:
        k = kind.strip().lower()
        if k not in {"preference", "decision", "fact", "episodic"}:
            console.print("[red]Invalid --kind. Use: preference|decision|fact|episodic[/red]")
            raise typer.Exit(1)
        kinds = {k}

    config = load_config()
    service = _make_memory_service(config)
    try:
        scope_keys = _memory_scope_keys(
            service,
            scope=scope_value,
            channel=channel,
            chat_id=chat_id,
            sender_id=sender_id,
        )
        pruned = service.prune(
            older_than_days=older_than_days,
            kinds=kinds,
            scope_keys=scope_keys or None,
            dry_run=dry_run,
        )
    finally:
        service.close()

    if dry_run:
        console.print(f"[yellow]Dry run:[/yellow] {pruned} entries would be pruned.")
    else:
        console.print(f"[green]✓[/green] Pruned {pruned} entries.")


@memory_app.command("backfill")
def memory_backfill(
    force: bool = typer.Option(False, "--force", help="Run backfill even if marker exists"),
):
    """Backfill legacy memory files into long-term memory DB."""
    from nanobot.config.loader import load_config

    config = load_config()
    service = _make_memory_service(config)
    try:
        imported = service.backfill_from_workspace_files(force=force)
    finally:
        service.close()

    console.print(f"[green]✓[/green] Backfill imported {imported} entries.")


@memory_app.command("reindex")
def memory_reindex():
    """Rebuild memory full-text index."""
    from nanobot.config.loader import load_config

    config = load_config()
    service = _make_memory_service(config)
    try:
        service.reindex()
    finally:
        service.close()

    console.print("[green]✓[/green] Memory FTS index rebuilt.")


# ============================================================================
# Status Commands
# ============================================================================


@app.command()
def logs(
    follow: bool = typer.Option(True, "--follow/--no-follow", help="Follow log output"),
    lines: int = typer.Option(200, "--lines", "-n", min=1, help="Initial lines for tail mode"),
    gateway: bool = typer.Option(True, "--gateway/--no-gateway", help="Include gateway log"),
    bridge: bool = typer.Option(True, "--bridge/--no-bridge", help="Include WhatsApp bridge log"),
    raw: bool = typer.Option(False, "--raw", help="Use tail instead of lnav"),
):
    """View nanobot logs (uses lnav when available)."""
    import shutil
    import subprocess

    paths: list[Path] = []
    if gateway:
        paths.append(_gateway_log_path())
    if bridge:
        paths.append(_bridge_log_path())

    if not paths:
        console.print("[red]No logs selected. Enable --gateway and/or --bridge.[/red]")
        raise typer.Exit(1)

    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)

    if not raw and shutil.which("lnav"):
        cmd = ["lnav", *[str(path) for path in paths]]
        try:
            subprocess.run(cmd, check=False)
        except KeyboardInterrupt:
            return
        return

    cmd = ["tail", "-n", str(lines)]
    if follow:
        cmd.append("-F")
    cmd.extend(str(path) for path in paths)
    try:
        subprocess.run(cmd, check=False)
    except KeyboardInterrupt:
        return


@app.command()
def status():
    """Show nanobot status."""
    from nanobot.config.loader import get_config_path, load_config
    from nanobot.policy.loader import get_policy_path

    config_path = get_config_path()
    policy_path = get_policy_path()
    config = load_config()
    workspace = config.workspace_path

    console.print(f"{__logo__} nanobot Status\n")

    console.print(
        f"Config: {config_path} {'[green]✓[/green]' if config_path.exists() else '[red]✗[/red]'}"
    )
    console.print(
        f"Policy: {policy_path} {'[green]✓[/green]' if policy_path.exists() else '[red]✗[/red]'}"
    )
    console.print(
        f"Workspace: {workspace} {'[green]✓[/green]' if workspace.exists() else '[red]✗[/red]'}"
    )

    if config_path.exists():
        from nanobot.providers.registry import PROVIDERS

        console.print(f"Model: {config.agents.defaults.model}")

        # Check API keys from registry
        for spec in PROVIDERS:
            p = getattr(config.providers, spec.name, None)
            if p is None:
                continue
            if spec.is_local:
                # Local deployments show api_base instead of api_key
                if p.api_base:
                    console.print(f"{spec.label}: [green]✓ {p.api_base}[/green]")
                else:
                    console.print(f"{spec.label}: [dim]not set[/dim]")
            else:
                has_key = bool(p.api_key)
                console.print(
                    f"{spec.label}: {'[green]✓[/green]' if has_key else '[dim]not set[/dim]'}"
                )


if __name__ == "__main__":
    app()
