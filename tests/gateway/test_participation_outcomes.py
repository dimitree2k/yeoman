from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from yeoman_gateway.evaluation.participation_outcomes import (
    evaluate,
    render_markdown,
    write_outputs,
)
from yeoman_gateway.history.schema import create


def _db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _message(conn, message_id, native_id, chat, direction, at, sender, reply=None):
    conn.execute(
        "INSERT INTO messages (message_id,channel,chat_id,native_message_id,sender_identifier,"
        "sender_basis,direction,sent_ms,time_certainty,text,media_json,reply_to_native_id,"
        "mentions_json,provenance,source_refs) VALUES (?,?,?,?,?,'unknown',?,?, 'native',"
        "'private fixture text',NULL,?,NULL,'native','[]')",
        (message_id, "whatsapp", chat, native_id, sender, direction, at, reply),
    )


def _fixture(tmp_path):
    history, processing, speakups = (tmp_path / name for name in ("history.db", "processing.db", "speakups.db"))
    conn = _db(history)
    create(conn)
    pivot = int(datetime(2026, 1, 15, tzinfo=UTC).timestamp() * 1000)
    conn.execute(
        "INSERT INTO projector_state VALUES ('@runtime',0,0,'',4,?)", (json.dumps({"generation": 7}),)
    )
    chat, other = "group@g.us", "other@g.us"
    _message(conn, "before", "bot-before", chat, "out", pivot - 120_000, "self")
    _message(conn, "u-below-boundary", "user-below", chat, "in", pivot - 60_001, "user", "bot-before")
    _message(conn, "u-at-boundary", "user-at", chat, "in", pivot - 60_000, "user", "bot-before")
    _message(conn, "bot-follow", "bot-follow", chat, "out", pivot - 30_000, "self")
    _message(conn, "u-after-boundary", "user-after", chat, "in", pivot - 59_999, "user")
    _message(conn, "after", "bot-after", chat, "out", pivot + 120_000, "self")
    _message(conn, "u-after", "user-after-bot", chat, "in", pivot + 130_000, "user")
    _message(conn, "other-anchor", "other-bot", other, "out", pivot + 120_000, "self")
    for event_id, occurred, emoji in (("r1", pivot - 60_000, "👍"), ("r2", pivot - 59_999, "❤️")):
        conn.execute(
            "INSERT INTO message_events (event_id,kind,channel,chat_id,target_message_id,"
            "actor_identifier,actor_basis,occurred_ms,time_certainty,payload_json,provenance,"
            "source_refs) VALUES (?,'reaction','whatsapp',?,'before','user','native_identifier',"
            "?,'native',?,'native','[]')",
            (event_id, chat, occurred, json.dumps({"emoji": emoji})),
        )
    conn.commit()
    conn.close()

    conn = _db(processing)
    conn.executescript("""
      CREATE TABLE effects(effect_id TEXT, origin TEXT, payload_kind TEXT, state TEXT);
      CREATE TABLE transport_receipts(effect_id TEXT, channel TEXT, chat_id TEXT, provider_message_id TEXT);
    """)
    for eid, chat_id, provider in (("e1", chat, "bot-before"), ("e2", chat, "bot-after"),
                                   ("e3", other, "other-bot")):
        conn.execute("INSERT INTO effects VALUES (?, 'participation','text','sent')", (eid,))
        conn.execute("INSERT INTO transport_receipts VALUES (?, 'whatsapp', ?, ?)", (eid, chat_id, provider))
    conn.commit()
    conn.close()

    conn = _db(speakups)
    conn.execute("CREATE TABLE judge_attempts(channel TEXT,chat_id TEXT,created_at_ms INTEGER,outcome TEXT,detail_code TEXT)")
    conn.executemany("INSERT INTO judge_attempts VALUES ('whatsapp',?,?,?,?)", [
        (chat, pivot - 1, "react", None), (chat, pivot, "invalid_response", "invalid_response"),
        (other, pivot, "comment", None),
    ])
    conn.execute("INSERT INTO judge_attempts VALUES ('telegram',?,?,?,?)",
                 (chat, pivot, "private detail text", "private detail text"))
    conn.execute("INSERT INTO judge_attempts VALUES ('whatsapp','dm@s.whatsapp.net',?,?,?)",
                 (pivot, "comment", None))
    conn.execute("INSERT INTO judge_attempts VALUES ('whatsapp',?,?,?,?)",
                 (chat, pivot + 1, "private detail text", "private detail text"))
    conn.commit()
    conn.close()
    return history, processing, speakups, pivot, chat, other


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_outcomes_filter_windows_boundaries_and_exclude_bot_activity(tmp_path):
    history, processing, speakups, pivot, chat, _other = _fixture(tmp_path)
    switch = datetime.fromtimestamp(pivot / 1000, UTC)
    now = switch + timedelta(days=1)
    before = {path: _digest(path) for path in (history, processing, speakups)}
    result = evaluate(history_db=history, processing_db=processing, speakups_db=speakups,
                      switch=switch, days=1, lull_minutes=1, chats={chat}, now=now)
    assert result["totals"]["before"]["bot_messages"] == 1
    assert result["totals"]["before"]["with_further_messages"] == 1
    assert result["totals"]["before"]["with_reply"] == 1
    assert result["totals"]["before"]["with_reaction"] == 1
    assert result["totals"]["before"]["any_share"] == 1.0
    assert result["totals"]["after"]["bot_messages"] == 1
    assert result["totals"]["after"]["any_share"] == 1.0
    assert result["judge_mix"]["before"]["actions"] == {"react": 1}
    assert result["judge_mix"]["after"]["errors"] == {"invalid_response": 1}
    assert result["judge_mix"]["after"]["other"] == {"unclassified": 1}
    assert set(result["chats"]) == {chat}
    assert {path: _digest(path) for path in before} == before


