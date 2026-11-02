"""As-of prompt builders, label simulation, cost guard and report shape."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from yeoman_gateway.adapters.responder_llm import (
    _PARTICIPATION_DRAFT_NOTE,
    _PARTICIPATION_DRAFT_RULES,
    LLMResponder,
    _render_participation_transcript,
    build_participation_draft_prompt,
)
from yeoman_gateway.agent.context import ContextBuilder
from yeoman_gateway.consciousness.log import SpeakupLog
from yeoman_gateway.evaluation.participation_context_ab import RecordedOpportunity
from yeoman_gateway.evaluation.reply_state import ReplyStateItem
from yeoman_gateway.evaluation.reply_state_run import (
    GRADER_ROUTE,
    REPLY_ROUTE,
    WRITER_ROUTE,
    ReplyRecord,
    ReplyStateRunner,
    RunConfig,
    WriterRecord,
    arm_messages,
    load_reply_records,
    load_writer_records,
)
from yeoman_gateway.history.queries import HistoryQueries
from yeoman_gateway.processing.model_route import RouteReply
from yeoman_gateway.processing.participation_context import ParticipationContextBounds
from yeoman_gateway.processing.store import ProcessingStore

from tests.gateway.history_test_helpers import ProjectedHistory

CHAT = "120000000000000042@g.us"
T0 = 1_790_000_000_000
MINUTE = 60_000
SECRET_AFTER = "synthetic wording that only exists after the item"
LABELS = json.dumps(
    {
        "topic": "synthetic topic",
        "mood": "relaxed",
        "open_questions": [{"text": "who brings drinks?", "asked_by": "p1", "source": "m1"}],
        "plans": [],
    }
)
GRADE = json.dumps(
    {
        "preference": "1",
        "sides": {
            "1": {
                "accurate": True,
                "repeats_arvid": False,
                "picks_up_open": True,
                "contradicts": False,
                "tone": 4,
            },
            "2": {
                "accurate": True,
                "repeats_arvid": False,
                "picks_up_open": False,
                "contradicts": False,
                "tone": 3,
            },
        },
    }
)


class RecordingClient:
    def __init__(self, *, text: str = "candidate", route_key: str = "fake") -> None:
        self.text = text
        self.route_key = route_key
        self.model = f"{route_key}-model"
        self.structured_output = "json_object"
        self.calls: list[list[dict[str, str]]] = []
        self.temperatures: list[float | None] = []

    async def chat_with_usage(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int,
        response_format: dict[str, Any] | None = None,
        timeout_ms: int | None = None,
        temperature: float | None = None,
    ) -> RouteReply:
        self.calls.append(messages)
        self.temperatures.append(temperature)
        return RouteReply(
            content=self.text,
            usage={"prompt_tokens": 10, "completion_tokens": 5},
            model=self.model,
            latency_ms=2,
            finish_reason="stop",
        )


def _history(tmp_path: Path, *, count: int = 35, marker_after: bool = False) -> ProjectedHistory:
    history = ProjectedHistory(tmp_path / "history")
    history.add_group(
        chat_id=CHAT,
        participants=[f"1000{index}@s.whatsapp.net" for index in range(3)],
        timestamp=(T0 - MINUTE) / 1000,
    )
    for index in range(count):
        history.add_message(
            chat_id=CHAT,
            message_id=f"m{index}",
            text=f"message {index}",
            timestamp=(T0 + index * 1000) / 1000,
            sender_id=f"1000{index % 3}@s.whatsapp.net",
            participant=f"1000{index % 3}@s.whatsapp.net",
            sender_name=f"p{index % 3}",
        )
    history.add_message(
        chat_id=CHAT,
        message_id="item",
        text="the item message",
        timestamp=(T0 + count * 1000) / 1000,
        sender_id="10000@s.whatsapp.net",
        participant="10000@s.whatsapp.net",
        sender_name="anna",
    )
    if marker_after:
        history.add_message(
            chat_id=CHAT,
            message_id="later",
            text=SECRET_AFTER,
            timestamp=(T0 + (count + 1) * 1000) / 1000,
            sender_id="10001@s.whatsapp.net",
            participant="10001@s.whatsapp.net",
            sender_name="ben",
        )
    history.snapshot().close()
    return history


def _record(history: ProjectedHistory, *, at_ms: int) -> ReplyRecord:
    return ReplyRecord(
        item=ReplyStateItem(
            item_id="reply:item",
            kind="reply",
            chat_id=CHAT,
            at_ms=at_ms,
            stratum="addressed-answered",
            hard_case=True,
        ),
        history_id="item",
        native_id="item",
        text="the item message",
    )


def _runner(
    tmp_path: Path,
    history: ProjectedHistory,
    *,
    labels: RecordingClient | None = None,
    grader: RecordingClient | None = None,
    reply: RecordingClient | None = None,
) -> ReplyStateRunner:
    from yeoman_shared.config.schema import Config

    config = Config()
    config.processing.group_state.labels.refresh_after_messages = 30
    config.processing.group_state.labels.min_messages = 1
    return ReplyStateRunner(
        config=config,
        history=history,
        context_builder=ContextBuilder(tmp_path / "workspace"),
        clients={
            REPLY_ROUTE: reply or RecordingClient(text="reply candidate", route_key=REPLY_ROUTE),
            WRITER_ROUTE: RecordingClient(text="writer candidate", route_key=WRITER_ROUTE),
            config.processing.group_state.labels.route: labels
            or RecordingClient(text=LABELS, route_key="labels"),
            GRADER_ROUTE: grader or RecordingClient(text=GRADE, route_key=GRADER_ROUTE),
        },
        persona_for=lambda channel, chat_id: "You are Arvid.",
        reply_records=[_record(history, at_ms=T0 + 35_000)],
        writer_records=[],
    )


@pytest.mark.asyncio
async def test_no_prompt_contains_message_after_item(tmp_path: Path) -> None:
    history = _history(tmp_path, marker_after=True)
    labels = RecordingClient(text=LABELS, route_key="labels")
    grader = RecordingClient(text=GRADE, route_key=GRADER_ROUTE)
    reply = RecordingClient(text="reply candidate", route_key=REPLY_ROUTE)
    runner = _runner(tmp_path, history, labels=labels, grader=grader, reply=reply)
    try:
        outcome = await runner.run(
            RunConfig(replies=1, writer=0, seed=7, yes=True),
            report_dir=tmp_path / "run",
        )
    finally:
        history.close()

    assert outcome.summary is not None
    prompts = [message for call in reply.calls for message in call]
    prompts += [message for call in labels.calls for message in call]
    prompts += [message for call in grader.calls for message in call]
    joined = "\n".join(str(message.get("content") or "") for message in prompts)
    assert SECRET_AFTER not in joined
    assert "the item message" in joined


@pytest.mark.asyncio
async def test_arms_differ_only_in_declared_block(tmp_path: Path) -> None:
    history = _history(tmp_path, count=3)
    reply = RecordingClient(text="reply candidate", route_key=REPLY_ROUTE)
    runner = _runner(tmp_path, history, reply=reply)
    try:
        await runner.run(
            RunConfig(replies=1, writer=0, seed=7, yes=True),
            report_dir=tmp_path / "run",
        )
    finally:
        history.close()

    # A, A2, S, P in order.
    a, a2, s, p = reply.calls[:4]
    assert a2 == a
    assert s[0] == a[0] and p[0] == a[0]
    assert len(s) == len(a) and len(p) == len(a)
    assert s[:-1] == a[:-1] and p[:-1] == a[:-1]
    assert s[-1]["content"].startswith(a[-1]["content"] + "\n\n[Group State]")
    assert p[-1]["content"].startswith(a[-1]["content"] + "\n\n[Group State]")
    assert s[-1]["content"] != a[-1]["content"]
    assert p[-1]["content"] != a[-1]["content"]


def test_writer_prompt_extraction_byte_identical_live(monkeypatch: pytest.MonkeyPatch) -> None:
    context: dict[str, object] = {
        "target_message_id": "cur",
        "messages": [
            {"event_id": "o1", "sender": "anna", "text": "optional"},
            {"event_id": "cur", "sender": "ben", "text": "current"},
        ],
        "anchors": [],
    }
    transcript = _render_participation_transcript(context)
    expected = (
        f"{_PARTICIPATION_DRAFT_RULES}\n\n{transcript}\n\n"
        f"{_PARTICIPATION_DRAFT_NOTE.format(purpose='why')}"
    )
    assert build_participation_draft_prompt(context, "why") == expected

    sentinel = "synthetic extracted writer prompt"
    monkeypatch.setattr(
        "yeoman_gateway.adapters.responder_llm.build_participation_draft_prompt",
        lambda context, purpose: sentinel,
    )
    captured: dict[str, str] = {}

    async def fake_generate(**kwargs: Any) -> str:
        captured["content"] = str(kwargs["content"])
        return "draft"

    async def fake_apply(text: str, metadata: dict[str, object], **kwargs: Any) -> str:
        return text

    responder = object.__new__(LLMResponder)
    responder.model = "fake"
    responder._route_for_event = lambda event: ("whatsapp", CHAT)  # type: ignore[method-assign]
    responder._metadata_for_event = lambda event: {}  # type: ignore[method-assign]
    responder._generate = fake_generate  # type: ignore[method-assign]
    responder._apply_draft_budget = fake_apply  # type: ignore[method-assign]
    event = SimpleNamespace(sender_id="anna")
    decision = SimpleNamespace(persona_text="persona", reply_budget=None)
    draft = asyncio.run(
        responder.generate_participation_draft(
            event,
            decision,
            purpose="why",
            context=context,
            model_profile="writer",
        )
    )

    assert draft == "draft"
    assert captured["content"] == sentinel


@pytest.mark.asyncio
async def test_label_cache_shared_across_arms_and_reruns(tmp_path: Path) -> None:
    history = _history(tmp_path, count=35)
    labels = RecordingClient(text=LABELS, route_key="labels")
    runner = _runner(tmp_path, history, labels=labels)
    report_dir = tmp_path / "run"
    try:
        prepared = await runner.prepare(RunConfig(replies=1, writer=0, seed=7, yes=True))
        first = await runner.execute(
            prepared, RunConfig(replies=1, writer=0, seed=7, yes=True), report_dir=report_dir
        )
        assert first.summary is not None
        assert len(labels.calls) == 1  # one call per item, shared by every pair
        assert first.summary["labels"]["s_view_with_derived_items"] == 1

        second_runner = _runner(tmp_path, history, labels=labels)
        second_prepared = await second_runner.prepare(
            RunConfig(replies=1, writer=0, seed=7, yes=True)
        )
        second = await second_runner.execute(
            second_prepared,
            RunConfig(replies=1, writer=0, seed=7, yes=True),
            report_dir=report_dir,
        )
        assert second.summary is not None
        assert len(labels.calls) == 1  # every label answer came from the cache
        assert second.summary["labels"]["label_cache_hits"] == 1
    finally:
        history.close()


@pytest.mark.asyncio
async def test_cost_guard_exits_without_yes(tmp_path: Path) -> None:
    history = _history(tmp_path, count=3)
    labels = RecordingClient(text=LABELS, route_key="labels")
    reply = RecordingClient(text="reply candidate", route_key=REPLY_ROUTE)
    grader = RecordingClient(text=GRADE, route_key=GRADER_ROUTE)
    runner = _runner(tmp_path, history, labels=labels, reply=reply, grader=grader)
    try:
        outcome = await runner.run(
            RunConfig(replies=1, writer=0, seed=7, yes=False),
            report_dir=tmp_path / "run",
        )
    finally:
        history.close()

    assert outcome.summary is None and outcome.report_dir is None
    assert not labels.calls and not reply.calls and not grader.calls
    assert outcome.planned.routes[REPLY_ROUTE] == 4
    assert outcome.planned.prompt_tokens > 0 and outcome.planned.completion_tokens > 0
    assert not (tmp_path / "run").exists()


@pytest.mark.asyncio
async def test_limit_calls_writes_partial_report(tmp_path: Path) -> None:
    history = _history(tmp_path, count=3)
    reply = RecordingClient(text="reply candidate", route_key=REPLY_ROUTE)
    runner = _runner(tmp_path, history, reply=reply)
    report_dir = tmp_path / "run"
    try:
        outcome = await runner.run(
            RunConfig(replies=1, writer=0, seed=7, yes=True, limit_calls=1),
            report_dir=report_dir,
        )
    finally:
        history.close()

    assert outcome.partial is True
    assert outcome.summary is not None and outcome.summary["partial"] is True
    assert len(reply.calls) == 1
    summary = json.loads((report_dir / "summary.json").read_text())
    assert summary["partial"] is True
    assert summary["calls"]["total"] == 1


@pytest.mark.asyncio
async def test_candidates_sampled_at_profile_temperature_grader_at_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import yeoman_gateway.evaluation.reply_state_run as run_module

    # A and A2 are only a noise floor when they are sampled the way live text is.
    monkeypatch.setattr(
        run_module,
        "_profile_temperature",
        lambda config, route: 0.7 if route == REPLY_ROUTE else None,
    )
    history = _history(tmp_path)
    reply = RecordingClient(text="reply candidate", route_key=REPLY_ROUTE)
    grader = RecordingClient(text=GRADE, route_key=GRADER_ROUTE)
    runner = _runner(tmp_path, history, reply=reply, grader=grader)
    try:
        await runner.run(
            RunConfig(replies=1, writer=0, seed=7, yes=True), report_dir=tmp_path / "run"
        )
    finally:
        history.close()

    assert reply.temperatures and set(reply.temperatures) == {0.7}
    assert grader.temperatures and set(grader.temperatures) == {None}
    candidates = [
        json.loads(line)
        for line in (tmp_path / "run" / "candidates.jsonl").read_text().splitlines()
    ]
    assert {candidate["temperature_used"] for candidate in candidates} == {0.7}


@pytest.mark.asyncio
async def test_report_has_no_message_text(tmp_path: Path) -> None:
    history = _history(tmp_path, marker_after=True)
    runner = _runner(tmp_path, history)
    report_dir = tmp_path / "run"
    try:
        outcome = await runner.run(
            RunConfig(replies=1, writer=0, seed=7, yes=True),
            report_dir=report_dir,
        )
    finally:
        history.close()

    assert outcome.summary is not None
    summary_text = (report_dir / "summary.json").read_text()
    report_text = (report_dir / "report.md").read_text()
    items_text = (report_dir / "items.jsonl").read_text()
    assert SECRET_AFTER not in summary_text + report_text + items_text
    assert "the item message" not in summary_text + report_text + items_text
    assert (report_dir / "candidates.jsonl").is_file()
    assert "text" in json.loads((report_dir / "candidates.jsonl").read_text().splitlines()[0])


@pytest.mark.asyncio
async def test_loaders_split_reply_and_writer_items(tmp_path: Path) -> None:
    history = _history(tmp_path, count=3)
    processing = tmp_path / "processing.db"
    ProcessingStore(processing).close()
    with sqlite3.connect(processing) as db:
        db.execute(
            "INSERT INTO threads (thread_id, channel, chat_id, root_principal, kind,"
            " state, opened_ms, last_activity_ms) VALUES ('th', 'whatsapp', ?, 'p', 'group',"
            " 'open', ?, ?)",
            (CHAT, T0, T0),
        )
        for turn, generation, outcome, source in (
            ("turn-send", "gen-send", "send", "item"),
            ("turn-error", "gen-error", "error", "m2"),
        ):
            db.execute(
                "INSERT INTO turns (turn_id, thread_id, principal, opened_ms, updated_ms,"
                " last_generation_id) VALUES (?, 'th', 'p', ?, ?, ?)",
                (turn, T0 + 35_000, T0 + 35_000, generation),
            )
            db.execute(
                "INSERT INTO generations (generation_id, turn_id, thread_id, revision,"
                " context_version, snapshot_hash, created_ms, outcome)"
                " VALUES (?, ?, 'th', 1, 1, 'hash', ?, ?)",
                (generation, turn, T0 + 35_000, outcome),
            )
            db.execute(
                "INSERT INTO turn_sources (turn_id, event_id, source_message_id, role,"
                " revision_at_join, added_ms) VALUES (?, ?, ?, 'trigger', 1, ?)",
                (turn, f"event-{turn}", source, T0 + 35_000),
            )
        db.execute(
            "INSERT INTO participation_admissions (admission_id, payload_json, created_ms)"
            " VALUES ('adm-effect-delivered', ?, ?)",
            (json.dumps({"opportunity_id": "opp-delivered"}), T0),
        )
        db.execute(
            "INSERT INTO effects (effect_id, operation_key, admission_id, payload_kind,"
            " payload_hash, state, created_ms, updated_ms)"
            " VALUES ('effect-delivered', 'op', 'adm-effect-delivered', 'text', 'hash',"
            " 'sent', ?, ?)",
            (T0, T0),
        )

    speakups = tmp_path / "speakups.db"
    SpeakupLog(speakups).close()
    with sqlite3.connect(speakups) as db:
        for index, (opportunity, disposition) in enumerate(
            (("opp-delivered", "comment_sent"), ("opp-failed", "comment_failed"))
        ):
            db.execute(
                "INSERT INTO judge_attempts (attempt_id, opportunity_id, evaluation_index,"
                " channel, chat_id, created_at_ms, hourly_limit, outcome)"
                " VALUES (?, ?, 0, 'whatsapp', ?, ?, 12, 'comment')",
                (f"{opportunity}:0", opportunity, CHAT, T0 + 35_000 + index),
            )
            db.execute(
                "INSERT INTO opportunity_dispositions (opportunity_id, channel, chat_id,"
                " observed_revision, activation_epoch, lane, trigger, source_ids_json,"
                " disposition, created_at_ms, updated_at_ms)"
                " VALUES (?, 'whatsapp', ?, 1, 1, 'production', 'inbound', ?, ?, ?, ?)",
                (
                    opportunity,
                    CHAT,
                    json.dumps(["item"]),
                    disposition,
                    T0 + 35_000 + index,
                    T0 + 35_000 + index,
                ),
            )
    try:
        replies = await load_reply_records(history, processing, since_ms=T0, until_ms=T0 + 60_000)
        writer = await load_writer_records(
            history, speakups, processing, since_ms=T0, until_ms=T0 + 60_000
        )
    finally:
        history.close()

    assert {record.item.stratum for record in replies} == {
        "addressed-answered",
        "addressed-unanswered",
    }
    by_stratum = {record.item.stratum: record for record in replies}
    assert by_stratum["addressed-answered"].item.hard_case is True
    assert by_stratum["addressed-unanswered"].item.hard_case is False
    assert {record.item.stratum for record in writer} == {"comment-delivered", "comment-failed"}
    assert {record.item.item_id for record in writer} == {
        "writer:opp-delivered",
        "writer:opp-failed",
    }


@pytest.mark.asyncio
async def test_writer_arms_use_the_extracted_prompt(tmp_path: Path) -> None:
    from yeoman_shared.config.schema import Config

    history = _history(tmp_path, count=3)
    snapshot = history.snapshot()
    try:
        item_row = HistoryQueries(snapshot).native_message(chat_id=CHAT, native_id="item")
        assert item_row is not None
        history_id = str(item_row["message_id"])
    finally:
        snapshot.close()
    opportunity = RecordedOpportunity(
        attempt_id="writer:0",
        opportunity_id="writer-opp",
        channel="whatsapp",
        chat_id=CHAT,
        at_ms=T0 + 35_000,
        live_outcome="comment",
        trigger="inbound",
        lane="production",
        observed_revision=1,
        activation_epoch=1,
        source_ids=(history_id,),
    )

    class FakeBuilder:
        async def build(self, opportunity, *, inputs, now_ms):
            # The real builder needs a live opportunity, not the recorded row.
            from yeoman_gateway.processing.participation import ParticipationOpportunity

            assert isinstance(opportunity, ParticipationOpportunity)
            assert tuple(opportunity.source_event_ids) == (history_id,)
            # Like the real builder: no target here; the runner sets it, as live does.
            return {
                "messages": [
                    # The real context carries native message ids, as the judge saw them.
                    {"event_id": "item", "sender": "anna", "text": "the item message"}
                ],
                "anchors": [],
            }

    writer = RecordingClient(text="writer candidate", route_key=WRITER_ROUTE)
    runner = ReplyStateRunner(
        config=Config(),
        history=history,
        context_builder=ContextBuilder(tmp_path / "workspace"),
        clients={
            REPLY_ROUTE: RecordingClient(route_key=REPLY_ROUTE),
            WRITER_ROUTE: writer,
            Config().processing.group_state.labels.route: RecordingClient(
                text=LABELS, route_key="labels"
            ),
            GRADER_ROUTE: RecordingClient(text=GRADE, route_key=GRADER_ROUTE),
        },
        participation_builder=FakeBuilder(),  # type: ignore[arg-type]
        bounds=ParticipationContextBounds(
            context_mode="conversation",
            window_minutes=120,
            max_messages=40,
            lull_minutes=25,
            burst_messages=4,
            burst_minutes=15,
            conversation_min_messages=0,
        ),
        writer_records=[
            WriterRecord(
                item=ReplyStateItem(
                    item_id="writer:writer-opp",
                    kind="writer",
                    chat_id=CHAT,
                    at_ms=T0 + 35_000,
                    stratum="comment-delivered",
                ),
                opportunity=opportunity,
                target_history_id=history_id,
                target_native_id="item",
                text="the item message",
            )
        ],
        reply_records=[],
    )
    try:
        outcome = await runner.run(
            RunConfig(replies=0, writer=1, seed=7, yes=True),
            report_dir=tmp_path / "run",
        )
    finally:
        history.close()

    assert outcome.summary is not None
    assert len(writer.calls) == 4  # A, A2, S, P
    assert writer.calls[0] == writer.calls[1]  # A2 is a second base sample
    assert writer.calls[0][0]["content"] != writer.calls[2][0]["content"]  # S has the view
    assert writer.calls[2][0]["content"] != writer.calls[3][0]["content"]  # P is its placebo
    assert outcome.summary["items"]["writer"] == 1


@pytest.mark.asyncio
async def test_oracle_adds_the_sx_arm_and_pair(tmp_path: Path) -> None:
    history = _history(tmp_path, count=35)
    runner = _runner(tmp_path, history)
    try:
        outcome = await runner.run(
            RunConfig(replies=1, writer=0, seed=7, yes=True, oracle=True),
            report_dir=tmp_path / "run",
        )
    finally:
        history.close()

    assert outcome.summary is not None
    assert outcome.summary["oracle"] is True
    assert "Sx_vs_A" in outcome.summary["pairs"]
    candidates = [
        json.loads(line)
        for line in (tmp_path / "run" / "candidates.jsonl").read_text().splitlines()
    ]
    assert "Sx" in {candidate["arm"] for candidate in candidates}


def test_arm_messages_is_a_pure_copy() -> None:
    base = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "message"},
    ]
    same = arm_messages(base, "")
    blocked = arm_messages(base, "[Group State] block")
    assert same == base
    assert blocked[0] == base[0]
    assert blocked[-1]["content"] == "message\n\n[Group State] block"
    assert base[-1]["content"] == "message"


def test_eval_route_keys_survive_config_key_round_trip() -> None:
    from yeoman_shared.config.loader import camel_to_snake, snake_to_camel

    for route in (REPLY_ROUTE, WRITER_ROUTE, GRADER_ROUTE):
        assert camel_to_snake(snake_to_camel(route)) == route


@pytest.mark.asyncio
async def test_concurrent_run_matches_serial_summary(tmp_path: Path) -> None:
    # Items in flight at once must not change what the report says.
    summaries = []
    for concurrency in (1, 4):
        history = _history(tmp_path / f"c{concurrency}")
        runner = _runner(tmp_path / f"c{concurrency}", history)
        try:
            outcome = await runner.run(
                RunConfig(replies=1, writer=0, seed=7, yes=True, concurrency=concurrency),
                report_dir=tmp_path / f"run{concurrency}",
            )
        finally:
            history.close()
        assert outcome.summary is not None
        summaries.append({key: outcome.summary[key] for key in ("pairs", "calls")})
    assert summaries[0] == summaries[1]
