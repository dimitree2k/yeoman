"""Tests for the content-free daily log digest."""
from __future__ import annotations

import gzip
import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest
from yeoman_overseer.executor.deterministic import DeterministicExecutor
from yeoman_overseer.executor.log_digest import build_log_digest, signature_of

NOW = datetime(2026, 10, 28, 6, 15, 0)


def _line(ts: str, level: str, where: str, msg: str) -> str:
    return f"2026-10-28 {ts}.123 | {level:<8} | {where} - {msg}\n"


def _make_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE decisions (stage TEXT, outcome TEXT, reason TEXT, created_ms INTEGER);
            CREATE TABLE effects (capability TEXT, state TEXT, payload_json TEXT, created_ms INTEGER);
            CREATE TABLE short_reply_decisions (
              channel TEXT, chat_id TEXT, message_id TEXT, status TEXT, emoji TEXT,
              created_ms INTEGER);
            """
        )
        recent = int(datetime(2026, 10, 28, 5, 0).timestamp() * 1000)
        conn.execute(
            "INSERT INTO decisions VALUES ('final','deny','participation_action_mismatch',?)",
            (recent,),
        )
        conn.execute("INSERT INTO effects VALUES ('send_media','blocked','{\"text\":\"secret\"}',?)", (recent,))
        conn.execute(
            "INSERT INTO short_reply_decisions VALUES ('whatsapp','chat@g.us','M1','react','😂',?)",
            (recent,),
        )


@pytest.fixture
def env(tmp_path: Path) -> dict[str, Path]:
    logs = tmp_path / "logs"
    logs.mkdir()
    db = tmp_path / "processing.db"
    _make_db(db)
    return {"logs": logs, "db": db, "out": tmp_path / "review"}


def _digest(env: dict[str, Path], now: datetime = NOW) -> dict:
    result = build_log_digest(
        log_dir=env["logs"], processing_db=env["db"], out_dir=env["out"], now=now
    )
    return json.loads(result.path.read_text())


def test_signature_drops_message_text_and_ids() -> None:
    sig = signature_of(
        "WARNING",
        "yeoman_gateway.media.vision",
        "_run",
        "vision image description failed model=mistralai/mistral-small-3 provider=openrouter "
        "caption=Hallo Welt chat=4917612345@s.whatsapp.net",
    )
    assert sig == (
        "WARNING yeoman_gateway.media.vision:_run vision image description failed "
        "model=mistralai/mistral-small-N provider=openrouter"
    )
    assert "Hallo" not in sig and "4917" not in sig


def test_digest_counts_signatures_tool_failures_and_db_outcomes(env: dict[str, Path]) -> None:
    (env["logs"] / "gateway.log").write_text(
        _line("05:00:00", "INFO", "yeoman_gateway.adapters.responder_llm:_run_chat_loop:2052",
              'Tool call: make_meme({"captions": ["Testnachricht aus Holland"]})')
        + _line("05:00:01", "WARNING", "yeoman_gateway.agent.tools.registry:execute:90",
                "tool_result_failed tool=make_meme kind=error_result")
        + _line("05:00:02", "WARNING", "yeoman_gateway.agent.tools.registry:execute:90",
                "tool_result_failed tool=make_meme kind=error_result")
        + _line("05:01:00", "ERROR", "yeoman_gateway.providers.litellm_provider:chat:120",
                "LLM provider request failed provider=openrouter text=Bitte hilf mir")
        + _line("05:02:00", "DEBUG", "yeoman_gateway.channels.whatsapp:_send_command:3185",
                "WhatsApp bridge command ok")
    )
    digest = _digest(env)
    raw = json.dumps(digest)
    assert "Holland" not in raw and "Bitte" not in raw and "secret" not in raw
    assert digest["tool_calls"] == {"make_meme": 1}
    by_kind = {(i["kind"], i["signature"]): i for i in digest["incidents"]}
    tool = by_kind[("tool_failure", "tool_failure make_meme error_result")]
    assert tool["count"] == 2 and tool["calls"] == 1 and tool["new"] is True
    assert any(
        i["kind"] == "log_signature" and i["level"] == "ERROR" and i["count"] == 1
        for i in digest["incidents"]
    )
    assert ("decision_denied", "decision_denied final participation_action_mismatch") in by_kind
    assert digest["processing"]["reply_sorter"] == {"react": 1}
    assert digest["processing"]["reply_sorter_reactions"][0]["message_id"] == "M1"


def test_checkpoint_skips_seen_lines_and_marks_recurring(env: dict[str, Path]) -> None:
    log = env["logs"] / "gateway.log"
    warn = ("yeoman_gateway.media.vision:_run:10", "vision OCR failed model=m provider=p")
    log.write_text(_line("05:00:00", "WARNING", *warn))
    first = _digest(env)
    assert first["incidents"][0]["new"] is True

    with log.open("a") as handle:
        handle.write(_line("06:20:00", "WARNING", *warn))
    second = _digest(env, now=datetime(2026, 10, 29, 6, 15))
    vision = [i for i in second["incidents"] if i["kind"] == "log_signature"]
    assert vision[0]["count"] == 1  # the 05:00 line was not counted again
    assert vision[0]["new"] is False and vision[0]["days_seen"] == 2


def test_reads_rotated_gz_after_rotation(env: dict[str, Path]) -> None:
    (env["out"]).mkdir()
    (env["out"] / "state.json").write_text(json.dumps({"last_ts": "2026-10-28 04:00:00.000000"}))
    rotated = env["logs"] / "gateway.2026-10-28_05-30-00_000000.log.gz"
    with gzip.open(rotated, "wt") as handle:
        handle.write(_line("05:00:00", "ERROR", "m:f:1", "rotated away"))
    os.utime(rotated, (NOW.timestamp(), NOW.timestamp()))
    (env["logs"] / "gateway.log").write_text(_line("05:40:00", "ERROR", "m:f:1", "rotated away"))
    digest = _digest(env)
    assert digest["lines_read"] == 2
    assert digest["incidents"][0]["count"] == 2


@pytest.mark.asyncio
async def test_log_digest_action_is_registered(env: dict[str, Path]) -> None:
    (env["logs"] / "gateway.log").write_text("")
    executor = DeterministicExecutor(comms=None)  # type: ignore[arg-type]
    result = await executor.execute(
        "log_digest", target=str(env["logs"]), processing_db=str(env["db"]), out_dir=str(env["out"])
    )
    assert result.success, result.detail
    assert "incidents" in result.detail
