"""CLI commands for nanobot."""

import asyncio
import json
from datetime import datetime
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from nanobot import __logo__, __version__

app = typer.Typer(
    name="nanobot",
    help=f"{__logo__} nanobot - Personal AI Assistant",
    no_args_is_help=True,
)

console = Console()


def version_callback(value: bool):
    if value:
        console.print(f"{__logo__} nanobot v{__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        None, "--version", "-v", callback=version_callback, is_eager=True
    ),
):
    """nanobot - Personal AI Assistant."""
    pass


# ============================================================================
# Onboard / Setup
# ============================================================================


@app.command()
def onboard():
    """Initialize nanobot configuration and workspace."""
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

    console.print(f"\n{__logo__} nanobot is ready!")
    console.print("\nNext steps:")
    console.print("  1. Add your API key to [cyan]~/.nanobot/config.json[/cyan]")
    console.print("     Get one at: https://openrouter.ai/keys")
    console.print("  2. Chat: [cyan]nanobot agent -m \"Hello!\"[/cyan]")
    console.print("\n[dim]Want Telegram/WhatsApp? See: https://github.com/HKUDS/nanobot#-chat-apps[/dim]")




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

I am nanobot, a lightweight AI assistant.

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

    # Create memory directory and MEMORY.md
    memory_dir = workspace / "memory"
    memory_dir.mkdir(exist_ok=True)
    memory_file = memory_dir / "MEMORY.md"
    if not memory_file.exists():
        memory_file.write_text("""# Long-term Memory

This file stores important information that should persist across sessions.

## User Information

(Important facts about the user)

## Preferences

(User preferences learned over time)

## Important Notes

(Things to remember)
""")
        console.print("  [dim]Created memory/MEMORY.md[/dim]")


def _make_provider(config):
    """Create LiteLLMProvider from config. Exits if no API key found."""
    from nanobot.providers.litellm_provider import LiteLLMProvider
    p = config.get_provider()
    model = config.agents.defaults.model
    if not (p and p.api_key) and not model.startswith("bedrock/"):
        console.print("[red]Error: No API key configured.[/red]")
        console.print("Set one in ~/.nanobot/config.json under providers section")
        raise typer.Exit(1)
    return LiteLLMProvider(
        api_key=p.api_key if p else None,
        api_base=config.get_api_base(),
        default_model=model,
        extra_headers=p.extra_headers if p else None,
    )


def _make_policy_engine(config):
    """Create policy engine + path from ~/.nanobot/policy.json."""
    from nanobot.config.loader import get_config_path
    from nanobot.policy.engine import PolicyEngine
    from nanobot.policy.loader import get_policy_path, load_policy, warn_legacy_allow_from

    warn_legacy_allow_from(get_config_path())
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
    from nanobot.config.loader import get_data_dir

    run_dir = get_data_dir() / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir / "gateway.pid"


def _gateway_log_path() -> Path:
    from nanobot.config.loader import get_data_dir

    logs_dir = get_data_dir() / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    return logs_dir / "gateway.log"


def _pid_alive(pid: int) -> bool:
    import os

    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _command_for_pid(pid: int) -> str:
    import subprocess

    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "command="],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


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


def _gateway_cmd_port(command: str) -> int | None:
    import shlex

    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()

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


def _is_nanobot_gateway_command(command: str) -> bool:
    import shlex

    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()

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

    command = _command_for_pid(pid)
    if not command:
        return False

    if _pid_has_env(pid, "NANOBOT_GATEWAY_DAEMON", "1"):
        cmd_port = _gateway_cmd_port(command)
        return cmd_port == port

    if not _is_nanobot_gateway_command(command):
        return False

    cmd_port = _gateway_cmd_port(command)
    return cmd_port == port


def _find_gateway_pids(port: int) -> list[int]:
    import subprocess

    pids: set[int] = set()

    pid_file = _gateway_pid_path()
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            if _pid_alive(pid) and _is_gateway_process_on_port(pid, port):
                pids.add(pid)
        except ValueError:
            pass

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
        if _pid_alive(pid) and _is_gateway_process_on_port(pid, port):
            pids.add(pid)

    return sorted(pids)


def _signal_gateway_pid(pid: int, sig: int) -> None:
    import os

    try:
        os.kill(pid, sig)
    except OSError:
        pass


