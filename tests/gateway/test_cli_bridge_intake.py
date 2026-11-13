from __future__ import annotations

import pytest
from typer.testing import CliRunner
from yeoman_gateway.cli import bridge_client
from yeoman_gateway.cli.channel_commands import (
    INTAKE_EXIT_STOPPED,
    INTAKE_EXIT_UNREACHABLE,
    bridge_app,
)

runner = CliRunner()


def _health(monkeypatch: pytest.MonkeyPatch, result: object) -> None:
    def fake(command_type: str, *args: object, **kwargs: object) -> object:
        assert command_type == "health"
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(bridge_client, "bridge_command", fake)


def test_intake_running_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    _health(monkeypatch, {"intakeStopped": False, "persistenceFailure": False})
    result = runner.invoke(bridge_app, ["intake"])
    assert result.exit_code == 0
    assert "accepting" in result.output


def test_intake_stopped_exits_three(monkeypatch: pytest.MonkeyPatch) -> None:
    _health(monkeypatch, {"intakeStopped": True, "persistenceFailure": True})
    result = runner.invoke(bridge_app, ["intake"])
    assert result.exit_code == INTAKE_EXIT_STOPPED
    assert "persistence failure" in result.output


def test_unreachable_bridge_exits_two(monkeypatch: pytest.MonkeyPatch) -> None:
    _health(monkeypatch, ConnectionRefusedError())
    result = runner.invoke(bridge_app, ["intake"])
    assert result.exit_code == INTAKE_EXIT_UNREACHABLE
