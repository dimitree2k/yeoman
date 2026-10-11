"""WhatsApp mention candidates never guess names without a live resolver."""

import pytest
from yeoman_gateway.core.intents import SendOutboundIntent
from yeoman_gateway.core.models import InboundEvent
from yeoman_gateway.core.pipeline import PipelineContext
from yeoman_gateway.pipeline.outbound import OutboundMiddleware


def _make_event(**overrides: object) -> InboundEvent:
    defaults = {
        "channel": "whatsapp",
        "chat_id": "group@g.us",
        "sender_id": "sender@s.whatsapp.net",
        "content": "hello",
        "message_id": "msg-001",
        "is_group": True,
    }
    defaults.update(overrides)
    return InboundEvent(**defaults)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("metadata", "reply", "expected"),
    [
        ({}, "Hey @Alex check this out", None),
        ({"mentioned_jids": ["491521234567@s.whatsapp.net"]}, "Hey there", ["491521234567@s.whatsapp.net"]),
    ],
)
async def test_without_history_knowledge_only_native_candidates_survive(metadata, reply, expected):
    middleware = OutboundMiddleware()
    ctx = PipelineContext(event=_make_event(raw_metadata=metadata))
    ctx.reply = reply
    ctx.decision = type("D", (), {"when_to_reply_mode": "all", "voice_output_mode": "text"})()

    async def next_fn(_ctx: PipelineContext) -> None:
        return None

    await middleware(ctx, next_fn)
    outbound = [item.event for item in ctx.intents if isinstance(item, SendOutboundIntent)]
    assert len(outbound) == 1
    assert outbound[0].metadata.get("mention_candidates") == expected
