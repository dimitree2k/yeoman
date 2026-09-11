from __future__ import annotations

from datetime import UTC, datetime

import pytest
from yeoman_gateway.core.models import InboundEvent, PolicyDecision
from yeoman_gateway.core.pipeline import PipelineContext
from yeoman_gateway.pipeline.responder import ResponderMiddleware


class _Responder:
    def __init__(self) -> None:
        self.calls = 0

    async def generate_reply(self, event, decision) -> str:
        self.calls += 1
        return "reply"


def _decision() -> PolicyDecision:
    return PolicyDecision(
        accept_message=True,
        should_respond=True,
        allowed_tools=frozenset(),
        reason="test",
    )


@pytest.mark.asyncio
async def test_failed_reply_admission_starts_no_generation_or_typing() -> None:
    responder = _Responder()
    typing: list[bool] = []
    ctx = PipelineContext(
        event=InboundEvent(
            channel="whatsapp",
            chat_id="group@g.us",
            sender_id="sender@s.whatsapp.net",
            content="Arvid, bitte prüf das",
            message_id="message-1",
            timestamp=datetime.now(UTC),
            is_group=True,
        ),
        decision=_decision(),
    )

    async def _next(_ctx: PipelineContext) -> None:
        raise AssertionError("failed admission must halt before downstream layers")

    async def _typing(_channel: str, _chat_id: str, enabled: bool) -> None:
        typing.append(enabled)

    await ResponderMiddleware(
        responder=responder,
        typing_notifier=_typing,
        reply_admission=lambda _event: False,
    )(ctx, _next)

    assert ctx.halted is True
    assert responder.calls == 0
    assert typing == []
