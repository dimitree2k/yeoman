"""The memory CLI uses bounded Gateway reads and refuses retired maintenance."""
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner
from yeoman_gateway.cli.commands import app


@pytest.fixture(autouse=True)
def isolated_memory_cli_config(tmp_path, monkeypatch):
    monkeypatch.setattr("yeoman_shared.config.loader.get_config_path", lambda: tmp_path / "config.json")


def test_memory_search_uses_authorized_gateway(monkeypatch):
    endpoint = Mock(return_value={"statements": ["synthetic statement"]})
    monkeypatch.setattr("yeoman_gateway.history.export.cli_secondary_request", endpoint)
    result = CliRunner().invoke(app, ["memory", "search", "--query", "synthetic",
        "--channel", "whatsapp", "--chat-id", "g@g.us", "--scope", "chat"])
    assert result.exit_code == 0, result.output
    assert "synthetic statement" in result.output
    endpoint.assert_called_once_with("knowledge_read", {"chat_id": "g@g.us", "query": "synthetic", "limit": 8})


def test_memory_search_requires_explicit_whatsapp_chat(monkeypatch):
    endpoint = Mock(side_effect=AssertionError("unscoped read"))
    monkeypatch.setattr("yeoman_gateway.history.export.cli_secondary_request", endpoint)
    result = CliRunner().invoke(app, ["memory", "search", "--query", "synthetic"])
    assert result.exit_code != 0
    assert "explicit WhatsApp chat scope required" in result.output
    endpoint.assert_not_called()


@pytest.mark.parametrize("args", [["reindex"], ["prune", "--older-than-days", "0", "--dry-run"],
    ["add", "--text", "synthetic", "--kind", "preference", "--scope", "global"]])
def test_memory_cli_refuses_retired_maintenance(args):
    result = CliRunner().invoke(app, ["memory", *args])
    assert result.exit_code != 0
    assert "legacy maintenance is offline only" in result.output