def _stop_gateway_processes(port: int, timeout_s: float = 10.0) -> int:
    import signal
    import time

    pids = _find_gateway_pids(port)
    if not pids:
        _gateway_pid_path().unlink(missing_ok=True)
        return 0

    for pid in pids:
        _signal_gateway_pid(pid, signal.SIGTERM)

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        remaining = [pid for pid in pids if _pid_alive(pid)]
        listeners = _find_gateway_pids(port)
        if not remaining and not listeners:
            break
        time.sleep(0.2)
    else:
        for pid in [pid for pid in pids if _pid_alive(pid)]:
            _signal_gateway_pid(pid, signal.SIGKILL)
        for pid in _find_gateway_pids(port):
            _signal_gateway_pid(pid, signal.SIGKILL)

    _gateway_pid_path().unlink(missing_ok=True)
    return len(pids)


def _start_gateway_daemon(port: int, verbose: bool) -> None:
    import os
    import subprocess
    import sys
    import time

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


def _run_gateway_foreground(port: int, verbose: bool) -> None:
    """Start the nanobot gateway in foreground."""
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.queue import MessageBus
    from nanobot.channels.manager import ChannelManager
    from nanobot.config.loader import get_data_dir, load_config
    from nanobot.cron.service import CronService
    from nanobot.cron.types import CronJob
    from nanobot.heartbeat.service import HeartbeatService
    from nanobot.session.manager import SessionManager

    if verbose:
        import logging
        logging.basicConfig(level=logging.DEBUG)

    console.print(f"{__logo__} Starting nanobot gateway on port {port}...")

    config = load_config()
    bus = MessageBus()
    provider = _make_provider(config)
    policy_engine, policy_path = _make_policy_engine(config)
    session_manager = SessionManager(config.workspace_path)

    # Create cron service first (callback set after agent creation)
    cron_store_path = get_data_dir() / "cron" / "jobs.json"
    cron = CronService(cron_store_path)

    # Create agent with cron service
    try:
        agent = AgentLoop(
            bus=bus,
            provider=provider,
            workspace=config.workspace_path,
            model=config.agents.defaults.model,
            max_iterations=config.agents.defaults.max_tool_iterations,
            brave_api_key=config.tools.web.search.api_key or None,
            exec_config=config.tools.exec,
            cron_service=cron,
            restrict_to_workspace=config.tools.restrict_to_workspace,
            session_manager=session_manager,
            policy_engine=policy_engine,
            policy_path=policy_path,
            timing_logs_enabled=config.agents.defaults.timing_logs_enabled,
        )
    except ValueError as e:
        console.print(f"[red]Policy validation error:[/red] {e}")
        raise typer.Exit(1)

    # Set cron callback (needs agent)
    async def on_cron_job(job: CronJob) -> str | None:
        """Execute a cron job through the agent."""
        response = await agent.process_direct(
            job.payload.message,
            session_key=f"cron:{job.id}",
            channel=job.payload.channel or "cli",
            chat_id=job.payload.to or "direct",
        )
        if job.payload.deliver and job.payload.to:
            from nanobot.bus.events import OutboundMessage
            await bus.publish_outbound(OutboundMessage(
                channel=job.payload.channel or "cli",
                chat_id=job.payload.to,
                content=response or ""
            ))
        return response
    cron.on_job = on_cron_job

    # Create heartbeat service
    async def on_heartbeat(prompt: str) -> str:
        """Execute heartbeat through the agent."""
        return await agent.process_direct(prompt, session_key="heartbeat")

    heartbeat = HeartbeatService(
        workspace=config.workspace_path,
        on_heartbeat=on_heartbeat,
        interval_s=30 * 60,  # 30 minutes
        enabled=True
    )

    # Create channel manager
    channels = ChannelManager(config, bus, session_manager=session_manager)

    if channels.enabled_channels:
        console.print(f"[green]✓[/green] Channels enabled: {', '.join(channels.enabled_channels)}")
    else:
        console.print("[yellow]Warning: No channels enabled[/yellow]")

    cron_status = cron.status()
    if cron_status["jobs"] > 0:
        console.print(f"[green]✓[/green] Cron: {cron_status['jobs']} scheduled jobs")

    console.print("[green]✓[/green] Heartbeat: every 30m")

    async def run():
        try:
            await cron.start()
            await heartbeat.start()
            await asyncio.gather(
                agent.run(),
                channels.start_all(),
            )
        except KeyboardInterrupt:
            console.print("\nShutting down...")
            heartbeat.stop()
            cron.stop()
            agent.stop()
            await channels.stop_all()

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
        console.print(f"[green]✓[/green] Gateway stopped ({stopped} process{'es' if stopped != 1 else ''})")
        return

    if mode == "restart":
        import time

        _stop_gateway_processes(port)
        time.sleep(0.2)
        if _find_gateway_pids(port):
            console.print(f"[red]Gateway restart failed: port {port} is still in use[/red]")
            raise typer.Exit(1)
        _start_gateway_daemon(port, verbose)
        return

    if mode == "start":
        _start_gateway_daemon(port, verbose)
        return

    _run_gateway_foreground(port, verbose)




