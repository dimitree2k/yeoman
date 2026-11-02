"""The writer transcript's optional group-state block."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from loguru import logger
from yeoman_gateway.adapters.responder_llm import _render_participation_transcript
from yeoman_gateway.group_state.contract import ReaderContext
from yeoman_gateway.group_state.render import RenderedState
from yeoman_gateway.group_state.view_reader import GroupStateViewReader
from yeoman_gateway.group_state.views import ViewTarget
from yeoman_gateway.processing.participation import _JudgeContext
from yeoman_gateway.processing.participation_context import ParticipationContextBuilder
from yeoman_shared.config.schema import GroupStateConfig

from tests.gateway.group_state.test_views import LULL, _full_state
from tests.gateway.history_test_helpers import ProjectedHistory
from tests.gateway.test_participation_context import (
    CHAT,
    NOW_MS,
    _inputs,
    _opportunity,
    _record,
)

BLOCK = "[Group State] synthetic writer view"
MINUTE = 60_000


class _Reader:
    def __init__(self, text: str | None = None) -> None:
        self.text = text
        self.rendered = RenderedState(
            text=text or "",
            revision=3,
            age_ms=MINUTE,
            sections=("presence",),
            dropped=(),
            fallback=None,
        )
        self.calls: list[tuple[str, ReaderContext, ViewTarget]] = []

    def render(
        self, view: str, *, reader: ReaderContext, target: ViewTarget
    ) -> tuple[str | None, RenderedState]:
        self.calls.append((view, reader, target))
        return self.text, self.rendered


class _Store:
    def __init__(self, state: Any) -> None:
        self.state = state

    def get(self, channel: str, chat_id: str) -> Any:
        return self.state


def _builder(reader: Any) -> ParticipationContextBuilder:
    return ParticipationContextBuilder(
        policy=None,
        source_authorizer=lambda row: True,
        group_state_views=reader,
    )


async def _build(tmp_path: Path, reader: Any) -> tuple[dict[str, object], Any]:
    history = ProjectedHistory(tmp_path / "history")
    _record(history, message_id="cur", text="current line", minutes_ago=1)
    opportunity = _opportunity("cur")
    try:
        async with history.turn():
            context = await _builder(reader).build(
                opportunity,
                inputs=_inputs(current_source_ids=opportunity.source_event_ids),
                now_ms=NOW_MS,
            )
    finally:
        history.close()
    return context, opportunity


@pytest.mark.asyncio
async def test_writer_context_gets_state_on(tmp_path: Path) -> None:
    reader = _Reader(BLOCK)

    context, opportunity = await _build(tmp_path, reader)

    assert context["writer_state_text"] == BLOCK
    assert "group_state_text" not in context
    assert "writer_state_status" not in context
    view, reader_context, target = reader.calls[0]
    assert view == "writer"
    assert (reader_context.channel, reader_context.chat_id) == ("whatsapp", CHAT)
    assert target.message_id == str(opportunity.source_event_ids[-1])
    assert target.reply_to_message_id is None


@pytest.mark.asyncio
async def test_writer_state_never_in_judge_context(tmp_path: Path) -> None:
    reader = _Reader(BLOCK)

    context, _ = await _build(tmp_path, reader)
    judge = _JudgeContext.from_mapping(context)

    assert BLOCK not in judge.render()
    assert judge.group_state_text == ""
    assert not hasattr(judge, "writer_state_text")


@pytest.mark.asyncio
async def test_writer_shadow_logs_not_rendered(tmp_path: Path) -> None:
    state = replace(_full_state(), chat_id=CHAT)
    settings = GroupStateConfig.model_validate(
        {
            "mode": "shadow",
            "chats": [f"whatsapp:{CHAT}"],
            "views": {
                "writer": {
                    "mode": "shadow",
                    "chats": [f"whatsapp:{CHAT}"],
                    "maxChars": 2_000,
                }
            },
        }
    )
    reader = GroupStateViewReader(
        store=_Store(state),  # type: ignore[arg-type]
        settings=settings,
        lull_ms=LULL,
        clock_ms=lambda: 10_000_000,
    )
    lines: list[str] = []
    sink = logger.add(lambda message: lines.append(message.record["message"]), level="INFO")
    try:
        context, _ = await _build(tmp_path, reader)
    finally:
        logger.remove(sink)

    assert "writer_state_text" not in context
    assert any(
        line.startswith("group_state_view view=writer") and "mode=shadow" in line for line in lines
    )


def _state_context() -> dict[str, Any]:
    return {
        "target_message_id": "cur",
        "messages": [
            {"event_id": "o1", "sender": "anna", "text": "optional line"},
            {"event_id": "cur", "sender": "ben", "text": "current"},
        ],
        "anchors": [],
        "writer_state_text": BLOCK,
    }


def test_writer_transcript_without_state_byte_identical() -> None:
    context = _state_context()
    context.pop("writer_state_text")

    rendered = _render_participation_transcript(context)

    assert rendered == "[CURRENT] ben: current\n[CONTEXT] anna: optional line"
    assert context["writer_state_status"] == "absent"


def test_writer_state_block_before_context_entries() -> None:
    context = _state_context()

    rendered = _render_participation_transcript(context)

    assert rendered == (f"[CURRENT] ben: current\n{BLOCK}\n[CONTEXT] anna: optional line")
    assert context["writer_state_status"] == "rendered"


def test_writer_state_dropped_before_any_context_entry() -> None:
    context = _state_context()
    without_state = _render_participation_transcript(
        {key: value for key, value in context.items() if key != "writer_state_text"}
    )
    # The state fits beside the target, but not beside the optional entry. The entry
    # outranks it: the state is dropped first, never the entry.
    context["writer_state_text"] = BLOCK
    limit = len("[CURRENT] ben: current") + 1 + len(BLOCK)

    rendered = _render_participation_transcript(context, limit=limit)

    assert rendered == without_state
    assert BLOCK not in rendered
    assert context["writer_state_status"] == "budget_dropped"
    assert context["writer_dropped_entry_ids"] == []


def test_writer_state_dropped_first_on_budget() -> None:
    context = _state_context()
    without_state = _render_participation_transcript(
        {key: value for key, value in context.items() if key != "writer_state_text"}
    )
    context["writer_state_text"] = BLOCK
    context["writer_state_status"] = "rendered"

    rendered = _render_participation_transcript(context, limit=len(without_state))

    assert rendered == "[CURRENT] ben: current\n[CONTEXT] anna: optional line"
    assert BLOCK not in rendered
    assert context["writer_state_status"] == "budget_dropped"
    assert context["writer_dropped_entry_ids"] == []
