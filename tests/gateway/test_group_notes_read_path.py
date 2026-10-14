"""Weekly group notes read path (Spec B step B3, section 9 read gate).

Synthetic and offline: projected synthetic history and a prepared empty Knowledge copy.
Nothing reads a runtime path or calls a provider.
"""

from __future__ import annotations

import inspect
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from yeoman_gateway.adapters.responder_llm import _render_participation_transcript
from yeoman_gateway.knowledge.api import open_knowledge_store
from yeoman_gateway.knowledge.authority import FakePolicyAuthority
from yeoman_gateway.knowledge.runtime import RuntimeKnowledgeSources
from yeoman_gateway.processing.participation import (
    ParticipationJudge,
    ParticipationOpportunity,
    _JudgeContext,
)
from yeoman_gateway.processing.participation_context import ParticipationContextBuilder
from yeoman_gateway.processing.participation_group_notes import (
    MAX_NOTE_AGE_MS,
    SHADOW_NOTE_KEY,
    GroupNotesReader,
    render_group_notes_block,
)

from tests.gateway.history_test_helpers import ProjectedHistory, prepare_empty_history_knowledge
from tests.gateway.test_participation_context import (
    CHANNEL,
    CHAT,
    NOW,
    NOW_MS,
    OTHER_CHAT,
    _inputs,
    _record,
)

DM = "10009@s.whatsapp.net"
DAY = 86_400_000
NOTE = "Short dry jokes land; long explainers end threads."
HEADER = "[Group notes, week 2026-W16 · style guidance from reactions, never fact]"


class Notes:
    """Synthetic history, a real Knowledge store and a switchable groupNotes mode."""

    def __init__(self, tmp_path: Path) -> None:
        self.history = ProjectedHistory(tmp_path / "history")
        self.knowledge = open_knowledge_store(
            prepare_empty_history_knowledge(tmp_path),
            workspace_id="group-notes-test",
            source_authority=RuntimeKnowledgeSources(),
            policy_authority=FakePolicyAuthority(),
        )
        self.mode = "on"
        self.reader = GroupNotesReader(knowledge=self.knowledge, mode=lambda: self.mode)
        for chat in (CHAT, OTHER_CHAT, DM):
            _record(self.history, message_id=f"{chat[:5]}-src", text="older line",
                    minutes_ago=600, chat_id=chat)
            _record(self.history, message_id=f"{chat[:5]}-now", text="current line",
                    minutes_ago=1, chat_id=chat)

    def store(self, chat: str = CHAT, *, week: str = "2026-W16", end_ms: int | None = None,
              sources: tuple[str, ...] | None = None) -> Any:
        end = NOW_MS - 5 * DAY if end_ms is None else end_ms
        ids = sources if sources is not None else (f"{CHANNEL}:{chat}:{chat[:5]}-src",)
        return self.knowledge.store_group_digest(
            chat_id=chat, week=week, content={"goes_down_well": [NOTE]}, rendered=NOTE,
            model_version="m", prompt_version="p", confidence="medium",
            signals={"reactions": 30}, window_start_ms=end - 7 * DAY, window_end_ms=end,
            sources=[(source, "approval") for source in ids], now_ms=end + 3600_000,
        )

    def builder(self, *, reader: bool = True) -> ParticipationContextBuilder:
        return self.history.wire(ParticipationContextBuilder(
            policy=None, source_authorizer=lambda row: True,
            group_notes=self.reader if reader else None,
        ))

    async def context(self, chat: str = CHAT, *, reader: bool = True,
                      now_ms: int = NOW_MS) -> dict[str, Any]:
        opportunity = _opportunity(chat)
        async with self.history.turn():
            return await self.builder(reader=reader).build(
                opportunity, inputs=_inputs(current_source_ids=opportunity.source_event_ids),
                now_ms=now_ms,
            )

    def close(self) -> None:
        self.history.close()
        self.knowledge.close()


def _opportunity(chat: str = CHAT, *, source: str | None = None) -> ParticipationOpportunity:
    return ParticipationOpportunity(
        opportunity_id=f"opp-{chat[:5]}", channel=CHANNEL, chat_id=chat, trigger="inbound",
        source_event_ids=(source or f"{chat[:5]}-now",), observed_revision=1,
        activation_epoch=1, created_at_ms=NOW_MS,
    )


@pytest.fixture
def notes(tmp_path: Path):
    harness = Notes(tmp_path)
    try:
        yield harness
    finally:
        harness.close()


def _judge_text(context: dict[str, Any]) -> str:
    return _JudgeContext.from_mapping(context).render()


def _stable(context: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in context.items()
            if key != "knowledge_selection_elapsed_ms"}