# ============================================================================
# Agent Commands
# ============================================================================


@app.command()
def agent(
    message: str = typer.Option(None, "--message", "-m", help="Message to send to the agent"),
    session_id: str = typer.Option("cli:default", "--session", "-s", help="Session ID"),
):
    """Interact with the agent directly."""
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.queue import MessageBus
    from nanobot.config.loader import load_config

    config = load_config()

    bus = MessageBus()
    provider = _make_provider(config)
    policy_engine, policy_path = _make_policy_engine(config)

    try:
        agent_loop = AgentLoop(
            bus=bus,
            provider=provider,
            workspace=config.workspace_path,
            brave_api_key=config.tools.web.search.api_key or None,
            exec_config=config.tools.exec,
            restrict_to_workspace=config.tools.restrict_to_workspace,
            policy_engine=policy_engine,
            policy_path=policy_path,
            timing_logs_enabled=config.agents.defaults.timing_logs_enabled,
        )
    except ValueError as e:
        console.print(f"[red]Policy validation error:[/red] {e}")
        raise typer.Exit(1)

    if message:
        # Single message mode
        async def run_once():
            response = await agent_loop.process_direct(message, session_id)
            console.print(f"\n{__logo__} {response}")

        asyncio.run(run_once())
    else:
        # Interactive mode
        console.print(f"{__logo__} Interactive mode (Ctrl+C to exit)\n")

        async def run_interactive():
            while True:
                try:
                    user_input = console.input("[bold blue]You:[/bold blue] ")
                    if not user_input.strip():
                        continue

                    response = await agent_loop.process_direct(user_input, session_id)
                    console.print(f"\n{__logo__} {response}\n")
                except KeyboardInterrupt:
                    console.print("\nGoodbye!")
                    break

        asyncio.run(run_interactive())


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
        wa.bridge_url
    )

    dc = config.channels.discord
    table.add_row(
        "Discord",
        "✓" if dc.enabled else "✗",
        dc.gateway_url
    )

    # Telegram
    tg = config.channels.telegram
    tg_config = f"token: {tg.token[:10]}..." if tg.token else "[dim]not configured[/dim]"
    table.add_row(
        "Telegram",
        "✓" if tg.enabled else "✗",
        tg_config
    )

    console.print(table)


def _get_bridge_dir() -> Path:
    """Get the bridge directory, setting it up if needed."""
    import shutil
    import subprocess

    # User's bridge location
    user_bridge = Path.home() / ".nanobot" / "bridge"

    # Check if already built
    if (user_bridge / "dist" / "index.js").exists():
        return user_bridge

    # Check for npm
    if not shutil.which("npm"):
        console.print("[red]npm not found. Please install Node.js >= 18.[/red]")
        raise typer.Exit(1)

    # Find source bridge: first check package data, then source dir
    pkg_bridge = Path(__file__).parent.parent / "bridge"  # nanobot/bridge (installed)
    src_bridge = Path(__file__).parent.parent.parent / "bridge"  # repo root/bridge (dev)

    source = None
    if (pkg_bridge / "package.json").exists():
        source = pkg_bridge
    elif (src_bridge / "package.json").exists():
        source = src_bridge

    if not source:
        console.print("[red]Bridge source not found.[/red]")
        console.print("Try reinstalling: pip install --force-reinstall nanobot")
        raise typer.Exit(1)

    console.print(f"{__logo__} Setting up bridge...")

    # Copy to user directory
    user_bridge.parent.mkdir(parents=True, exist_ok=True)
    if user_bridge.exists():
        shutil.rmtree(user_bridge)
    shutil.copytree(source, user_bridge, ignore=shutil.ignore_patterns("node_modules", "dist"))

    # Install and build
    try:
        console.print("  Installing dependencies...")
        subprocess.run(["npm", "install"], cwd=user_bridge, check=True, capture_output=True)

        console.print("  Building...")
        subprocess.run(["npm", "run", "build"], cwd=user_bridge, check=True, capture_output=True)

        console.print("[green]✓[/green] Bridge ready\n")
    except subprocess.CalledProcessError as e:
        console.print(f"[red]Build failed: {e}[/red]")
        if e.stderr:
            console.print(f"[dim]{e.stderr.decode()[:500]}[/dim]")
        raise typer.Exit(1)

    return user_bridge


