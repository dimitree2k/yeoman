"""``yeoman knowledge digest run|show`` on synthetic history and a fake model client."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner
from yeoman_gateway.cli import knowledge_commands
from yeoman_gateway.cli.commands import app
from yeoman_shared.config.schema import Config

from tests.gateway.test_group_digest import CHAT, WEEK, Harness, _busy_week

runner = CliRunner()


class _Unclosed:
    """The CLI closes what it opened; the test keeps reading afterwards."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def close(self) -> None:
        return None


@pytest.fixture
def cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from yeoman_shared.config import loader

    monkeypatch.setenv("YEOMAN_HOME", str(tmp_path / "home"))
    harness = Harness(tmp_path)
    _busy_week(harness.history)
    config = Config()
    monkeypatch.setattr(loader, "load_config", lambda *args, **kwargs: config)
    monkeypatch.setattr(knowledge_commands, "_digest_history", lambda: _Unclosed(harness.history))
    monkeypatch.setattr(
        knowledge_commands, "_open_capture_runtime",
        lambda: (config, _Unclosed(harness.knowledge), _Unclosed(object())),
    )
    monkeypatch.setattr("yeoman_gateway.processing.model_route.RouteClient",
                        lambda **kwargs: harness.client)
    try:
        yield harness
    finally:
        harness.close()


def test_dry_run_prints_aggregates_only_and_calls_no_model(
    cli: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_client(**kwargs: Any) -> None:
        pytest.fail("a dry run must not build a model client")

    monkeypatch.setattr("yeoman_gateway.processing.model_route.RouteClient", no_client)
    result = runner.invoke(app, ["knowledge", "digest", "run", "--chat", CHAT,
                                 "--week", WEEK.label, "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "synthetic topic" not in result.output and "synthetic arvid" not in result.output
    assert "minimum signal met" in result.output and "model calls: 0" in result.output
    payload = json.loads(result.output[: result.output.rindex("}") + 1])
    assert payload["week"] == "2026-W40" and payload["conversations"] == 6
    assert cli.knowledge.group_digests() == ()


def test_run_makes_one_call_and_show_lists_the_note(cli: Harness) -> None:
    result = runner.invoke(app, ["knowledge", "digest", "run", "--chat", f"whatsapp:{CHAT}",
                                 "--week", WEEK.label])
    assert result.exit_code == 0, result.output
    assert "week 2026-W40: done; model calls: 1" in result.output
    assert "Goes down well: Short dry jokes about markets" in result.output
    assert len(cli.client.calls) == 1
    cli.knowledge.enqueue_group_digest(chat_id=CHAT, week="2026-W41", now_ms=1)
    shown = runner.invoke(app, ["knowledge", "digest", "show", "--chat", CHAT])
    assert shown.exit_code == 0, shown.output
    assert "[Group notes, week 2026-W40]" in shown.output
    assert "v1 · active · confidence medium" in shown.output
    assert "weekly digest jobs" in shown.output and "2026-W41" in shown.output


def test_run_without_the_route_fails_with_a_hint(
    cli: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    from yeoman_gateway.processing.model_route import RouteUnavailableError

    def missing(**kwargs: Any) -> None:
        raise RouteUnavailableError("models.routes missing 'participation.digest'")

    monkeypatch.setattr("yeoman_gateway.processing.model_route.RouteClient", missing)
    result = runner.invoke(app, ["knowledge", "digest", "run", "--chat", CHAT,
                                 "--week", WEEK.label])
    assert result.exit_code == 2
    assert "participation.digest" in result.output
    assert cli.knowledge.group_digests() == ()


def test_invalid_output_exits_non_zero_and_stores_nothing(cli: Harness) -> None:
    cli.client.answers = ["not json"]
    result = runner.invoke(app, ["knowledge", "digest", "run", "--chat", CHAT,
                                 "--week", WEEK.label])
    assert result.exit_code == 2
    assert "failed (invalid_json)" in result.output
    assert cli.knowledge.group_digests() == ()


def test_bad_week_is_refused(cli: Harness) -> None:
    result = runner.invoke(app, ["knowledge", "digest", "run", "--chat", CHAT,
                                 "--week", "2026-42", "--dry-run"])
    assert result.exit_code == 2
    assert "--week" in result.output

