"""Tests for the deterministic native-forward command middleware."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from yeoman_gateway.core.intents import SendOutboundIntent
from yeoman_gateway.core.models import InboundEvent, PolicyDecision
from yeoman_gateway.core.pipeline import PipelineContext
from yeoman_gateway.pipeline.forward import ForwardCommandMiddleware


def _event(**overrides: object) -> InboundEvent:
    payload: dict[str, object] = {
        "channel": "whatsapp",
        "chat_id": "molty@g.us",
        "sender_id": "owner@s.whatsapp.net",
        "content": "forward",
        "message_id": "command-1",
        "timestamp": datetime(2026, 9, 22, 20, 8, tzinfo=UTC),
        "is_group": True,
    }
    payload.update(overrides)
    return InboundEvent(**payload)  # type: ignore[arg-type]


def _decision(**overrides: object) -> PolicyDecision:
    payload: dict[str, object] = {
        "accept_message": True,
        "should_respond": True,
        "allowed_tools": frozenset({"forward_message"}),
        "reason": "owner",
        "is_owner": True,
    }
    payload.update(overrides)
    return PolicyDecision(**payload)  # type: ignore[arg-type]


async def _tracking_next(ctx: PipelineContext) -> None:
    ctx.reply = "downstream reached"


def _forward_intent(ctx: PipelineContext) -> SendOutboundIntent:
    assert len(ctx.intents) == 1
    intent = ctx.intents[0]
    assert isinstance(intent, SendOutboundIntent)
    return intent


@pytest.mark.asyncio
async def test_owner_forwards_quoted_message_to_current_chat() -> None:
    lookups: list[tuple[str, str]] = []

    async def lookup(chat_id: str, message_id: str) -> dict[str, object]:
        lookups.append((chat_id, message_id))
        return {"status": "found"}

    ctx = PipelineContext(
        event=_event(reply_to_message_id="source-1"),
        decision=_decision(),
    )

    await ForwardCommandMiddleware(source_lookup=lookup)(ctx, _tracking_next)

    intent = _forward_intent(ctx)
    assert ctx.halted is True
    assert ctx.reply is None
    assert lookups == [("molty@g.us", "source-1")]
    assert intent.event.chat_id == "molty@g.us"
    assert intent.event.content == ""
    assert intent.event.metadata["forward_message"] == {
        "source_chat_id": "molty@g.us",
        "source_message_id": "source-1",
    }


@pytest.mark.asyncio
async def test_owner_forwards_to_resolved_target() -> None:
    resolved: list[str] = []

    async def lookup(chat_id: str, message_id: str) -> dict[str, object]:
        assert (chat_id, message_id) == ("molty@g.us", "source-1")
        return {"status": "found"}

    def resolve(reference: str) -> tuple[str | None, str | None]:
        resolved.append(reference)
        return "ente@g.us", None

    ctx = PipelineContext(
        event=_event(content="/forward Ente", reply_to_message_id="source-1"),
        decision=_decision(),
    )

    await ForwardCommandMiddleware(target_resolver=resolve, source_lookup=lookup)(
        ctx, _tracking_next
    )

    assert _forward_intent(ctx).event.chat_id == "ente@g.us"
    assert resolved == ["Ente"]


@pytest.mark.asyncio
async def test_forward_requires_a_quoted_source() -> None:
    async def lookup(chat_id: str, message_id: str) -> dict[str, object]:
        raise AssertionError("source lookup must not run without a quote")

    ctx = PipelineContext(event=_event(), decision=_decision())

    await ForwardCommandMiddleware(source_lookup=lookup)(ctx, _tracking_next)

    intent = _forward_intent(ctx)
    assert ctx.halted is True
    assert intent.event.chat_id == "molty@g.us"
    assert intent.event.content
    assert "forward_message" not in intent.event.metadata


@pytest.mark.asyncio
async def test_unavailable_source_stops_without_forward_intent() -> None:
    async def lookup(chat_id: str, message_id: str) -> dict[str, object]:
        return {"status": "absent"}

    ctx = PipelineContext(
        event=_event(content="forward Ente", reply_to_message_id="source-1"),
        decision=_decision(),
    )

    await ForwardCommandMiddleware(
        target_resolver=lambda reference: ("ente@g.us", None),
        source_lookup=lookup,
    )(ctx, _tracking_next)

    intent = _forward_intent(ctx)
    assert ctx.halted is True
    assert "forward_message" not in intent.event.metadata


@pytest.mark.asyncio
async def test_unresolved_target_stops_without_forward_intent() -> None:
    async def lookup(chat_id: str, message_id: str) -> dict[str, object]:
        return {"status": "found"}

    ctx = PipelineContext(
        event=_event(content="forward Unknown", reply_to_message_id="source-1"),
        decision=_decision(),
    )

    await ForwardCommandMiddleware(
        target_resolver=lambda reference: (None, "unknown target"),
        source_lookup=lookup,
    )(ctx, _tracking_next)

    intent = _forward_intent(ctx)
    assert ctx.halted is True
    assert intent.event.chat_id == "molty@g.us"
    assert "forward_message" not in intent.event.metadata


@pytest.mark.asyncio
async def test_non_owner_is_silent_and_callbacks_are_not_called() -> None:
    calls: list[str] = []

    async def lookup(chat_id: str, message_id: str) -> dict[str, object]:
        calls.append("lookup")
        return {"status": "found"}

    def resolve(reference: str) -> tuple[str | None, str | None]:
        calls.append("resolve")
        return "ente@g.us", None

    ctx = PipelineContext(
        event=_event(content="forward Ente", reply_to_message_id="source-1"),
        decision=_decision(is_owner=False),
    )

    await ForwardCommandMiddleware(target_resolver=resolve, source_lookup=lookup)(
        ctx, _tracking_next
    )

    assert ctx.halted is True
    assert ctx.intents == []
    assert ctx.reply is None
    assert calls == []


@pytest.mark.asyncio
async def test_normal_text_passes_through() -> None:
    ctx = PipelineContext(event=_event(content="forwarding is useful"), decision=_decision())

    await ForwardCommandMiddleware(source_lookup=None)(ctx, _tracking_next)

    assert ctx.halted is False
    assert ctx.reply == "downstream reached"
    assert ctx.intents == []