@channels_app.command("login")
def channels_login():
    """Link device via QR code."""
    import subprocess

    bridge_dir = _get_bridge_dir()

    console.print(f"{__logo__} Starting bridge...")
    console.print("Scan the QR code to connect.\n")

    try:
        subprocess.run(["npm", "start"], cwd=bridge_dir, check=True)
    except subprocess.CalledProcessError as e:
        console.print(f"[red]Bridge failed: {e}[/red]")
    except FileNotFoundError:
        console.print("[red]npm not found. Please install Node.js.[/red]")


# WhatsApp bridge process manager
bridge_app = typer.Typer(help="Manage WhatsApp bridge process")
channels_app.add_typer(bridge_app, name="bridge")


def _bridge_pid_path() -> Path:
    from nanobot.config.loader import get_data_dir

    run_dir = get_data_dir() / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir / "whatsapp-bridge.pid"


def _bridge_log_path() -> Path:
    from nanobot.config.loader import get_data_dir

    logs_dir = get_data_dir() / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    return logs_dir / "whatsapp-bridge.log"


def _bridge_port_from_config() -> int:
    from urllib.parse import urlparse

    from nanobot.config.loader import load_config

    bridge_url = load_config().channels.whatsapp.bridge_url
    parsed = urlparse(bridge_url)
    if parsed.port is not None:
        return parsed.port
    if parsed.scheme == "wss":
        return 443
    if parsed.scheme == "ws":
        return 80
    return 3001


def _process_cwd(pid: int) -> Path | None:
    proc_cwd = Path(f"/proc/{pid}/cwd")
    try:
        if proc_cwd.exists():
            return proc_cwd.resolve()
    except OSError:
        return None
    return None


