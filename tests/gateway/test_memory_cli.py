from __future__ import annotations

import sqlite3
from pathlib import Path

from typer.testing import CliRunner
from yeoman_gateway.cli.commands import app

runner = CliRunner()


def test_memory_media_growth_cli_is_read_only(tmp_path: Path) -> None:
    processing_db = tmp_path / "processing.db"
    connection = sqlite3.connect(processing_db)
    try:
        connection.execute(
            "CREATE TABLE events(event_id TEXT, occurred_at_ms INTEGER, payload_json TEXT)"
        )
        connection.commit()
    finally:
        connection.close()

    result = runner.invoke(
        app, ["memory", "media-growth", "--processing-db", str(processing_db)]
    )

    assert result.exit_code == 0, result.output
    assert "Media growth (read-only, no deletion)" in result.output
    assert processing_db.exists()
