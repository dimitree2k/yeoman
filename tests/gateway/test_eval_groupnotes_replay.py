"""Offline replay of the weekly group notes (Spec B section 8, without live shadow).

Synthetic history, a synthetic speakup ledger, an empty synthetic Knowledge copy and fake
model clients only. Nothing here reads a runtime path or talks to a provider.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
import sqlite3
import time
from datetime import UTC
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner
from yeoman_gateway.cli.commands import app
from yeoman_gateway.consciousness.log import SpeakupLog
from yeoman_gateway.evaluation.harness import StaticClient
from yeoman_gateway.evaluation.participation_context_ab import (
    FrozenHistory,
    UsageCapture,
    captured_usage,
    load_opportunities,
)
from yeoman_gateway.evaluation.participation_groupnotes_replay import (
    NOTES_FILE,
    MemoryDigestSink,
    ReplayNotes,
    note_week,
    replay,
    stored_note_reader,
    summarize,
    write_outputs,
)
from yeoman_gateway.history.group_signals import iso_week
from yeoman_gateway.knowledge.api import open_knowledge_store
from yeoman_gateway.knowledge.authority import FakePolicyAuthority
from yeoman_gateway.knowledge.runtime import RuntimeKnowledgeSources
from yeoman_gateway.processing.group_digest import (
    DigestBounds,
    GroupDigestRunner,
    validate_digest,
)
from yeoman_gateway.processing.model_route import RouteReply
from yeoman_gateway.processing.participation import ParticipationJudge
from yeoman_gateway.processing.participation_context import (
    ParticipationContextBounds,
    ParticipationContextBuilder,
)

from tests.gateway.history_test_helpers import ProjectedHistory, prepare_empty_history_knowledge

CHAT = "120363000000000091@g.us"
QUIET = "120363000000000092@g.us"
DIRECT = "4915100000077@s.whatsapp.net"
MIN = 60_000
HOUR = 60 * MIN
W39 = iso_week("2026-W39", tz=UTC)
W40 = iso_week("2026-W40", tz=UTC)
W41 = iso_week("2026-W41", tz=UTC)
DIGEST_BOUNDS = DigestBounds(lull_ms=25 * MIN, burst_n=4, burst_ms=15 * MIN)
SECRET = "synthetic-private-wording"
LEAK = "synthetic-after-week-wording"
LATE = "synthetic-late-reacted-wording"
_WEEK_IN_PROMPT = re.compile(r'"week":"(\d{4}-W\d{2})"')


def _phone(index: int) -> str:
    return f"49151000{index:04d}"


def _busy_week(history: ProjectedHistory, chat: str, week, *, conversations: int = 6) -> None:
    """Enough signal: two-hour-apart conversations from Tuesday 09:00, reacted 1-3 times."""
    base = (week.start_ms + 33 * HOUR) // 1000
    for conversation in range(conversations):
        for line in range(5):
            mid = f"{chat[-4:]}-{week.label}-c{conversation}-m{line}"
            sender = f"{_phone(line % 3 + 1)}@s.whatsapp.net"
            history.add_message(
                chat_id=chat, message_id=mid, text=f"{SECRET} topic {conversation} line {line}",
                timestamp=base + conversation * 7200 + line * 60, sender_id=sender,
                participant=sender,
            )
            for reactor in range(line % 3 + 1):
                history.append(
                    "reaction", chat_id=chat, timestamp=base + conversation * 7200 + line * 60 + 30,
                    emoji="😂", removed=False,
                    senderId=f"{_phone(900 + reactor)}@s.whatsapp.net", targetMessageId=mid,
                )


def _history(tmp_path: Path) -> ProjectedHistory:
    history = ProjectedHistory(tmp_path / "history")
    _busy_week(history, CHAT, W40)
    # A W40 message whose only reactions arrive in W41: never part of the W40 note.
    late_ts = (W40.start_ms + 4 * 24 * HOUR) // 1000
    history.add_message(chat_id=CHAT, message_id="late-w40", text=LATE, timestamp=late_ts,
                        sender_id=f"{_phone(5)}@s.whatsapp.net",
                        participant=f"{_phone(5)}@s.whatsapp.net")
    for reactor in range(12):
        history.append("reaction", chat_id=CHAT, timestamp=(W41.start_ms + HOUR) // 1000 + reactor,
                       emoji="🔥", removed=False,
                       senderId=f"{_phone(700 + reactor)}@s.whatsapp.net",
                       targetMessageId="late-w40")
    # W40 opportunity source, and W41 messages written after W40 ended.
    history.add_message(chat_id=CHAT, message_id="w40-src", text=f"{SECRET} w40",
                        timestamp=(W40.start_ms + 3 * 24 * HOUR) // 1000,
                        sender_id=f"{_phone(1)}@s.whatsapp.net",
                        participant=f"{_phone(1)}@s.whatsapp.net")
    for index in range(2):
        history.add_message(
            chat_id=CHAT, message_id=f"w41-src-{index}", text=f"{LEAK} {index}",
            timestamp=(W41.start_ms + 2 * 24 * HOUR + index * 5 * MIN) // 1000,
            sender_id=f"{_phone(2)}@s.whatsapp.net", participant=f"{_phone(2)}@s.whatsapp.net",
        )
    # A quiet group: a few messages, two reactions.
    for index in range(3):
        history.add_message(
            chat_id=QUIET, message_id=f"quiet-{index}", text=f"{SECRET} quiet {index}",
            timestamp=(W40.start_ms + 2 * 24 * HOUR + index * MIN) // 1000,
            sender_id=f"{_phone(3)}@s.whatsapp.net", participant=f"{_phone(3)}@s.whatsapp.net",
        )
    history.add_message(chat_id=QUIET, message_id="quiet-src", text=f"{SECRET} quiet src",
                        timestamp=(W41.start_ms + 2 * 24 * HOUR) // 1000,
                        sender_id=f"{_phone(3)}@s.whatsapp.net",
                        participant=f"{_phone(3)}@s.whatsapp.net")
    history.snapshot().close()
    return history


OPPORTUNITIES = (
    # (id, chat, at_ms, sources)
    ("opp-w40", CHAT, W40.start_ms + 3 * 24 * HOUR + MIN, ["w40-src"]),
    ("opp-w41-a", CHAT, W41.start_ms + 2 * 24 * HOUR + MIN, ["w41-src-0"]),
    ("opp-w41-b", CHAT, W41.start_ms + 2 * 24 * HOUR + 6 * MIN, ["w41-src-1"]),
    ("opp-quiet", QUIET, W41.start_ms + 2 * 24 * HOUR + MIN, ["quiet-src"]),
    ("opp-direct", DIRECT, W41.start_ms + 2 * 24 * HOUR + MIN, ["direct-1"]),
)


def _speakups(path: Path) -> Path:
    SpeakupLog(path).close()
    with sqlite3.connect(path) as db:
        for opportunity, chat, at_ms, sources in OPPORTUNITIES:
            db.execute(
                "INSERT INTO judge_attempts (attempt_id, opportunity_id, evaluation_index, channel,"
                " chat_id, created_at_ms, hourly_limit, outcome)"
                " VALUES (?,?,0,'whatsapp',?,?,12,'silence')",
                (f"{opportunity}:0", opportunity, chat, at_ms),
            )
            db.execute(
                "INSERT INTO opportunity_dispositions (opportunity_id, channel, chat_id,"
                " observed_revision, activation_epoch, lane, trigger, source_ids_json,"
                " disposition, created_at_ms, updated_at_ms)"
                " VALUES (?, 'whatsapp', ?, 1, 1, 'production', 'inbound', ?, 'judged', ?, ?)",
                (opportunity, chat, json.dumps(sources), at_ms, at_ms),
            )
    return path


def _all_opportunities(path: Path):
    return load_opportunities(path, since_ms=W39.start_ms, until_ms=W41.end_ms)


class DigestClient:
    """Answers a valid note naming the week it was built from; counts calls."""

    model = "fake/digest"
    structured_output = "json_schema_strict"

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def chat_with_usage(self, messages, **kwargs) -> RouteReply:  # noqa: ANN001, ANN003
        prompt = messages[-1]["content"]
        self.calls.append(prompt)
        week = _WEEK_IN_PROMPT.search(prompt).group(1)
        note = {
            "goes_down_well": [f"Dry jokes, built from {week}"],
            "engages": ["Market debates, friendly"],
            "arvid_effect": ["unclear"],
            "confidence": "medium",
        }
        return RouteReply(content=json.dumps(note), model=self.model, usage={"prompt_tokens": 9})


class JudgeClient:
    """Reacts when the prompt carries a group note, stays silent otherwise."""

    route_key = "eval.fake"
    structured_output = "json_schema_strict"
    model = "fake"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def chat_with_usage(self, messages, **kwargs) -> RouteReply:  # noqa: ANN001, ANN003
        prompt = messages[-1]["content"]
        self.prompts.append(prompt)
        content = (
            '{"action":"react","intent":"initiate","emoji":"👍","reason":"x"}'
            if "[Group notes" in prompt
            else '{"action":"silence","intent":"initiate","reason":"x"}'
        )
        return RouteReply(content=content, usage={"prompt_tokens": len(prompt) // 4},
                          model="fake", finish_reason="stop")


class NoCalls:
    """Any model call fails the test."""

    route_key = model = "none"
    structured_output = "json_schema_strict"

    async def chat_with_usage(self, *args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("no model call expected")


def _runner_for(history: Any, sinks: list[MemoryDigestSink]):
    def factory():
        raise AssertionError("the replay passes its digest client explicitly")

    def runner_for(week):
        sink = MemoryDigestSink()
        sinks.append(sink)
        return GroupDigestRunner(knowledge=sink, history=history, bounds=DIGEST_BOUNDS,
                                 client_factory=factory, tz=UTC)

    return runner_for


def _bounds() -> ParticipationContextBounds:
    return ParticipationContextBounds(
        window_minutes=120, max_messages=40, context_mode="window",
        lull_minutes=25, burst_messages=4, burst_minutes=15, conversation_min_messages=0,
    )


async def _replay(tmp_path: Path, *, judged: bool = True, generate: bool = True,
                  use_stored: bool = False, knowledge_db: Path | None = None,
                  digest: Any = None, judge_client: Any = None, concurrency: int = 4,
                  before_run: Any = None, judge_repeats: int = 1):
    history = _history(tmp_path)
    history.close()  # projected; the replay opens its own read-only snapshots
    opportunities = _all_opportunities(_speakups(tmp_path / "speakups.db"))
    if before_run is not None:
        before_run()
    digest = digest if digest is not None else (DigestClient() if generate else None)
    judge_client = judge_client or JudgeClient()
    frozen = FrozenHistory(history.path)
    sinks: list[MemoryDigestSink] = []
    notes = ReplayNotes(
        runner_for=_runner_for(frozen, sinks), client=digest, generate=generate,
        stored=stored_note_reader(knowledge_db), use_stored=use_stored,
    )
    try:
        records = await replay(
            opportunities, history=frozen,
            builder=ParticipationContextBuilder(policy=None, source_authorizer=lambda row: True),
            bounds=_bounds(),
            estimator=ParticipationJudge(client=StaticClient("{}"), allowed_emojis=("👍",)),
            notes=notes,
            judge=ParticipationJudge(client=UsageCapture(judge_client), allowed_emojis=("👍",))
            if judged else None,
            usage=captured_usage, concurrency=concurrency, judge_repeats=judge_repeats, tz=UTC,
        )
    finally:
        frozen.close()
    return records, notes, digest, judge_client, sinks


def _by_id(records: list[dict]) -> dict[str, dict]:
    return {record["opportunity_id"]: record for record in records}


def _store_digest(knowledge_db: Path, chat: str, week) -> str:
    store = open_knowledge_store(
        knowledge_db, workspace_id="replay-test", source_authority=RuntimeKnowledgeSources(),
        policy_authority=FakePolicyAuthority(),
    )
    try:
        content, rendered = validate_digest(json.dumps({
            "goes_down_well": ["Stored note, short charts"], "engages": ["Rates, heated"],
            "arvid_effect": ["unclear"], "confidence": "low",
        }))
        view = store.store_group_digest(
            chat_id=chat, week=week.label, content=content, rendered=rendered,
            model_version="m", prompt_version="p", confidence="low", signals={},
            window_start_ms=week.start_ms, window_end_ms=week.end_ms,
            sources=(("m-1", "approval"),),
        )
    finally:
        store.close()
    return str(view.digest_id)


def _fingerprints(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*")) if path.is_file()
    }


# ── leakage ─────────────────────────────────────────────────────────────────


def test_note_week_is_the_week_before_the_opportunity() -> None:
    assert note_week(W41.start_ms, tz=UTC).label == "2026-W40"
    assert note_week(W41.end_ms - 1, tz=UTC).label == "2026-W40"
    assert note_week(W40.end_ms - 1, tz=UTC).label == "2026-W39"


@pytest.mark.asyncio
async def test_a_weeks_note_sees_only_that_week_and_serves_only_the_next(tmp_path: Path) -> None:
    records, notes, digest, judge, _sinks = await _replay(tmp_path)
    by_id = _by_id(records)

    # One W40 note for CHAT; W39 and the quiet group never reach the model.
    assert len(digest.calls) == 1
    [prompt] = digest.calls
    assert '"week":"2026-W40"' in prompt
    assert LEAK not in prompt  # messages after the week's end
    payload = json.loads(prompt.split("\n", 1)[1])
    # Reacted only after the week's end: no approval signal, not counted.
    assert all(LATE not in item["excerpt"] for item in payload["top_reacted_messages"])
    assert payload["aggregates"]["reactions"] == 54
    assert "🔥" not in json.dumps(payload["aggregates"], ensure_ascii=False)

    # Opportunities in W41 see the W40 note; the one in W40 never does.
    for opportunity_id in ("opp-w41-a", "opp-w41-b"):
        assert by_id[opportunity_id]["note"]["week"] == "2026-W40"
        assert by_id[opportunity_id]["note"]["source"] == "generated"
        assert by_id[opportunity_id]["B"]["group_notes_rendered"] is True
    assert by_id["opp-w40"]["note"]["week"] == "2026-W39"
    assert by_id["opp-w40"]["skipped"] == "insufficient_signal"

    with_note = [p for p in judge.prompts if "[Group notes" in p]
    assert len(with_note) == 2 and len(judge.prompts) == 4
    assert all("[Group notes, week 2026-W40" in p and "built from 2026-W40" in p
               for p in with_note)
    assert {note.week for note in notes.notes() if note.status == "ready"} == {"2026-W40"}


# ── no writes, caching, minimum signal ──────────────────────────────────────


@pytest.mark.asyncio
async def test_replay_writes_nothing_to_knowledge_or_history(tmp_path: Path) -> None:
    knowledge_db = prepare_empty_history_knowledge(tmp_path)
    _store_digest(knowledge_db, QUIET, W39)  # digest tables exist; one unrelated row
    before: dict[str, str] = {}
    records, notes, _digest, _judge, sinks = await _replay(
        tmp_path / "run", knowledge_db=knowledge_db,
        before_run=lambda: before.update(_fingerprints(tmp_path)),
    )
    assert _fingerprints(tmp_path) == before  # Knowledge, history, ledger: all unchanged
    with sqlite3.connect(knowledge_db) as db:
        assert db.execute("SELECT COUNT(*) FROM knowledge_group_digests").fetchone()[0] == 1
    # The runner's "store" went to the in-memory sink only.
    stored = [digest for sink in sinks for digest in sink.stored]
    assert [(item.chat_id, item.week) for item in stored] == [(CHAT, "2026-W40")]
    assert stored[0].digest_id == f"replay:{CHAT}:2026-W40"
    assert notes.digest_calls == 1 and any("B" in record for record in records)


@pytest.mark.asyncio
async def test_one_digest_call_per_chat_week_even_in_parallel(tmp_path: Path) -> None:
    class SlowDigest(DigestClient):
        async def chat_with_usage(self, messages, **kwargs) -> RouteReply:  # noqa: ANN001, ANN003
            await asyncio.sleep(0.02)
            return await super().chat_with_usage(messages, **kwargs)

    records, notes, digest, _judge, _sinks = await _replay(tmp_path, digest=SlowDigest(),
                                                           concurrency=8)
    assert len(digest.calls) == 1
    summary = summarize(records, notes.notes(), judged=True, context_mode="window")
    assert summary["notes"]["digest_calls"] == 1
    assert summary["notes"]["chat_weeks"] == 3  # CHAT W39, CHAT W40, QUIET W40


@pytest.mark.asyncio
async def test_below_minimum_signal_is_skipped_and_counted(tmp_path: Path) -> None:
    records, notes, _digest, _judge, _sinks = await _replay(tmp_path)
    by_id = _by_id(records)
    assert by_id["opp-quiet"]["skipped"] == "insufficient_signal"
    assert by_id["opp-quiet"]["note"]["week"] == "2026-W40"
    assert by_id["opp-direct"]["skipped"] == "not_group_chat"
    assert "A" not in by_id["opp-quiet"] and "A" not in by_id["opp-direct"]
    summary = summarize(records, notes.notes(), judged=True, context_mode="window")
    assert summary["skipped"] == {"insufficient_signal": 2, "not_group_chat": 1}
    assert summary["notes"]["by_status"] == {"insufficient_signal": 2, "ready": 1}
    assert summary["agreement_rate"] == 0.0
    assert summary["decision_mix"] == {"silence->react": 2}
    assert summary["actions"] == {"A": {"silence": 2}, "B": {"react": 2}}
    assert summary["judge_calls"] == 4
    assert summary["disagreement_ids"] == ["opp-w41-a", "opp-w41-b"]
    assert summary["estimated_token_delta_mean"] > 0
    assert summary["note_rendered_share"] == 1.0


# ── --no-judge and --use-stored ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_judge_makes_zero_model_calls(tmp_path: Path) -> None:
    records, notes, _digest, judge, _sinks = await _replay(
        tmp_path, judged=False, generate=False, judge_client=NoCalls(),
    )
    by_id = _by_id(records)
    assert judge.__class__ is NoCalls and notes.digest_calls == 0
    assert by_id["opp-w41-a"]["skipped"] == "note_unavailable"  # signal met, nothing stored
    assert by_id["opp-quiet"]["skipped"] == "insufficient_signal"
    summary = summarize(records, notes.notes(), judged=False, context_mode="window")
    assert summary["notes"]["digest_calls"] == 0
    assert summary["notes"]["digest_calls_needed"] == 1
    assert summary["judge_calls_needed"] == 4


@pytest.mark.asyncio
async def test_no_judge_uses_a_stored_digest_for_token_estimates(tmp_path: Path) -> None:
    knowledge_db = prepare_empty_history_knowledge(tmp_path)
    digest_id = _store_digest(knowledge_db, CHAT, W40)
    records, notes, _digest, _judge, _sinks = await _replay(
        tmp_path / "run", judged=False, generate=False, knowledge_db=knowledge_db,
    )
    record = _by_id(records)["opp-w41-a"]
    assert record["note"] == {"chat_id": CHAT, "week": "2026-W40", "status": "ready",
                              "reason": "", "source": "stored", "digest_id": digest_id}
    assert record["B"]["estimated_prompt_tokens"] > record["A"]["estimated_prompt_tokens"]
    assert notes.digest_calls == 0


@pytest.mark.asyncio
async def test_use_stored_reuses_the_digest_and_generates_only_without_one(
    tmp_path: Path,
) -> None:
    knowledge_db = prepare_empty_history_knowledge(tmp_path)
    _store_digest(knowledge_db, CHAT, W40)
    records, notes, digest, judge, _sinks = await _replay(
        tmp_path / "run", use_stored=True, knowledge_db=knowledge_db,
    )
    assert digest.calls == []
    assert _by_id(records)["opp-w41-a"]["note"]["source"] == "stored"
    assert any("Stored note, short charts" in p for p in judge.prompts)
    # Default: generate even when a stored digest exists.
    records, _notes, digest, _judge, _sinks = await _replay(
        tmp_path / "again", knowledge_db=knowledge_db,
    )
    assert len(digest.calls) == 1
    assert _by_id(records)["opp-w41-a"]["note"]["source"] == "generated"


@pytest.mark.asyncio
async def test_judge_repeats_ask_the_b_judge_several_times(tmp_path: Path) -> None:
    records, notes, _digest, judge, _sinks = await _replay(tmp_path, judge_repeats=3)
    record = _by_id(records)["opp-w41-a"]
    assert record["B"]["judge_repeats"] == 3 and record["B"]["unanimous"] is True
    assert record["B"]["action"] == "react"
    assert len(judge.prompts) == 2 * (1 + 3)
    summary = summarize(records, notes.notes(), judged=True, context_mode="window")
    assert summary["judge_calls"] == 8 and summary["b_unanimous_share"] == 1.0


# ── outputs ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_outputs_hold_no_message_text(tmp_path: Path) -> None:
    records, notes, _digest, _judge, _sinks = await _replay(tmp_path)
    summary = summarize(records, notes.notes(), judged=True, context_mode="window")
    out = tmp_path / "report"
    out.mkdir()
    write_outputs(out, records, summary, "# report\n", notes.notes())
    for path in out.iterdir():
        text = path.read_text(encoding="utf-8")
        for secret in (SECRET, LEAK, LATE):
            assert secret not in text, path.name
    for name in ("records.jsonl", "summary.json"):
        assert "built from" not in (out / name).read_text()  # the note stays in notes.jsonl
    notes_file = out / NOTES_FILE
    assert notes_file.stat().st_mode & 0o777 == 0o600
    ready = [json.loads(line) for line in notes_file.read_text().splitlines()
             if json.loads(line)["status"] == "ready"]
    assert [entry["week"] for entry in ready] == ["2026-W40"]


# ── CLI ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def utc_local(monkeypatch: pytest.MonkeyPatch):
    """The CLI uses local time for weeks; pin it, and restore it afterwards."""
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def _cli_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, knowledge_db=None) -> Path:
    import yeoman_gateway.evaluation.participation_context_ab as ab
    import yeoman_gateway.evaluation.participation_groupnotes_replay as replay_module

    home = tmp_path / "home"
    monkeypatch.setenv("YEOMAN_HOME", str(home))
    history = _history(tmp_path)
    (home / "data" / "history").mkdir(parents=True)
    shutil.copy(history.path, home / "data" / "history" / "history.db")
    history.close()
    (home / "data" / "ops").mkdir(parents=True)
    _speakups(home / "data" / "ops" / "speakups.db")
    original = ab.load_opportunities
    monkeypatch.setattr(
        ab, "load_opportunities",
        lambda path, *, since_ms, until_ms, chat_ids=None: original(
            path, since_ms=W39.start_ms, until_ms=W41.end_ms, chat_ids=chat_ids),
    )
    reader = replay_module.stored_note_reader
    monkeypatch.setattr(replay_module, "stored_note_reader", lambda path: reader(knowledge_db))
    return home


def _report(home: Path) -> Path:
    return sorted((home / "var" / "eval" / "participation-groupnotes-replay").glob("*-replay"))[-1]


@pytest.mark.usefixtures("utc_local")
def test_cli_no_judge_makes_no_model_calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import yeoman_gateway.evaluation.judge_format as judge_format
    import yeoman_gateway.processing.model_route as model_route

    home = _cli_home(tmp_path, monkeypatch)

    def forbidden(*args, **kwargs):
        raise AssertionError("--no-judge must not build a model client")

    monkeypatch.setattr(judge_format, "build_eval_client", forbidden)
    monkeypatch.setattr(model_route, "RouteClient", forbidden)
    result = CliRunner().invoke(app, ["eval", "participation-groupnotes", "--no-judge"])
    assert result.exit_code == 0, result.output
    assert "model calls" not in result.output
    summary = json.loads((_report(home) / "summary.json").read_text())
    assert summary["judged"] is False and summary["notes"]["digest_calls"] == 0
    assert summary["skipped"]["note_unavailable"] == 2


@pytest.mark.usefixtures("utc_local")
def test_cli_replay_then_review_labels(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import yeoman_gateway.evaluation.judge_format as judge_format
    import yeoman_gateway.processing.model_route as model_route

    home = _cli_home(tmp_path, monkeypatch)
    judge, digest = JudgeClient(), DigestClient()
    monkeypatch.setattr(judge_format, "build_eval_client", lambda config, **kwargs: judge)
    monkeypatch.setattr(model_route, "RouteClient", lambda **kwargs: digest)
    result = CliRunner().invoke(app, ["eval", "participation-groupnotes", "--chat", CHAT,
                                      "--concurrency", "2"])
    assert result.exit_code == 0, result.output
    assert "digest_calls=1" in result.output and "agreement=0.0" in result.output
    assert len(digest.calls) == 1 and len(judge.prompts) == 4
    report = _report(home)
    summary = json.loads((report / "summary.json").read_text())
    assert summary["opportunities"] == 3 and summary["context_mode"] == "window"

    command = ["eval", "participation-groupnotes-review", str(report)]
    first = CliRunner().invoke(app, command, input="b\nq\n")
    assert first.exit_code == 0, first.output
    assert "=== 1/2 opp-w41-a" in first.output
    assert LEAK in first.output  # the owner sees the messages in the terminal only
    assert "[Group notes, week 2026-W40" in first.output
    assert "A decision: silence" in first.output and "B decision: react" in first.output
    assert "labelled=1 a_better=0 b_better=1 same=0" in first.output
    labels = (report / "labels.jsonl").read_text()
    assert LEAK not in labels and SECRET not in labels

    again = CliRunner().invoke(app, [
        "eval", "participation-groupnotes", "--from-report", str(report), "--chat", CHAT,
    ])
    assert again.exit_code == 0, again.output
    assert len(digest.calls) == 1  # the source report's notes are reused
    rerun = json.loads((_report(home) / "summary.json").read_text())
    assert rerun["notes"]["by_source"] == {"report": 1}
    assert rerun["label_check"]["checked"] == 1
