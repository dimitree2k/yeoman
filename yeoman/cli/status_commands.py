"""General status/logging CLI commands."""

from __future__ import annotations

from pathlib import Path

import typer

from yeoman import __logo__

from .channel_commands import _bridge_log_path
from .core import app, console
from .gateway_commands import _gateway_log_path


@app.command()
def logs(
    follow: bool = typer.Option(True, "--follow/--no-follow", help="Follow log output"),
    lines: int = typer.Option(200, "--lines", "-n", min=1, help="Initial lines for tail mode"),
    gateway: bool = typer.Option(True, "--gateway/--no-gateway", help="Include gateway log"),
    bridge: bool = typer.Option(True, "--bridge/--no-bridge", help="Include WhatsApp bridge log"),
    raw: bool = typer.Option(False, "--raw", help="Use tail instead of lnav"),
) -> None:
    """View yeoman logs (uses lnav when available)."""
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
def status() -> None:
    """Show yeoman status."""
    from yeoman.config.loader import get_config_path, load_config
    from yeoman.policy.loader import get_policy_path

    config_path = get_config_path()
    policy_path = get_policy_path()
    config = load_config()
    workspace = config.workspace_path

    console.print(f"{__logo__} yeoman Status\n")

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
        from yeoman.providers.registry import PROVIDERS

        console.print(f"Model: {config.agents.defaults.model}")

        for spec in PROVIDERS:
            provider = getattr(config.providers, spec.name, None)
            if provider is None:
                continue
            if spec.is_local:
                if provider.api_base:
                    console.print(f"{spec.label}: [green]✓ {provider.api_base}[/green]")
                else:
                    console.print(f"{spec.label}: [dim]not set[/dim]")
            else:
                has_key = bool(provider.api_key)
                console.print(
                    f"{spec.label}: {'[green]✓[/green]' if has_key else '[dim]not set[/dim]'}"
                )
