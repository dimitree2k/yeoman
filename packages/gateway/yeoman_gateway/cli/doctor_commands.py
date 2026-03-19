"""Doctor/diagnostic CLI commands."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import typer

from .core import app, console


def _builtin_doctor_script() -> Path:
    return Path(__file__).resolve().parent.parent / "skills" / "agent-doctor" / "scripts" / "doctor.sh"


def _workspace_doctor_script() -> Path:
    return Path.home() / ".yeoman" / "workspace" / "skills" / "agent-doctor" / "scripts" / "doctor.sh"


@app.command("doctor")
def doctor() -> None:
    """Run the yeoman health check."""
    workspace_script = _workspace_doctor_script()
    builtin_script = _builtin_doctor_script()
    script = workspace_script if workspace_script.exists() else builtin_script

    if not script.exists():
        console.print("[red]Doctor script not found.[/red]")
        raise typer.Exit(1)

    env = dict(os.environ)
    env.setdefault("YEOMAN_BIN", "yeoman")

    try:
        completed = subprocess.run(["bash", str(script)], env=env, check=False)
    except FileNotFoundError:
        console.print("[red]bash not found.[/red]")
        raise typer.Exit(1)

    if completed.returncode:
        raise typer.Exit(completed.returncode)
