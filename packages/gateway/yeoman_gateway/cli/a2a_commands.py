"""Standalone Hermes/Yeoman A2A relay commands."""

from __future__ import annotations

from pathlib import Path

import typer
from dotenv import load_dotenv
from yeoman_shared.utils.helpers import get_data_path, get_operational_store_path

from yeoman_gateway.cli.core import app

a2a_app = typer.Typer(help="Run and validate the standalone A2A relay.")
app.add_typer(a2a_app, name="a2a")


def _a2a_env_path() -> Path:
    return Path.home() / ".yeoman" / "secrets" / "a2a.env"


def _a2a_capabilities_env_path() -> Path:
    return Path.home() / ".yeoman" / "a2a-capabilities.env"


@a2a_app.command()
def serve() -> None:
    """Run the standalone authenticated A2A relay."""
    from yeoman_gateway.a2a import relay

    load_dotenv(_a2a_capabilities_env_path(), override=False)
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


@a2a_app.command("migrate-store")
def migrate_store(
    relay_source: Path | None = typer.Option(None, "--relay-source", exists=True, dir_okay=False),
    research_source: Path | None = typer.Option(None, "--research-source", exists=True, dir_okay=False),
    target: Path | None = typer.Option(None, "--target"),
) -> None:
    """Explicitly merge the two frozen legacy stores into data/ops/a2a.db."""
    from yeoman_gateway.a2a.store_migration import migrate_a2a_stores

    data = get_data_path() / "data"
    report = migrate_a2a_stores(
        relay_source or data / "a2a" / "relay.db",
        research_source or data / "processing" / "a2a-research.db",
        target or get_operational_store_path("a2a"),
    )
    typer.echo(report.to_json())
