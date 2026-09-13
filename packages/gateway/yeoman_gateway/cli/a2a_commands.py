"""Standalone Hermes/Yeoman A2A relay commands."""

from __future__ import annotations

from pathlib import Path

import typer
from dotenv import load_dotenv

from yeoman_gateway.cli.core import app

a2a_app = typer.Typer(help="Run and validate the standalone A2A relay.")
app.add_typer(a2a_app, name="a2a")


def _a2a_env_path() -> Path:
    return Path.home() / ".yeoman" / "secrets" / "a2a.env"


@a2a_app.command()
def serve() -> None:
    """Run the standalone authenticated A2A relay."""
    from yeoman_gateway.a2a import relay

    load_dotenv(_a2a_env_path(), override=False)
    raise typer.Exit(relay.main())


@a2a_app.command()
def conformance(
    contracts_checkout: Path = typer.Option(
        ..., "--contracts-checkout", exists=True, file_okay=False, resolve_path=True
    ),
) -> None:
    """Validate contract-provided positive and negative fixtures."""
    from yeoman_gateway.a2a import conformance as checker

    raise typer.Exit(checker.main(contracts_checkout))
