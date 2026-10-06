"""Read-only ProcessingStore health commands."""

from __future__ import annotations

import sqlite3
from contextlib import closing

import typer

from .core import app

processing_app = typer.Typer(help="Read-only processing journal checks")
app.add_typer(processing_app, name="processing")


@processing_app.command("last-message-ms")
def last_message_ms() -> None:
    """Print the latest message event timestamp in milliseconds, or zero."""
    from yeoman_shared.utils.helpers import get_operational_store_path

    database = get_operational_store_path("processing")
    if not database.is_file():
        typer.echo("0")
        return
    with closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)) as connection:
        row = connection.execute(
            "SELECT MAX(created_ms) FROM events WHERE kind = 'message'"
        ).fetchone()
    typer.echo(str(row[0] or 0))