# ── read gate ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_on_renders_the_chats_own_note_as_one_fixed_block(notes: Notes) -> None:
    digest = notes.store()
    context = await notes.context()
    assert (context["group_notes_text"], context["group_notes_week"],
            context["group_notes_digest_id"]) == (NOTE, "2026-W16", digest.digest_id)
    rendered = _judge_text(context)
    assert rendered.count("[Group notes") == 1
    assert f"{HEADER}\n{NOTE}" in rendered
    assert render_group_notes_block("2026-W16", NOTE) == f"{HEADER}\n{NOTE}"


@pytest.mark.asyncio
async def test_note_of_chat_x_never_renders_for_chat_y_or_a_direct_chat(notes: Notes) -> None:
    notes.store(CHAT)
    # A direct chat never gets a note, even one stored under its own id.
    notes.store(DM, sources=(f"{CHANNEL}:{DM}:{DM[:5]}-src",))
    for chat in (OTHER_CHAT, DM):
        context = await notes.context(chat)
        assert "group_notes_text" not in context
        assert SHADOW_NOTE_KEY not in context
        assert "[Group notes" not in _judge_text(context)
    notes.mode = "shadow"
    for chat in (OTHER_CHAT, DM):
        assert SHADOW_NOTE_KEY not in await notes.context(chat)
    # Another channel with the same chat id is another chat.
    async with notes.history.turn():
        assert notes.reader.note_for("telegram", CHAT, now_ms=NOW_MS) is None
        assert notes.reader.note_for(CHANNEL, CHAT, now_ms=NOW_MS) is not None


@pytest.mark.asyncio
async def test_deleted_source_message_makes_the_note_stale_and_unrendered(notes: Notes) -> None:
    notes.store()
    assert (await notes.context()).get("group_notes_text") == NOTE
    notes.history.append("delete", chat_id=CHAT, timestamp=(NOW - timedelta(minutes=2)).timestamp(),
                         messageId=f"{CHAT[:5]}-src")
    context = await notes.context()
    assert "group_notes_text" not in context
    assert "[Group notes" not in _judge_text(context)
    assert notes.knowledge.group_digests(chat_id=CHAT)[0].status == "stale"


@pytest.mark.asyncio
async def test_stale_digest_falls_back_to_nothing_not_to_an_older_week(notes: Notes) -> None:
    notes.store(week="2026-W15", end_ms=NOW_MS - 12 * DAY)
    notes.store(week="2026-W16", sources=(f"{CHANNEL}:{CHAT}:missing",))
    assert "group_notes_text" not in await notes.context()


@pytest.mark.asyncio
async def test_note_older_than_fourteen_days_is_not_rendered(notes: Notes) -> None:
    notes.store(end_ms=NOW_MS - MAX_NOTE_AGE_MS)
    assert (await notes.context()).get("group_notes_text") == NOTE  # exactly 14 days
    notes.store(week="2026-W15", end_ms=NOW_MS - MAX_NOTE_AGE_MS - 1)
    assert "group_notes_text" not in await notes.context()
    # A digest written after the decision's moment is not visible to it.
    notes.store(week="2026-W17", end_ms=NOW_MS)
    assert "group_notes_text" not in await notes.context(now_ms=NOW_MS)


@pytest.mark.asyncio
async def test_off_mode_and_no_reader_leave_the_context_unchanged(notes: Notes) -> None:
    notes.store()
    notes.mode = "off"
    without = await notes.context(reader=False)
    off = await notes.context()
    assert _stable(off) == _stable(without)
    assert _judge_text(off) == _judge_text(without)
    notes.mode = "bogus"  # an unknown setting is off
    assert _stable(await notes.context()) == _stable(without)


@pytest.mark.asyncio
async def test_shadow_mode_holds_the_note_aside_and_live_renders_nothing(notes: Notes) -> None:
    digest = notes.store()
    without = await notes.context(reader=False)
    notes.mode = "shadow"
    context = await notes.context()
    assert context[SHADOW_NOTE_KEY] == {"digest_id": digest.digest_id, "chat_id": CHAT,
                                        "week": "2026-W16", "text": NOTE}
    assert "group_notes_text" not in context
    assert _judge_text(context) == _judge_text(without)
    shown = {key: value for key, value in _stable(context).items() if key != SHADOW_NOTE_KEY}
    assert shown == _stable(without)


def test_recall_never_reads_group_digests() -> None:
    from yeoman_gateway.knowledge import _embedding_index, _retrieval

    for module in (_retrieval, _embedding_index):
        assert "group_digest" not in inspect.getsource(module)


# ── budget order ─────────────────────────────────────────────────────────────


