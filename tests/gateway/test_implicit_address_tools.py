"""A message promoted to an answer gets the tools of a real address, not none.

Before this, "Hey Arvid, mach ein Meme" in a mention-only group was answered with no
tools at all: the policy stage had evaluated it as unaddressed (no tools), and the
promotion only flipped ``should_respond``.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from yeoman_gateway.adapters.policy_engine import EnginePolicyAdapter
from yeoman_gateway.core.models import InboundEvent
from yeoman_gateway.core.pipeline import PipelineContext
from yeoman_gateway.pipeline.implicit_address import ImplicitBotAddressMiddleware
from yeoman_gateway.policy.engine import PolicyEngine
from yeoman_gateway.policy.schema import PolicyConfig
from yeoman_gateway.short_reply.decider import ShortReplyVerdict

from tests.gateway.test_short_reply_middleware import _reactor

GROUP = "group@g.us"
OWNER = "owner@s.whatsapp.net"
MEMBER = "user@s.whatsapp.net"
KNOWN = {"web_search", "make_meme", "cron", "exec"}


def _adapter(tmp_path: Path) -> EnginePolicyAdapter:
    policy = PolicyConfig.model_validate(
        {
            "owners": {"whatsapp": [OWNER]},
            "channels": {
                "whatsapp": {
                    "chats": {
                        GROUP: {
                            "whoCanTalk": {"mode": "everyone"},
                            "whenToReply": {"mode": "mention_only"},
                            "allowedTools": {
                                "mode": "allowlist",
                                "tools": ["web_search", "make_meme", "cron"],
                            },
                            "toolAccess": {"cron": {"mode": "owner_only"}},
                            "memes": True,
                        }
                    }
                }
            },
        }
    )
    engine = PolicyEngine(policy, workspace=tmp_path)
    return EnginePolicyAdapter(engine=engine, known_tools=set(KNOWN), reload_on_change=False)


def _event(content: str, **overrides: object) -> InboundEvent:
    payload: dict[str, object] = {
        "channel": "whatsapp",
        "chat_id": GROUP,
        "sender_id": MEMBER,
        "participant": MEMBER,
        "content": content,
        "message_id": "msg-1",
        "timestamp": datetime(2026, 10, 28, 6, 35, tzinfo=UTC),
        "is_group": True,
    }
    payload.update(overrides)
    return InboundEvent(**payload)  # type: ignore[arg-type]


async def _downstream(ctx: PipelineContext) -> None:
    ctx.reply = "downstream reached"


@pytest.mark.asyncio
async def test_name_only_request_gets_the_tools_of_an_at_mention(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    event = _event("Hey Arvid kannst du ein Meme erstellen über die Themen hier?")
    unaddressed = adapter.evaluate(event)
    assert unaddressed.should_respond is False and unaddressed.allowed_tools == frozenset()

    ctx = PipelineContext(event=event, decision=unaddressed)
    await ImplicitBotAddressMiddleware(policy=adapter)(ctx, _downstream)

    assert ctx.reply == "downstream reached"
    assert ctx.decision is not None and ctx.decision.should_respond is True
    assert ctx.decision.reason == "when_to_reply:implicit_plain_name_request"
    mentioned = adapter.evaluate(replace(event, mentioned_bot=True))
    assert ctx.decision.allowed_tools == mentioned.allowed_tools
    assert {"make_meme", "web_search"} <= ctx.decision.allowed_tools
    # Per-sender tool access still applies: cron stays owner-only.
    assert "cron" not in ctx.decision.allowed_tools


@pytest.mark.asyncio
async def test_short_reply_answer_gets_the_tools_of_a_reply_to_the_bot(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    event = _event(
        "Es gab nichts dergleichen, es war buy and hold",
        reply_to_bot=True,
        reply_to_message_id="bot-1",
        reply_to_text="Starker Trade.",
        raw_metadata={"thread_id": "th_1", "turn_id": "tu_1"},
    )
    held_back = replace(adapter.evaluate(event), should_respond=False, allowed_tools=frozenset())
    ctx = PipelineContext(event=event, decision=held_back)
    reactor = _reactor(ShortReplyVerdict(action="answer"))

    await ImplicitBotAddressMiddleware(short_reply_reactor=reactor, policy=adapter)(
        ctx, _downstream
    )

    assert ctx.reply == "downstream reached"
    assert ctx.decision is not None and ctx.decision.should_respond is True
    assert ctx.decision.reason == "when_to_reply:short_reply_answer"
    assert ctx.decision.allowed_tools == adapter.evaluate(event).allowed_tools
    assert "make_meme" in ctx.decision.allowed_tools


@pytest.mark.asyncio
async def test_unaddressed_message_still_gets_no_tools(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    event = _event("ich geh jetzt einkaufen")
    ctx = PipelineContext(event=event, decision=adapter.evaluate(event))

    await ImplicitBotAddressMiddleware(policy=adapter)(ctx, _downstream)

    assert ctx.decision is not None
    assert ctx.decision.should_respond is False
    assert ctx.decision.allowed_tools == frozenset()


@pytest.mark.asyncio
async def test_failed_reevaluation_keeps_the_answer(tmp_path: Path) -> None:
    class _Broken:
        def evaluate(self, event):
            raise RuntimeError("policy unavailable")

    adapter = _adapter(tmp_path)
    event = _event("Arvid kannst du Nokia kurz checken")
    ctx = PipelineContext(event=event, decision=adapter.evaluate(event))

    await ImplicitBotAddressMiddleware(policy=_Broken())(ctx, _downstream)  # type: ignore[arg-type]

    assert ctx.decision is not None and ctx.decision.should_respond is True
    assert ctx.decision.allowed_tools == frozenset()
