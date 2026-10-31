"""``yeoman sentiment report|validate`` on a synthetic store (no gateway)."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner
from yeoman_gateway.cli import sentiment_commands
from yeoman_gateway.cli.commands import app
from yeoman_gateway.reception.store import SentimentStore

CHAT = "120363000000000042@g.us"
runner = CliRunner()


def _store(tmp_path: Path) -> SentimentStore:
    store = SentimentStore(tmp_path / "ops" / "sentiment.db")
    store.store_weekly(
        chat_id=CHAT, iso_week="2026-W41", computed_at_ms=1, status="ok",
        persona_file="personas/arvid.md", persona_sha="sha",
        policy_snapshot={}, metrics={
            "arvid_messages": 8, "human_messages": 60, "addressed": 3,
            "unprompted": 5, "direct_rate": 0.5, "indirect_rate": 0.2,
            "silence_rate": 0.1, "reactions_per_arvid": 0.4,
            "reactions_per_human": 0.1, "negative_reactions": 1,
            "breadth": 3, "top_person_share": 0.5,
        },
    )
    store.store_label(
        chat_id=CHAT, iso_week="2026-W41", arvid_message_id="a", message_id="m",
        responds_to_arvid="yes", stance="playful", signals=(), confidence=0.9,
        model="fake", prompt_version="v1", labeled_at_ms=1,
    )
    return store


def test_report_reads_the_store_and_renders_a_table(
    tmp_path: Path, monkeypatch
) -> None:
    store = _store(tmp_path)
    monkeypatch.setattr(sentiment_commands, "_store_read_only", lambda: store)
    monkeypatch.setattr(
        sentiment_commands, "_policy_chat_maps", lambda: ({CHAT: "Ente"}, [CHAT])
    )
    result = runner.invoke(app, ["sentiment", "report", "--chat", "Ente"])
    assert result.exit_code == 0, result.output
    assert "Ente" in result.output and "direct 50%" in result.output
    assert "stance pla 1" in result.output


def test_validate_reports_agreement_from_a_csv(tmp_path: Path, monkeypatch) -> None:
    store = _store(tmp_path)
    monkeypatch.setattr(sentiment_commands, "_store_read_only", lambda: store)
    csv_path = tmp_path / "labels.csv"
    csv_path.write_text(
        "message_id,expected_responds,expected_stance\nm,yes,playful\nmissing,yes,neutral\n",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["sentiment", "validate", "--labels", str(csv_path)])
    assert result.exit_code == 0, result.output
    assert "responds_accuracy" in result.output
    assert "missing_labels" in result.output and "negative_accuracy" in result.output
    assert "yes->missing" not in result.output


def test_run_fails_loudly_when_days_got_no_labels(tmp_path: Path, monkeypatch) -> None:
    from types import SimpleNamespace

    from yeoman_shared.config import loader

    monkeypatch.setattr(
        loader, "load_config",
        lambda: SimpleNamespace(ipc=SimpleNamespace(resolved_gateway_socket_path=tmp_path / "s")),
    )
    monkeypatch.setattr(sentiment_commands, "_policy_chat_maps", lambda: ({}, [CHAT]))

    async def reply(_path, _args):
        return {"status": "ok", "weeks": 2, "labels": 0, "failed_days": 3}

    monkeypatch.setattr(sentiment_commands, "_request_sentiment_run", reply)
    result = runner.invoke(app, ["sentiment", "run", "--since", "2026-10-16"])
    assert result.exit_code == 1, result.output
    assert "3 Tage ohne Labels" in result.output
