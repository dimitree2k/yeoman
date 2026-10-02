from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner
from yeoman_gateway.cli.commands import app
from yeoman_gateway.evaluation import judge_format
from yeoman_gateway.evaluation.harness import StaticClient
from yeoman_gateway.evaluation.judge_format import FormatSummary
from yeoman_gateway.evaluation.report import new_report_dir, write_report

runner = CliRunner()


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("YEOMAN_HOME", str(tmp_path))
    return tmp_path


def _latest(home: Path, kind: str) -> Path:
    [directory] = sorted((home / "var" / "eval" / "memory").glob(f"*-{kind}"))
    return directory


def test_retrieval_command_writes_json_and_markdown(home: Path) -> None:
    result = runner.invoke(app, ["eval", "retrieval"])
    assert result.exit_code == 0, result.output
    directory = _latest(home, "retrieval")
    report = json.loads((directory / "report.json").read_text())
    assert report["overall"]["memory_items_mean"] == 0.0
    assert (directory / "report.md").read_text().startswith("# Retrieval")


def test_discretion_stub_run_needs_no_config_or_model(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from yeoman_shared.config import loader

    def unexpected_call(*_args: object, **_kwargs: object) -> None:
        pytest.fail("stub evaluation must not load config or build a model client")

    monkeypatch.setattr(loader, "load_config", unexpected_call)
    monkeypatch.setattr(judge_format, "build_eval_client", unexpected_call)
    result = runner.invoke(app, ["eval", "discretion", "--stub", "--runs", "1"])
    assert result.exit_code == 0, result.output
    directory = _latest(home, "discretion")
    report = json.loads((directory / "report.json").read_text())
    statuses = {row["scenario_id"]: row["status"] for row in report["results"]}
    assert statuses["S2"] == "pending" and statuses["S1"] == "pass"
    assert (directory / "report.md").read_text().startswith("# Discretion suite")


def test_judge_format_writes_report_with_a_stubbed_harness(
    home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from yeoman_shared.config import loader
    from yeoman_shared.config.schema import Config

    summary = FormatSummary(
        route="eval.judge", model="stub", structured_output="prompt_only", attempts=1,
        failures=0, failure_rate=0.0, by_class={}, latency_ms_p50=0, latency_ms_p90=0,
    )

    async def run_stub(client, scenarios, *, runs, allowed_emojis, record_dir=None):
        assert isinstance(client, StaticClient)
        assert scenarios and all(not scenario.requires for scenario in scenarios)
        assert runs == 1 and isinstance(allowed_emojis, tuple) and record_dir is None
        return summary

    monkeypatch.setattr(judge_format, "build_eval_client", lambda *_args, **_kwargs: StaticClient("{}"))
    monkeypatch.setattr(judge_format, "run_judge_format", run_stub)
    monkeypatch.setattr(loader, "load_config", Config)
    result = runner.invoke(app, ["eval", "judge-format", "--profile", "test", "--runs", "1"])
    assert result.exit_code == 0, result.output
    directory = _latest(home, "judge-format")
    report = json.loads((directory / "report.json").read_text())
    assert report["model"] == "stub" and report["attempts"] == 1
    assert (directory / "report.md").read_text().startswith("# Judge output format")


def test_judge_format_rejects_an_unknown_profile_before_client_creation(
    home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_client(**_kwargs: object) -> None:
        pytest.fail("unknown profiles must fail before provider construction")

    monkeypatch.setattr(judge_format, "RouteClient", unexpected_client)
    result = runner.invoke(app, ["eval", "judge-format", "--profile", "nope"])
    assert result.exit_code == 2
    assert "unknown profile" in result.output
    assert not (home / "var" / "eval").exists()


def test_report_dirs_are_unique_and_writer_saves_both_formats(tmp_path: Path) -> None:
    first = new_report_dir("x", base=tmp_path)
    second = new_report_dir("x", base=tmp_path)
    assert first != second and first.is_dir() and second.is_dir()

    write_report(first, {"ok": True}, "# report\n")
    assert json.loads((first / "report.json").read_text()) == {"ok": True}
    assert (first / "report.md").read_text() == "# report\n"
