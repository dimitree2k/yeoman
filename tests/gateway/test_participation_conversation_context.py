"""Conversation context mode (Spec A section 5.2): a subset of today's window.

Synthetic chats and synthetic participants only. Nothing here talks to a provider.
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest
from loguru import logger
from yeoman_gateway.processing import participation_context
from yeoman_gateway.processing.participation import _JudgeContext
from yeoman_gateway.processing.participation_context import ParticipationContextBounds

from tests.gateway.test_participation_context import (
    NOW,
    _build_context,
    _builder,
    _opportunity,
    _record,
)

CHAT = "group@g.us"


def _bounds(mode: str, *, max_messages: int = 40, **kwargs: int) -> ParticipationContextBounds:
    values = {"lull_minutes": 25, "burst_messages": 4, "burst_minutes": 15, **kwargs}
    return ParticipationContextBounds(
        window_minutes=120, max_messages=max_messages, context_mode=mode, **values
    )


def _ids(context: dict[str, object]) -> list[str]:
    return [str(row["event_id"]) for row in context["messages"]]  # type: ignore[index, union-attr]


def _arvid(archive, *, message_id: str, minutes_ago: float) -> None:
    archive.add_message(
        chat_id=CHAT, message_id=message_id, text="synthetic Arvid line",
        timestamp=NOW.timestamp() - minutes_ago * 60, sender_id="yeoman", direction="out",
    )


@pytest.mark.asyncio
async def test_window_mode_is_unchanged_and_reports_its_mode(tmp_path: Path) -> None:
    builder = await _builder(tmp_path)
    archive = builder._history_projector
    _record(archive, message_id="old", text="older topic", minutes_ago=90)
    _record(archive, message_id="new", text="new topic", minutes_ago=5)
    context = await _build_context(builder, _opportunity("new"))
    assert _ids(context) == ["old", "new"]
    assert context["context_mode"] == "window"
    assert context["conversation_id"] == ""
    assert context["conversation_header"] == ""
    assert context["conversation"] is None


@pytest.mark.asyncio
async def test_conversation_start_after_a_lull_drops_the_older_one(tmp_path: Path) -> None:
    builder = await _builder(tmp_path)
    archive = builder._history_projector
    _record(archive, message_id="a1", text="earlier topic", minutes_ago=70, sender="anna")
    _record(archive, message_id="a2", text="earlier reply", minutes_ago=68, sender="ben")
    _record(archive, message_id="b1", text="new topic after a lull", minutes_ago=2, sender="cara")
    window = await _build_context(builder, _opportunity("b1"), bounds=_bounds("window"))
    context = await _build_context(builder, _opportunity("b1"), bounds=_bounds("conversation"))
    assert _ids(window) == ["a1", "a2", "b1"]
    assert _ids(context) == ["b1"]
    assert context["context_mode"] == "conversation"
    assert str(context["conversation_id"]).startswith("conv:")
    assert context["truncated_messages"] == 2
    assert context["dropped_source_ids"] == ["a1", "a2"]
    assert context["dropped_source_count"] == 2


@pytest.mark.asyncio
async def test_two_conversations_in_the_window_keep_the_triggering_one(tmp_path: Path) -> None:
    builder = await _builder(tmp_path)
    archive = builder._history_projector
    for index, minutes in enumerate((110, 108, 105)):
        _record(archive, message_id=f"x{index}", text="first", minutes_ago=minutes)
    for index, minutes in enumerate((40, 38, 30)):
        _record(archive, message_id=f"y{index}", text="second", minutes_ago=minutes)
    # The trigger is the newest source; it belongs to the second conversation.
    context = await _build_context(builder, _opportunity("y2"), bounds=_bounds("conversation"))
    assert _ids(context) == ["y0", "y1", "y2"]
    # An opportunity on the older conversation keeps the older one.
    older = await _build_context(builder, _opportunity("x1"), bounds=_bounds("conversation"))
    assert _ids(older) == ["x0", "x1", "x2"]


@pytest.mark.asyncio
async def test_quote_to_an_older_conversation_is_shown_not_merged(tmp_path: Path) -> None:
    builder = await _builder(tmp_path)
    archive = builder._history_projector
    _record(archive, message_id="q0", text="old claim", minutes_ago=90, sender="anna")
    _record(archive, message_id="q1", text="unrelated", minutes_ago=3, sender="ben")
    _record(archive, message_id="q2", text="about that", minutes_ago=2, sender="cara",
            reply_to_message_id="q0")
    context = await _build_context(builder, _opportunity("q2"), bounds=_bounds("conversation"))
    assert _ids(context) == ["q1", "q2"]
    rendered = {row["event_id"]: row for row in context["messages"]}  # type: ignore[union-attr]
    assert rendered["q2"]["reply_to_message_id"] == "q0"
    assert context["conversation"]["outside_reply_count"] == 1  # type: ignore[index]


@pytest.mark.asyncio
async def test_required_sources_outside_the_conversation_stay(tmp_path: Path) -> None:
    builder = await _builder(tmp_path)
    archive = builder._history_projector
    _record(archive, message_id="early", text="earlier question", minutes_ago=80)
    _record(archive, message_id="noise", text="earlier noise", minutes_ago=79)
    _record(archive, message_id="late", text="current message", minutes_ago=1)
    context = await _build_context(
        builder, _opportunity("early", "late"), bounds=_bounds("conversation")
    )
    assert _ids(context) == ["early", "late"]
    assert context["dropped_source_ids"] == ["noise"]


@pytest.mark.asyncio
async def test_first_message_is_kept_when_the_conversation_is_over_the_cap(
    tmp_path: Path,
) -> None:
    builder = await _builder(tmp_path)
    archive = builder._history_projector
    for index in range(60):
        _record(archive, message_id=f"c{index:02d}", text=f"line {index}",
                minutes_ago=61 - index, sender=("anna", "ben", "cara")[index % 3])
    context = await _build_context(
        builder, _opportunity("c59"), bounds=_bounds("conversation", max_messages=40)
    )
    ids = _ids(context)
    assert len(ids) == 40
    assert ids[0] == "c00"
    assert ids[1:] == [f"c{index:02d}" for index in range(21, 60)]
    assert context["truncated_messages"] == 20
    assert context["conversation"]["message_count"] == 60  # type: ignore[index]


@pytest.mark.asyncio
async def test_header_is_one_line_rendered_before_the_messages(tmp_path: Path) -> None:
    builder = await _builder(tmp_path)
    archive = builder._history_projector
    _record(archive, message_id="h1", text="opening", minutes_ago=30, sender="anna")
    _arvid(archive, message_id="arvid-1", minutes_ago=20)
    _record(archive, message_id="h2", text="reply", minutes_ago=10, sender="ben")
    _record(archive, message_id="h3", text="more", minutes_ago=2, sender="cara")
    archive.append("reaction", chat_id=CHAT, timestamp=NOW.timestamp() - 60, emoji="😂",
                   removed=False, senderId="10002@s.whatsapp.net", targetMessageId="h1")
    context = await _build_context(builder, _opportunity("h3"), bounds=_bounds("conversation"))
    header = str(context["conversation_header"])
    assert "\n" not in header
    assert header.startswith("Conversation since ")
    assert "(28 min)" in header
    assert "4 people · 4 messages · 1 reaction (😂)" in header
    assert "Arvid: 1 message, answered" in header
    assert header.endswith("state: slowing")
    rendered = _JudgeContext.from_mapping(context).render()
    assert rendered.splitlines()[0] == header


@pytest.mark.asyncio
async def test_registry_thread_marks_the_conversation_addressed(tmp_path: Path) -> None:
    builder = await _builder(tmp_path)
    builder._thread_lookup = lambda native: ("thread-1", None) if native == "arvid-1" else None
    archive = builder._history_projector
    _record(archive, message_id="r1", text="hey Arvid", minutes_ago=5)
    _arvid(archive, message_id="arvid-1", minutes_ago=4)
    context = await _build_context(builder, _opportunity("r1"), bounds=_bounds("conversation"))
    assert context["conversation"]["arvid_addressed"] is True  # type: ignore[index]


@pytest.mark.asyncio
async def test_segmentation_error_falls_back_to_the_window_and_logs_once_per_hour(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    builder = await _builder(tmp_path)
    archive = builder._history_projector
    _record(archive, message_id="old", text="older topic", minutes_ago=90)
    _record(archive, message_id="new", text="new topic", minutes_ago=5)

    def broken(*args: object, **kwargs: object) -> list[object]:
        raise RuntimeError("synthetic segmentation failure")

    monkeypatch.setattr(participation_context, "segment", broken)
    lines: list[str] = []
    sink = logger.add(lambda message: lines.append(str(message)), level="WARNING")
    try:
        first = await _build_context(builder, _opportunity("new"), bounds=_bounds("conversation"))
        await _build_context(builder, _opportunity("new"), bounds=_bounds("conversation"))
        hour_later = await _build_context(
            builder, _opportunity("new"), bounds=_bounds("conversation"),
            now_ms=int(NOW.timestamp() * 1000) + 3_600_000,
        )
    finally:
        logger.remove(sink)
    assert _ids(first) == ["old", "new"]
    assert first["context_mode"] == "window"
    assert first["conversation_id"] == ""
    assert hour_later["context_mode"] == "window"
    fallbacks = [line for line in lines if "participation_conversation_fallback" in line]
    assert len(fallbacks) == 2
    assert "older topic" not in "".join(fallbacks)


@pytest.mark.asyncio
@pytest.mark.parametrize("seed", range(25))
async def test_conversation_view_is_a_subset_of_the_window(tmp_path: Path, seed: int) -> None:
    rng = random.Random(seed)
    builder = await _builder(tmp_path)
    archive = builder._history_projector
    minutes = sorted({round(rng.uniform(0.5, 150), 3) for _ in range(rng.randint(3, 50))})
    for index, minute in enumerate(minutes):
        if 119.9 <= minute <= 120.1:
            continue  # the window edge itself is covered by the window tests
        _record(archive, message_id=f"p{index:02d}", text=f"line {index}", minutes_ago=minute,
                sender=rng.choice(("anna", "ben", "cara")))
    in_window = {f"p{index:02d}" for index, minute in enumerate(minutes) if minute < 119.9}
    candidates = sorted(in_window)
    if not candidates:
        return
    required = tuple(rng.sample(candidates, k=min(len(candidates), rng.randint(1, 2))))
    bounds = {"max_messages": rng.randint(5, 15), "lull_minutes": rng.choice((5, 15, 25)),
              "burst_messages": 4, "burst_minutes": 15}
    window = await _build_context(
        builder, _opportunity(*required), bounds=_bounds("window", **bounds)
    )
    conversation = await _build_context(
        builder, _opportunity(*required), bounds=_bounds("conversation", **bounds)
    )
    window_ids, conversation_ids = _ids(window), _ids(conversation)
    assert set(conversation_ids) <= in_window
    assert len(conversation_ids) <= len(window_ids)
    assert set(required) <= set(conversation_ids)
    assert set(required) <= set(window_ids)
    assert (len(conversation_ids) + int(conversation["truncated_messages"])  # type: ignore[call-overload]
            == len(window_ids) + int(window["truncated_messages"]))  # type: ignore[call-overload]
    assert conversation["context_mode"] == "conversation"


def test_knowledge_log_line_names_the_context_mode_and_conversation() -> None:
    from yeoman_gateway.processing.participation_runtime import ParticipationRuntime

    lines: list[str] = []
    sink = logger.add(lambda message: lines.append(str(message)), level="INFO")
    try:
        ParticipationRuntime._record_knowledge_stage(
            {"context_mode": "conversation", "conversation_id": "conv:0123456789abcdef"}, "judge"
        )
        ParticipationRuntime._record_knowledge_stage({}, "judge")
    finally:
        logger.remove(sink)
    assert "context_mode=conversation conversation_id=conv:0123456789abcdef" in lines[0]
    assert "context_mode=window conversation_id=none" in lines[1]
