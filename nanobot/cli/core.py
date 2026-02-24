"""Shared CLI application context and setup helpers."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

from nanobot import __logo__, __version__

app = typer.Typer(
    name="nanobot",
    help=f"{__logo__} nanobot-stack (compat: nanobot) - Personal AI Assistant",
    no_args_is_help=True,
)

console = Console()


def version_callback(value: bool) -> None:
    if value:
        console.print(f"{__logo__} nanobot-stack v{__version__} (compat command: nanobot)")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(None, "--version", "-v", callback=version_callback, is_eager=True),
) -> None:
    """nanobot-stack (compat: nanobot) - Personal AI Assistant."""


def _create_workspace_templates(workspace: Path) -> None:
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


@app.command()
def onboard() -> None:
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

    config = Config()
    save_config(config)
    console.print(f"[green]✓[/green] Created config at {config_path}")
    policy_path = ensure_policy_file()
    console.print(f"[green]✓[/green] Created policy at {policy_path}")

    workspace = get_workspace_path()
    console.print(f"[green]✓[/green] Created workspace at {workspace}")
    _create_workspace_templates(workspace)

    console.print(f"\n{__logo__} nanobot-stack is ready!")
    console.print("\nNext steps:")
    console.print("  1. Add your API key to [cyan]~/.nanobot/config.json[/cyan]")
    console.print("     Get one at: https://openrouter.ai/keys")
    console.print('  2. Chat: [cyan]nanobot agent -m "Hello!"[/cyan]')
    console.print("\n[dim]Want Telegram/WhatsApp? See project README > Chat Apps[/dim]")


def make_provider(config):
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

    provider_cfg = config.get_provider(model)
    if not (provider_cfg and provider_cfg.api_key) and not model.startswith("bedrock/"):
        console.print("[red]Error: No API key configured.[/red]")
        console.print("Set one in ~/.nanobot/config.json under providers section")
        raise typer.Exit(1)
    return LiteLLMProvider(
        api_key=provider_cfg.api_key if provider_cfg else None,
        api_base=provider_cfg.api_base if provider_cfg else None,
        default_model=model,
        extra_headers=provider_cfg.extra_headers if provider_cfg else None,
    )


def make_memory_service(config):
    """Create memory service from config/workspace."""
    from nanobot.memory import MemoryService

    return MemoryService(workspace=config.workspace_path, config=config.memory, root_config=config)


def make_policy_engine(config):
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
