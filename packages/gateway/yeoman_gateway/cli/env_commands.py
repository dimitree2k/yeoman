"""CLI environment inspection commands."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from .core import app, console


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _launcher_python(path: Path) -> str | None:
    try:
        first_line = path.read_text(encoding="utf-8").splitlines()[0]
    except (OSError, IndexError, UnicodeDecodeError):
        return None
    if not first_line.startswith("#!"):
        return None
    shebang = first_line[2:].strip()
    return shebang or None


@app.command("env")
def env() -> None:
    """Show the active yeoman launcher and Python runtime."""
    repo_root = _project_root()
    repo_wrapper = repo_root / "bin" / "yeoman"
    repo_python = repo_root / ".venv" / "bin" / "python3"
    package_root = Path(__file__).resolve().parents[1]
    workspace_root = Path.home() / ".yeoman"
    invoked_via = os.environ.get("YEOMAN_LAUNCHER_PATH")
    launcher_kind = os.environ.get("YEOMAN_LAUNCHER_KIND")

    active_python = Path(sys.executable).resolve()
    active_launcher_raw = shutil.which("yeoman")
    active_launcher = Path(active_launcher_raw).resolve() if active_launcher_raw else None
    active_launcher_python = (
        _launcher_python(active_launcher) if active_launcher and active_launcher.is_file() else None
    )

    mode = "installed/runtime"
    if repo_python.exists() and active_python == repo_python.resolve():
        mode = "source checkout (.venv)"

    console.print("[bold]yeoman Environment[/bold]")
    console.print(f"mode: {mode}")
    console.print(f"python: {active_python}")
    console.print(f"package: {package_root}")
    console.print(f"workspace: {workspace_root}")
    if invoked_via:
        console.print(f"invoked_via: {invoked_via}")
        if launcher_kind:
            console.print(f"launcher_kind: {launcher_kind}")

    if active_launcher is not None:
        console.print(f"launcher: {active_launcher}")
        if active_launcher_python:
            console.print(f"launcher_python: {active_launcher_python}")
    else:
        console.print("launcher: (not found in PATH)")

    console.print(
        f"repo_wrapper: {repo_wrapper} {'[green]✓[/green]' if repo_wrapper.exists() else '[dim]missing[/dim]'}"
    )
    console.print(
        f"repo_python: {repo_python} {'[green]✓[/green]' if repo_python.exists() else '[dim]missing[/dim]'}"
    )

    if mode == "source checkout (.venv)":
        console.print("\n[green]Recommended:[/green] use ./bin/yeoman inside this checkout.")
    else:
        console.print("\n[yellow]Recommended:[/yellow] use the installed 'yeoman' CLI for user/runtime operation.")
        if repo_wrapper.exists():
            console.print(f"[dim]For source development, switch explicitly to {repo_wrapper}.[/dim]")

    if active_launcher is not None and repo_wrapper.exists() and active_launcher != repo_wrapper.resolve():
        console.print(
            "[dim]PATH 'yeoman' is not the repo wrapper. Mixing launcher and Python environments can cause dependency mismatches.[/dim]"
        )