class _NoClient:
    route_key = "test.participation"

    async def chat_with_usage(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("budget tests never call a model")


def _budget_context(*, optional: int, knowledge: str = "", notes: str = NOTE) -> dict[str, Any]:
    messages = [
        {"event_id": f"o{index}", "sender": "anna", "text": "optional line " * 20,
         "timestamp": index}
        for index in range(optional)
    ]
    messages.append({"event_id": "cur", "sender": "ben", "text": "current", "timestamp": 999})
    return {
        "messages": messages, "anchors": [], "current_source_ids": ["cur"],
        "allowed_actions": ["silence", "react", "comment"], "allowed_intents": ["initiate"],
        **({"selected_knowledge_text": knowledge} if knowledge else {}),
        **({"group_notes_text": notes, "group_notes_week": "2026-W16"} if notes else {}),
    }


def _fit(context: dict[str, Any], *, max_input_tokens: int) -> dict[str, Any]:
    judge = ParticipationJudge(client=_NoClient(), max_input_tokens=max_input_tokens)
    judge._fit_context_for_judge(context, _opportunity(source="cur"))  # noqa: SLF001
    return context


def _tokens(context: dict[str, Any]) -> int:
    judge = ParticipationJudge(client=_NoClient(), max_input_tokens=100_000)
    view = _JudgeContext.from_mapping(context)
    return judge._estimate_tokens(judge._build_messages(_opportunity(source="cur"), view))  # noqa: SLF001


def test_judge_drops_optional_messages_before_group_notes() -> None:
    full = _budget_context(optional=6, knowledge="synthetic knowledge line")
    lean = _budget_context(optional=0, knowledge="synthetic knowledge line")
    context = _fit(full, max_input_tokens=_tokens(lean) + 5)
    assert [row["event_id"] for row in context["messages"]] == ["cur"]
    assert context["group_notes_rendered_to_judge"] is True
    assert context["knowledge_rendered_to_judge"] is True
    assert f"{HEADER}\n{NOTE}" in _JudgeContext.from_mapping(context).render()


def test_judge_drops_group_notes_before_knowledge() -> None:
    lean = _budget_context(optional=0, knowledge="synthetic knowledge line", notes="")
    context = _fit(_budget_context(optional=3, knowledge="synthetic knowledge line"),
                   max_input_tokens=_tokens(lean) + 2)
    assert context["group_notes_rendered_to_judge"] is False
    assert context["group_notes_status"] == "budget_dropped"
    assert context["knowledge_rendered_to_judge"] is True
    assert "[Group notes" not in _JudgeContext.from_mapping(context).render()


def test_writer_budget_drops_group_notes_whole_before_knowledge() -> None:
    knowledge = "k" * 3700
    context = _fit(_budget_context(optional=0, knowledge=knowledge, notes="n" * 400),
                   max_input_tokens=100_000)
    assert context["group_notes_status"] == "budget_dropped"
    assert context["knowledge_rendered_to_judge"] is True


def _writer_context(*, knowledge: str = "", notes: str = NOTE, optional: int = 2) -> dict:
    return {
        "target_message_id": "cur",
        "messages": [
            *({"event_id": f"o{index}", "sender": "anna", "text": "optional line " * 10}
              for index in range(optional)),
            {"event_id": "cur", "sender": "ben", "text": "current"},
        ],
        "anchors": [],
        **({"selected_knowledge_text": knowledge} if knowledge else {}),
        **({"group_notes_text": notes, "group_notes_week": "2026-W16"} if notes else {}),
    }


def test_writer_renders_the_block_and_drops_optional_entries_first() -> None:
    context = _writer_context(knowledge="synthetic knowledge line")
    fixed = len(_render_participation_transcript(_writer_context(
        knowledge="synthetic knowledge line", optional=0)))
    rendered = _render_participation_transcript(context, limit=fixed + 20)
    assert f"{HEADER}\n{NOTE}" in rendered
    assert "synthetic knowledge line" in rendered
    assert context["group_notes_rendered_to_writer"] is True
    assert context["writer_dropped_entry_ids"] == ["o0", "o1"]


def test_writer_drops_group_notes_before_knowledge() -> None:
    context = _writer_context(knowledge="synthetic knowledge line", optional=0)
    without = len(_render_participation_transcript(_writer_context(
        knowledge="synthetic knowledge line", notes="", optional=0)))
    rendered = _render_participation_transcript(context, limit=without + 5)
    assert "[Group notes" not in rendered
    assert "synthetic knowledge line" in rendered
    assert context["group_notes_rendered_to_writer"] is False
    assert context["group_notes_status"] == "budget_dropped"
    assert context["knowledge_rendered_to_writer"] is True


def test_writer_without_a_note_is_unchanged() -> None:
    plain = _writer_context(notes="")
    assert "[Group notes" not in _render_participation_transcript(plain)
    assert plain["group_notes_rendered_to_writer"] is False
    assert "group_notes_status" not in plain
