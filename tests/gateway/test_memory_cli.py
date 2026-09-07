from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner
from yeoman_gateway.cli.commands import app
from yeoman_shared.config.loader import save_config
from yeoman_shared.config.schema import Config

runner = CliRunner()


def test_memory_cli_commands_end_to_end(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    cfg = Config()
    save_config(cfg)

    add = runner.invoke(
        app,
        [
            "memory",
            "add",
            "--text",
            "I prefer dark mode",
            "--kind",
            "preference",
            "--scope",
            "user",
            "--channel",
            "cli",
            "--chat-id",
            "direct",
            "--sender-id",
            "u1",
        ],
    )
    assert add.exit_code == 0, add.output
    assert "memory entry" in add.output

    search = runner.invoke(
        app,
        [
            "memory",
            "search",
            "--query",
            "dark mode",
            "--channel",
            "cli",
            "--chat-id",
            "direct",
            "--sender-id",
            "u1",
            "--scope",
            "user",
        ],
    )
    assert search.exit_code == 0, search.output
    assert "preference" in search.output.lower()

    status = runner.invoke(app, ["memory", "status"])
    assert status.exit_code == 0, status.output
    assert "backend" in status.output
    assert "total_active" in status.output

    prune = runner.invoke(app, ["memory", "prune", "--older-than-days", "0", "--dry-run"])
    assert prune.exit_code == 0, prune.output
    assert "Dry run" in prune.output

    reindex = runner.invoke(app, ["memory", "reindex"])
    assert reindex.exit_code == 0, reindex.output
    assert "rebuilt" in reindex.output.lower()