def test_partial_window_censors_incomplete_lull_and_report_has_no_message_text(tmp_path):
    history, processing, speakups, pivot, chat, _other = _fixture(tmp_path)
    switch = datetime.fromtimestamp(pivot / 1000, UTC)
    result = evaluate(history_db=history, processing_db=processing, speakups_db=speakups,
                      switch=switch, days=1, lull_minutes=1, chats={chat},
                      now=switch + timedelta(minutes=2, seconds=30))
    assert result["partial"] is True
    assert result["totals"]["after"]["censored"] == 1
    assert result["totals"]["after"]["observed_bot_messages"] == 0
    assert result["totals"]["after"]["with_further_messages"] == 1
    assert result["totals"]["after"]["any_share"] == 1.0
    rendered = json.dumps(result) + render_markdown(result)
    assert "private fixture text" not in rendered
    assert "private detail text" not in rendered
    assert "after window is partial" in rendered.lower()


def test_requires_timezone_and_half_open_period_boundaries(tmp_path):
    history, processing, speakups, pivot, _chat, _other = _fixture(tmp_path)
    naive = datetime.fromtimestamp(pivot / 1000)
    try:
        evaluate(history_db=history, processing_db=processing, speakups_db=speakups,
                 switch=naive, days=1, lull_minutes=1, now=datetime.now(UTC))
    except ValueError as exc:
        assert "timezone" in str(exc)
    else:
        raise AssertionError("naive switch accepted")


def test_empty_windows_write_aggregate_only_private_report(tmp_path):
    history, processing, speakups, pivot, _chat, other = _fixture(tmp_path)
    switch = datetime.fromtimestamp(pivot / 1000, UTC)
    result = evaluate(history_db=history, processing_db=processing, speakups_db=speakups,
                      switch=switch, days=1, lull_minutes=1, chats={other},
                      now=switch + timedelta(days=1))
    assert result["chats"][other]["before"]["bot_messages"] == 0
    assert result["chats"][other]["before"]["censored"] == 0
    for metric in ("with_reply", "with_reaction", "with_further_messages", "any"):
        assert result["chats"][other]["before"][metric] == 0
        assert result["chats"][other]["before"][f"{metric}_share"] == 0.0
    assert result["chats"][other]["after"]["bot_messages"] == 1
    assert result["chats"][other]["after"]["censored"] == 0
    report = tmp_path / "report"
    write_outputs(report, result)
    assert (report / "summary.json").is_file()
    assert (report / "summary.md").is_file()
    text = (report / "summary.json").read_text(encoding="utf-8")
    assert "private fixture text" not in text
    assert "private detail text" not in text
    assert report.stat().st_mode & 0o777 == 0o700
    assert (report / "summary.json").stat().st_mode & 0o777 == 0o600