def _is_bridge_dir(path: Path) -> bool:
    package_json = path / "package.json"
    if not package_json.exists():
        return False
    try:
        data = json.loads(package_json.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return data.get("name") == "nanobot-whatsapp-bridge"


def _is_bridge_process(pid: int) -> bool:
    cmd = _command_for_pid(pid).lower()
    if not cmd:
        return False
    cwd = _process_cwd(pid)
    if cwd and _is_bridge_dir(cwd):
        return True
    return "nanobot-whatsapp-bridge" in cmd


def _listener_pids_for_port(port: int) -> set[int]:
    import shutil
    import subprocess

    listener_pids: set[int] = set()

    if shutil.which("lsof"):
        result = subprocess.run(
            ["lsof", "-nP", "-tiTCP:{0}".format(port), "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    pid = int(line)
                    listener_pids.add(pid)
                except ValueError:
                    continue
    return listener_pids


def _find_bridge_pids(port: int) -> list[int]:
    pids: set[int] = set(_listener_pids_for_port(port))

    pid_file = _bridge_pid_path()
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            if _pid_alive(pid) and (pid in pids or _is_bridge_process(pid)):
                pids.add(pid)
        except ValueError:
            pass

    return sorted(pid for pid in pids if _pid_alive(pid))


def _signal_bridge_pid(pid: int, sig: int) -> None:
    import os

    try:
        pgid = os.getpgid(pid)
    except OSError:
        pgid = None

    # Prefer process-group signaling so npm wrappers + child node both stop.
    current_pgid = os.getpgrp()
    if pgid is not None and pgid > 0 and pgid != current_pgid:
        try:
            os.killpg(pgid, sig)
            return
        except OSError:
            pass

    try:
        os.kill(pid, sig)
    except OSError:
        pass


def _stop_bridge_processes(port: int, timeout_s: float = 8.0) -> int:
    import signal
    import time

    pids = _find_bridge_pids(port)
    if not pids:
        _bridge_pid_path().unlink(missing_ok=True)
        return 0

    stopped = 0
    for pid in pids:
        _signal_bridge_pid(pid, signal.SIGTERM)

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        remaining = [pid for pid in pids if _pid_alive(pid)]
        listeners = _find_bridge_pids(port)
        if not remaining and not listeners:
            stopped = len(pids)
            break
        time.sleep(0.2)
    else:
        remaining = [pid for pid in pids if _pid_alive(pid)]
        for pid in remaining:
            _signal_bridge_pid(pid, signal.SIGKILL)
        # Final sweep for any remaining listener process on the bridge port.
        for pid in _find_bridge_pids(port):
            _signal_bridge_pid(pid, signal.SIGKILL)
        stopped = len(pids)

    _bridge_pid_path().unlink(missing_ok=True)
    return stopped


@bridge_app.command("start")
def bridge_start(
    port: int = typer.Option(None, "--port", "-p", help="Bridge port (default: from config bridge_url)"),
):
    """Start WhatsApp bridge in background."""
    import os
    import shutil
    import subprocess
    import time

    if not shutil.which("node"):
        console.print("[red]node not found. Please install Node.js >= 18.[/red]")
        raise typer.Exit(1)

    bridge_dir = _get_bridge_dir()
    resolved_port = _bridge_port_from_config() if port is None else port

    running = _find_bridge_pids(resolved_port)
    if running:
        console.print(f"[yellow]Bridge already running (pid {running[0]})[/yellow]")
        console.print(f"Log: {_bridge_log_path()}")
        return

    log_path = _bridge_log_path()
    with open(log_path, "a") as log_file:
        env = dict(os.environ)
        env["BRIDGE_PORT"] = str(resolved_port)
        proc = subprocess.Popen(
            ["node", "dist/index.js"],
            cwd=bridge_dir,
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )

    time.sleep(0.6)
    if proc.poll() is not None:
        console.print("[red]Bridge failed to start. Check log:[/red]")
        console.print(log_path)
        raise typer.Exit(1)

    _bridge_pid_path().write_text(str(proc.pid))
    console.print(f"[green]✓[/green] Bridge started (pid {proc.pid}, port {resolved_port})")
    console.print(f"Log: {log_path}")


@bridge_app.command("stop")
def bridge_stop(
    port: int = typer.Option(None, "--port", "-p", help="Bridge port (default: from config bridge_url)"),
):
    """Stop WhatsApp bridge."""
    resolved_port = _bridge_port_from_config() if port is None else port
    stopped = _stop_bridge_processes(resolved_port)
    if stopped == 0:
        console.print("[yellow]Bridge is not running[/yellow]")
        return
    console.print(f"[green]✓[/green] Bridge stopped ({stopped} process{'es' if stopped != 1 else ''})")


@bridge_app.command("restart")
def bridge_restart(
    port: int = typer.Option(None, "--port", "-p", help="Bridge port (default: from config bridge_url)"),
):
    """Restart WhatsApp bridge."""
    import time

    resolved_port = _bridge_port_from_config() if port is None else port
    _stop_bridge_processes(resolved_port)
    # Give OS/socket state a brief moment before re-check/start.
    time.sleep(0.2)
    if _find_bridge_pids(resolved_port):
        console.print(f"[red]Bridge restart failed: port {resolved_port} is still in use[/red]")
        raise typer.Exit(1)
    bridge_start(port=resolved_port)


@bridge_app.command("status")
def bridge_status(
    port: int = typer.Option(None, "--port", "-p", help="Bridge port (default: from config bridge_url)"),
):
    """Show WhatsApp bridge status."""
    resolved_port = _bridge_port_from_config() if port is None else port
    running = _find_bridge_pids(resolved_port)
    if not running:
        console.print(f"[yellow]Bridge not running on port {resolved_port}[/yellow]")
        return
    console.print(f"[green]Bridge running[/green] on port {resolved_port} (pid {running[0]})")
    console.print(f"Log: {_bridge_log_path()}")


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
        "web_search",
        "web_fetch",
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
    from nanobot.config.loader import load_config
    from nanobot.policy.middleware import PolicyMiddleware

    config = load_config()
    policy_engine, policy_path = _make_policy_engine(config)
    policy_engine.validate(_policy_known_tools())
    middleware = PolicyMiddleware(
        engine=policy_engine,
        known_tools=_policy_known_tools(),
        policy_path=policy_path,
        reload_on_change=False,
    )
    report = middleware.explain(
        channel=channel,
        chat_id=chat_id,
        sender_id=sender_id,
        is_group=is_group,
        mentioned_bot=mentioned_bot,
        reply_to_bot=reply_to_bot,
    )
    console.print_json(json.dumps(report, ensure_ascii=False, indent=2))


@policy_app.command("migrate-allowfrom")
def policy_migrate_allowfrom(
    dry_run: bool = typer.Option(False, "--dry-run", help="Show migration result without writing policy.json"),
):
    """Migrate legacy channels.*.allowFrom into policy defaults."""
    import shutil

    from nanobot.config.loader import get_config_path
    from nanobot.policy.loader import (
        get_policy_path,
        load_legacy_allow_from,
        load_policy,
        migrate_allow_from,
        save_policy,
    )

    config_path = get_config_path()
    policy_path = get_policy_path()
    legacy_allow_from = load_legacy_allow_from(config_path)
    policy = load_policy(policy_path)
    migrated, notes, changed = migrate_allow_from(policy, legacy_allow_from)

    if notes:
        console.print("Migration notes:")
        for note in notes:
            console.print(f"- {note}")
    else:
        console.print("No legacy allowFrom entries found.")

    if not changed:
        return

    if dry_run:
        console.print("[yellow]Dry run only. No files were changed.[/yellow]")
        return

    if policy_path.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = policy_path.with_name(f"{policy_path.name}.bak-{stamp}")
        shutil.copy2(policy_path, backup_path)
        console.print(f"[green]✓[/green] Backup written: {backup_path}")

    save_policy(migrated, policy_path)
    console.print(f"[green]✓[/green] Updated policy: {policy_path}")


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
    from nanobot.config.loader import get_data_dir
    from nanobot.cron.service import CronService

    store_path = get_data_dir() / "cron" / "jobs.json"
    service = CronService(store_path)

    jobs = service.list_jobs(include_disabled=all)

    if not jobs:
        console.print("No scheduled jobs.")
        return

    table = Table(title="Scheduled Jobs")
    table.add_column("ID", style="cyan")
    table.add_column("Name")
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
            next_time = time.strftime("%Y-%m-%d %H:%M", time.localtime(job.state.next_run_at_ms / 1000))
            next_run = next_time

        status = "[green]enabled[/green]" if job.enabled else "[dim]disabled[/dim]"

        table.add_row(job.id, job.name, sched, status, next_run)

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
    channel: str = typer.Option(None, "--channel", help="Channel for delivery (e.g. 'telegram', 'whatsapp')"),
):
    """Add a scheduled job."""
    from nanobot.config.loader import get_data_dir
    from nanobot.cron.service import CronService
    from nanobot.cron.types import CronSchedule

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

    store_path = get_data_dir() / "cron" / "jobs.json"
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


@cron_app.command("remove")
def cron_remove(
    job_id: str = typer.Argument(..., help="Job ID to remove"),
):
    """Remove a scheduled job."""
    from nanobot.config.loader import get_data_dir
    from nanobot.cron.service import CronService

    store_path = get_data_dir() / "cron" / "jobs.json"
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
    from nanobot.config.loader import get_data_dir
    from nanobot.cron.service import CronService

    store_path = get_data_dir() / "cron" / "jobs.json"
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
    from nanobot.config.loader import get_data_dir
    from nanobot.cron.service import CronService

    store_path = get_data_dir() / "cron" / "jobs.json"
    service = CronService(store_path)

    async def run():
        return await service.run_job(job_id, force=force)

    if asyncio.run(run()):
        console.print("[green]✓[/green] Job executed")
    else:
        console.print(f"[red]Failed to run job {job_id}[/red]")


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

    console.print(f"Config: {config_path} {'[green]✓[/green]' if config_path.exists() else '[red]✗[/red]'}")
    console.print(f"Policy: {policy_path} {'[green]✓[/green]' if policy_path.exists() else '[red]✗[/red]'}")
    console.print(f"Workspace: {workspace} {'[green]✓[/green]' if workspace.exists() else '[red]✗[/red]'}")

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
                console.print(f"{spec.label}: {'[green]✓[/green]' if has_key else '[dim]not set[/dim]'}")


if __name__ == "__main__":
    app()
