import hashlib
import sqlite3
from pathlib import Path

from typer.testing import CliRunner

from yeoman_gateway.cli.commands import app


runner = CliRunner()


def test_last_message_ms_reads_resolved_database_without_modifying_it(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "home"
    db = home / "data" / "ops" / "processing.db"
    db.parent.mkdir(parents=True)
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE events (kind TEXT, created_ms INTEGER)")
        connection.executemany(
            "INSERT INTO events VALUES (?, ?)", [("message", 123), ("other", 999), ("message", 456)]
        )
    before = hashlib.sha256(db.read_bytes()).digest()
    monkeypatch.setenv("YEOMAN_HOME", str(home))

    result = runner.invoke(app, ["processing", "last-message-ms"])

    assert result.exit_code == 0, result.output
    assert result.output.strip() == "456"
    assert hashlib.sha256(db.read_bytes()).digest() == before


def test_last_message_ms_does_not_create_missing_database(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("YEOMAN_HOME", str(home))

    result = runner.invoke(app, ["processing", "last-message-ms"])

    assert result.exit_code == 0, result.output
    assert result.output.strip() == "0"
    assert not (home / "data" / "ops" / "processing.db").exists()