def test_cli_uses_camel_case_lull_and_writes_only_requested_output(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from yeoman_gateway.cli.eval_commands import eval_app
    from yeoman_gateway.evaluation import participation_outcomes
    from yeoman_shared.utils import helpers

    root = tmp_path / "runtime"
    data = root / "data"
    for path in (root / "config.json", data / "history" / "history.db",
                 data / "ops" / "processing.db", data / "ops" / "speakups.db"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    (root / "config.json").write_text(json.dumps({
        "consciousness": {"lullSilenceMinutes": 17},
        "processing": {"dbPath": "data/ops/processing.db"},
    }), encoding="utf-8")
    output = tmp_path / "chosen-report"
    captured = {}

    def fake_evaluate(**kwargs):
        captured.update(kwargs)
        zeroes = {"bot_messages": 0, "with_reply": 0, "with_reaction": 0,
                  "with_further_messages": 0, "any": 0, "with_reply_share": 0.0,
                  "with_reaction_share": 0.0, "with_further_messages_share": 0.0,
                  "any_share": 0.0}
        return {
            "switch": "2026-01-15T00:00:00+00:00", "partial": False, "chats": {},
            "totals": {"before": zeroes, "after": zeroes},
            "judge_mix": {p: {"actions": {}, "errors": {}} for p in ("before", "after")},
        }

    monkeypatch.setattr(helpers, "get_yeoman_home", lambda: root)
    monkeypatch.setattr(participation_outcomes, "evaluate", fake_evaluate)
    monkeypatch.setattr(participation_outcomes, "write_outputs", lambda directory, summary: captured.update(output_dir=directory))
    result = CliRunner().invoke(eval_app, [
        "participation-outcomes", "--switch", "2026-01-15T00:00:00Z",
        "--chat", "123@g.us", "--chat", "456@g.us", "--output-dir", str(output),
    ])
    assert result.exit_code == 0, result.output
    assert captured["lull_minutes"] == 17
    assert captured["chats"] == {"123@g.us", "456@g.us"}
    assert captured["history_db"] == data / "history" / "history.db"
    assert captured["processing_db"] == data / "ops" / "processing.db"
    assert captured["output_dir"] == output
    assert "Judge mix" in result.output


@pytest.mark.parametrize(("kind", "offset", "expected"), [
    ("message", 59_999, 1), ("message", 60_000, 1), ("message", 60_001, 0),
    ("reaction", 59_999, 1), ("reaction", 60_000, 1), ("reaction", 60_001, 0),
])
def test_lull_cutoff_and_self_activity(kind, offset, expected, tmp_path):
    folder = tmp_path / f"{kind}-{offset}"
    folder.mkdir()
    history, processing, speakups, pivot, chat, _other = _fixture(folder)
    conn = _db(history)
    conn.execute("DELETE FROM message_events WHERE chat_id=?", (chat,))
    conn.execute("DELETE FROM messages WHERE chat_id=? AND message_id!='before'", (chat,))
    _message(conn, "own-followup", "own-followup", chat, "out", pivot - 110_000, "self")
    if kind == "message":
        _message(conn, "response", "response", chat, "in", pivot - 120_000 + offset,
                 "participant", "bot-before")
    else:
        conn.execute(
            "INSERT INTO message_events (event_id,kind,channel,chat_id,target_message_id,"
            "actor_identifier,actor_basis,occurred_ms,time_certainty,payload_json,provenance,"
            "source_refs) VALUES ('self-reaction','reaction','whatsapp',?,'before','self',"
            "'native_identifier',?,'native','{\"emoji\":\"👍\"}','native','[]')",
            (chat, pivot - 110_000),
        )
        conn.execute(
            "INSERT INTO message_events (event_id,kind,channel,chat_id,target_message_id,"
            "actor_identifier,actor_basis,occurred_ms,time_certainty,payload_json,provenance,"
            "source_refs) VALUES ('response-reaction','reaction','whatsapp',?,'before','participant',"
            "'native_identifier',?,'native','{\"emoji\":\"👍\"}','native','[]')",
            (chat, pivot - 120_000 + offset),
        )
    conn.commit()
    conn.close()
    conn = _db(processing)
    conn.execute("DELETE FROM transport_receipts WHERE chat_id=? AND effect_id!='e1'", (chat,))
    conn.execute("DELETE FROM effects WHERE effect_id!='e1'")
    conn.commit()
    conn.close()

    switch = datetime.fromtimestamp(pivot / 1000, UTC)
    result = evaluate(history_db=history, processing_db=processing, speakups_db=speakups,
                      switch=switch, days=1, lull_minutes=1, chats={chat},
                      now=switch + timedelta(days=1))
    metric = "with_further_messages" if kind == "message" else "with_reaction"
    assert result["totals"]["before"][metric] == expected
    assert result["totals"]["before"]["any"] == expected
    assert result["totals"]["after"][metric] == 0
