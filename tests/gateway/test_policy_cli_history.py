from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from typer.testing import CliRunner
from yeoman_gateway.cli.commands import app
from yeoman_gateway.policy.loader import save_policy
from yeoman_gateway.policy.schema import PolicyConfig


def test_policy_cli_group_discovery_uses_gateway_history_chats(
    tmp_path: Path, monkeypatch
) -> None:
    policy_path = tmp_path / "policy.json"
    save_policy(PolicyConfig.model_validate({}), policy_path)
    endpoint = Mock(
        return_value={
            "status": "ready",
            "chats": [{"chat_id": "planning@g.us", "subject": "Planning"}],
        }
    )
    monkeypatch.setattr("yeoman_gateway.history.export.cli_secondary_request", endpoint)
    monkeypatch.setattr(
        "yeoman_shared.config.loader.load_config",
        lambda: SimpleNamespace(workspace_path=tmp_path, processing=object()),
    )
    monkeypatch.setattr(
        "yeoman_gateway.cli.policy_commands.make_policy_engine",
        lambda config: (None, policy_path),
    )

    result = CliRunner().invoke(app, ["policy", "cmd", "/policy list-groups"])

    assert result.exit_code == 0, result.output
    assert "planning@g.us" in result.output
    assert "history" in result.output
    assert "Planning" in result.output
    endpoint.assert_called_once_with("history_chats", {})
