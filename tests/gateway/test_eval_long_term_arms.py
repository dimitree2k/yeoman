"""Long-term eval arms: as-of inputs, placebos, grades and the CI gate."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from yeoman_gateway.evaluation.long_term_context import (
    AsOfCache,
    build_past_block_for_conversations,
    build_people_block,
    last_daily_cut_ms,
)
from yeoman_gateway.evaluation.reply_state import (
    ReplyStateItem,
    build_placebo,
    layer_gate,
)
from yeoman_gateway.evaluation.reply_state_run import (
    GRADES_FILE,
    RunConfig,
)

from tests.gateway.test_eval_reply_state_run import (
    CHAT,
    GRADE,
    T0,
    RecordingClient,
    ReplyRecord,
    _history,
    _record,
    _runner,
)

DAY = 86_400_000


# ── direct as-of builders ─────────────────────────────────────────────────────


class _Store:
    def query_one(self, sql: str, params: tuple[Any, ...]) -> dict[str, Any]:
        return {"speaker": "p-tom", "updated_ms": 10, "content": "likes charts"}


class _Knowledge:
    policy_revision = 1

    def __init__(self) -> None:
        self._store = _Store()
        self.queries: list[Any] = []

    def person_for_principal(self, principal: str) -> str | None:
        return "p-tom"

    def canonical_id(self, person_id: str) -> str:
        return str(person_id)

    def display_name(
        self, person_id: str, *, context: Any = None, for_group: bool = False
    ) -> str | None:
        return "Tom"

    def recall(self, query: Any, *, context: Any, **kwargs: Any) -> Any:
        self.queries.append(query)
        assert query.as_of_ms is not None, "long-term reads must be as-of"
        return SimpleNamespace(statement_ids=("s1",), text="before the cut")


class _Queries:
    def __init__(self, *, cut_ms: int) -> None:
        self.cut_ms = cut_ms
        self.calls: list[tuple[str, int | None]] = []

    def members(self, *, chat_id: str, at_ms: int) -> Any:
        self.calls.append(("members", at_ms))
        return SimpleNamespace(status="known", members=frozenset({"whatsapp:4910000000002"}))

    def recent(
        self, *, chat_id: str, limit: int, until_ms: int | None = None
    ) -> list[dict[str, Any]]:
        self.calls.append(("recent", until_ms))
        rows = [
            {
                "message_id": "m1",
                "direction": "in",
                "sender_contact_id": "p-tom",
                "text": "before the cut",
                "sent_ms": self.cut_ms - 1,
            },
            {
                "message_id": "m2",
                "direction": "in",
                "sender_contact_id": "p-tom",
                "text": "synthetic later wording",
                "sent_ms": self.cut_ms + 1000,
            },
        ]
        return [row for row in rows if until_ms is None or row["sent_ms"] <= until_ms]

    def terminal(self, contact_id: str) -> str:
        return str(contact_id)

    def content_fingerprint(self, message_id: str) -> str:
        return f"fp-{message_id}"

    def assistant_contact_ids(self) -> frozenset[str]:
        return frozenset()

    def contact_names(self, contact_ids: tuple[str, ...]) -> dict[str, str]:
        return {str(item): "Tom" for item in contact_ids}


class _ChatClient:
    def __init__(self, *, kind: str = "people") -> None:
        self.kind = kind
        self.calls: list[list[dict[str, str]]] = []
        self.model = "fake"

    async def chat_with_usage(self, messages: list[dict[str, str]], **kwargs: Any) -> Any:
        self.calls.append(messages)
        if self.kind == "people":
            content = json.dumps({"lines": [{"text": "observed line", "refs": ["m1"]}]})
        else:
            is_week = "closed ISO week" in str(messages[0]["content"])
            content = json.dumps(
                {
                    "lines": [
                        {
                            "text": "week line" if is_week else "conversation line",
                            "refs": ["c1" if is_week else "m1"],
                        }
                    ]
                }
            )
        return SimpleNamespace(content=content, usage={"prompt_tokens": 4})


@pytest.mark.asyncio
async def test_no_arm_sees_later_or_corrected_input(tmp_path: Path) -> None:
    cut_ms = last_daily_cut_ms(T0)
    knowledge = _Knowledge()
    queries = _Queries(cut_ms=cut_ms)
    client = _ChatClient()
    cache = AsOfCache(tmp_path / "cache.jsonl")
    block = await build_people_block(
        knowledge=knowledge,
        queries=queries,
        channel="whatsapp",
        chat_id=CHAT,
        at_ms=T0,
        target=SimpleNamespace(
            sender_principal="whatsapp:4910000000002",
            reply_to_principal=None,
            mentioned_principals=(),
        ),
        cache=cache,
        client=client,
        policy_revision=1,
    )

    assert "likes charts" in block
    assert "observed line" in block
    assert "synthetic later wording" not in "".join(
        str(message.get("content")) for call in client.calls for message in call
    )
    assert knowledge.queries and all(query.as_of_ms <= cut_ms for query in knowledge.queries)
    assert all(at_ms == cut_ms for name, at_ms in queries.calls if name == "members")
    assert all(until == cut_ms for name, until in queries.calls if name == "recent")
    assert (
        not (tmp_path / "cache.jsonl").exists()
        or "(later wording)" not in (tmp_path / "cache.jsonl").read_text()
    )


@pytest.mark.asyncio
async def test_past_block_excludes_conversations_ended_inside_the_tick(
    tmp_path: Path,
) -> None:
    cut_ms = T0
    tick = 30_000

    def conversation(identifier: str, start: int, last: int) -> Any:
        return SimpleNamespace(
            id=identifier,
            chat_id=CHAT,
            start_ms=start,
            last_ms=last,
            message_ids=(f"{identifier}-m1",),
            message_count=4,
            state="ended",
        )

    old = conversation("old", T0 - 10 * DAY, T0 - 10 * DAY + 60_000)
    later = conversation("later", T0 - 1000, T0 + 1000)
    queries = _Queries(cut_ms=cut_ms)
    rows = {
        "old-m1": {
            "message_id": "old-m1",
            "direction": "in",
            "sender_contact_id": "p-tom",
            "text": "synthetic old conversation",
            "sent_ms": old.start_ms + 1,
            "current_text": "synthetic old conversation",
        },
        "later-m1": {
            "message_id": "later-m1",
            "direction": "in",
            "sender_contact_id": "p-tom",
            "text": "synthetic later conversation",
            "sent_ms": later.start_ms + 1,
            "current_text": "synthetic later conversation",
        },
    }
    queries.recent = lambda *, chat_id, limit, after_ms=None, until_ms=None: [  # type: ignore[method-assign]
        row
        for row in rows.values()
        if (after_ms is None or row["sent_ms"] > after_ms)
        and (until_ms is None or row["sent_ms"] <= until_ms)
    ]
    client = _ChatClient(kind="summaries")
    block = await build_past_block_for_conversations(
        queries=queries,
        at_ms=T0,
        settings=SimpleNamespace(
            min_messages=4,
            min_interval_seconds=30,
            keep_weeks=12,
            window_hours=24,
            max_input_chars=12_000,
        ),
        conversations=[old, later],
        client=client,
        cache=AsOfCache(tmp_path / "cache.jsonl"),
        tick=tick,
    )

    assert "conversation line" in block
    joined = "".join(str(message.get("content")) for call in client.calls for message in call)
    assert "synthetic old conversation" in joined
    assert "synthetic later conversation" not in joined
    assert "week line" in block


# ── runner integration ─────────────────────────────────────────────────────────


def _old_record(history: Any, *, signals: tuple[str, ...] = ()) -> ReplyRecord:
    base = _record(history, at_ms=T0 + 35_000)
    return ReplyRecord(
        item=ReplyStateItem(
            item_id=base.item.item_id,
            kind="reply",
            chat_id=base.item.chat_id,
            at_ms=base.item.at_ms,
            stratum=base.item.stratum,
            hard_case=True,
            older_context_signals=signals,
        ),
        history_id=base.history_id,
        native_id=base.native_id,
        text=base.text,
        sender_principal=base.sender_principal,
    )


def _patch_blocks(monkeypatch: pytest.MonkeyPatch, *, people: str, past: str) -> None:
    import yeoman_gateway.evaluation.reply_state_run as run_module

    async def fake_people(**kwargs: Any) -> str:
        return people

    async def fake_past(**kwargs: Any) -> str:
        return past

    monkeypatch.setattr(run_module, "build_people_block", fake_people)
    monkeypatch.setattr(run_module, "build_past_block", fake_past)


@pytest.mark.asyncio
async def test_placebo_per_layer_matches_length(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    people = "[People here]\n- Tom: likes synthetic charts and plays chess"
    past = "[Earlier in this group]\n- they planned the synthetic trip in May"
    _patch_blocks(monkeypatch, people=people, past=past)
    history = _history(tmp_path, count=35)
    reply = RecordingClient(text="reply candidate")
    from tests.gateway.test_eval_reply_state_run import FakeRecallKnowledge

    runner = _runner(tmp_path, history, reply=reply, knowledge=FakeRecallKnowledge())
    try:
        prepared = await runner.prepare(RunConfig(replies=1, writer=0, seed=7, yes=True))
        prepared = type(prepared)(
            selection=prepared.selection,
            replies=(_old_record(history),),
            writer=(),
            planned=prepared.planned,
        )
        await runner.execute(
            prepared,
            RunConfig(
                replies=1,
                writer=0,
                seed=7,
                yes=True,
                arms=("N", "P_N", "H", "P_H"),
            ),
            report_dir=tmp_path / "run",
        )
    finally:
        history.close()

    prompts = [call[-1]["content"] for call in reply.calls]
    assert len(prompts) == 4
    n_prompt, pn_prompt, h_prompt, ph_prompt = prompts
    assert people in n_prompt and past in h_prompt
    assert abs(len(pn_prompt) - len(n_prompt)) <= 0.1 * len(n_prompt)
    assert abs(len(ph_prompt) - len(h_prompt)) <= 0.1 * len(h_prompt)
    assert build_placebo(people, seed=7) != ""


@pytest.mark.asyncio
async def test_past_layer_runs_without_knowledge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    past = "[Earlier in this group]\n- they planned a trip"
    _patch_blocks(monkeypatch, people="", past=past)
    history = _history(tmp_path, count=35)
    reply = RecordingClient(text="reply candidate")
    runner = _runner(tmp_path, history, reply=reply)
    try:
        prepared = await runner.prepare(RunConfig(replies=1, writer=0, seed=7, yes=True))
        prepared = type(prepared)(
            selection=prepared.selection,
            replies=(_old_record(history),),
            writer=(),
            planned=prepared.planned,
        )
        await runner.execute(
            prepared,
            RunConfig(replies=1, writer=0, seed=7, yes=True, arms=("H",)),
            report_dir=tmp_path / "run",
        )
    finally:
        history.close()

    assert reply.calls and past in reply.calls[-1][-1]["content"]


@pytest.mark.asyncio
async def test_report_has_older_context_stratum_and_block_subset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_blocks(
        monkeypatch,
        people="[People here]\n- Tom: likes charts",
        past="[Earlier in this group]\n- they planned a trip",
    )
    history = _history(tmp_path, count=35)
    from tests.gateway.test_eval_reply_state_run import FakeRecallKnowledge

    runner = _runner(tmp_path, history, knowledge=FakeRecallKnowledge())
    record = _old_record(history, signals=("reply_outside_window",))
    try:
        prepared = await runner.prepare(RunConfig(replies=1, writer=0, seed=7, yes=True))
        prepared = type(prepared)(
            selection=prepared.selection,
            replies=(record,),
            writer=(),
            planned=prepared.planned,
        )
        outcome = await runner.execute(
            prepared,
            RunConfig(
                replies=1,
                writer=0,
                seed=7,
                yes=True,
                arms=("A", "A2", "N", "P_N"),
            ),
            report_dir=tmp_path / "run",
        )
    finally:
        history.close()

    summary = outcome.summary
    assert summary is not None
    pair = summary["pairs"]["N_vs_A"]
    assert (
        pair["strata"]["older_context"]["wins"]
        + pair["strata"]["older_context"]["losses"]
        + pair["strata"]["older_context"]["ties"]
        == 1
    )
    assert (
        pair["older_context"]["wins"]
        + pair["older_context"]["losses"]
        + pair["older_context"]["ties"]
        == 1
    )
    assert pair["block_present_items"] == 1
    assert (
        pair["block_present"]["wins"]
        + pair["block_present"]["losses"]
        + pair["block_present"]["ties"]
        == 1
    )
    assert "layers" in summary["gate"] and "N" in summary["gate"]["layers"]

    grades = [
        json.loads(line) for line in (tmp_path / "run" / GRADES_FILE).read_text().splitlines()
    ]
    assert grades and all("text" not in grade for grade in grades)
    assert {grade["order"] for grade in grades} == {1, 2}
    assert "the item message" not in (tmp_path / "run" / GRADES_FILE).read_text()


@pytest.mark.asyncio
async def test_grader_background_is_the_union_of_the_item_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    people = "[People here]\n- Tom: likes charts"
    past = "[Earlier in this group]\n- they planned a trip"
    _patch_blocks(monkeypatch, people=people, past=past)
    history = _history(tmp_path, count=35)
    grader = RecordingClient(text=GRADE)
    from tests.gateway.test_eval_reply_state_run import FakeRecallKnowledge

    runner = _runner(tmp_path, history, grader=grader, knowledge=FakeRecallKnowledge())
    try:
        prepared = await runner.prepare(RunConfig(replies=1, writer=0, seed=7, yes=True))
        prepared = type(prepared)(
            selection=prepared.selection,
            replies=(_old_record(history),),
            writer=(),
            planned=prepared.planned,
        )
        await runner.execute(
            prepared,
            RunConfig(replies=1, writer=0, seed=7, yes=True, arms=("A", "N", "H")),
            report_dir=tmp_path / "run",
        )
    finally:
        history.close()

    assert grader.calls
    for call in grader.calls:
        assert people in call[-1]["content"]
        assert past in call[-1]["content"]


def test_gate_uses_ci_bounds() -> None:
    gate = layer_gate(
        x_vs_a={"win_rate": 0.7, "ci95": [0.66, 0.74]},
        a2_vs_a={"win_rate": 0.55, "ci95": [0.45, 0.65]},
        x_vs_placebo={"win_rate": 0.6},
        x_contradicts=0.1,
        a_contradicts=0.2,
        read_p95_ms=3.0,
    )

    assert gate["threshold"] == 0.65
    assert gate["x_gt_a"] is True
    assert gate["x_gt_placebo"] is True
    assert gate["contradictions_not_higher"] is True
    assert gate["read_p95_ok"] is True
    assert gate["passed"] is True

    below = layer_gate(
        x_vs_a={"win_rate": 0.7, "ci95": [0.64, 0.74]},
        a2_vs_a={"win_rate": 0.55, "ci95": [0.45, 0.65]},
        x_vs_placebo={"win_rate": 0.6},
        x_contradicts=0.1,
        a_contradicts=0.2,
        read_p95_ms=3.0,
    )
    assert below["x_gt_a"] is False and below["passed"] is False


def test_temporal_grounding_uses_the_item_time(tmp_path: Path) -> None:
    from datetime import datetime

    from yeoman_gateway.agent.context import ContextBuilder
    from yeoman_gateway.evaluation.reply_state_run import build_reply_base_messages

    moment = datetime.fromtimestamp(T0 / 1000).astimezone()
    pin = ContextBuilder._build_temporal_grounding(moment)  # noqa: SLF001
    assert f"Current local datetime: {moment.isoformat(timespec='seconds')}" in pin

    messages = build_reply_base_messages(
        ContextBuilder(tmp_path / "workspace"),
        text="hello",
        persona_text=None,
        channel="whatsapp",
        chat_id=CHAT,
        ambient=[],
        reply_to=None,
        now=moment,
    )
    assert any(
        message["content"].startswith("# Temporal Grounding")
        and moment.isoformat(timespec="seconds") in message["content"]
        for message in messages
    )


def test_live_temporal_grounding_still_uses_the_wall_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import datetime

    import yeoman_gateway.agent.context as context_module
    from yeoman_gateway.agent.context import ContextBuilder

    fixed = datetime(2026, 11, 2, 9, 30, tzinfo=datetime.now().astimezone().tzinfo)

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[no-untyped-def]
            return fixed

    monkeypatch.setattr(context_module, "datetime", _FixedDatetime)
    live = ContextBuilder._build_temporal_grounding()  # noqa: SLF001
    assert f"Current local datetime: {fixed.isoformat(timespec='seconds')}" in live
    assert live == ContextBuilder._build_temporal_grounding(fixed)  # noqa: SLF001
