from __future__ import annotations

from datetime import UTC, datetime

import pytest
from loguru import logger
from yeoman_gateway.core.intents import SendReactionIntent
from yeoman_gateway.core.models import InboundEvent, PolicyDecision
from yeoman_gateway.core.pipeline import PipelineContext
from yeoman_gateway.pipeline.implicit_address import ImplicitBotAddressMiddleware
from yeoman_gateway.processing.models import RecentReaction, ShortReplyClaim
from yeoman_gateway.short_reply.decider import ShortReplyVerdict
from yeoman_gateway.short_reply.reactor import ShortReplyReactor
from yeoman_gateway.short_reply.variety import cooldown_active
from yeoman_shared.config.schema import ProcessingShortReplyConfig
from yeoman_shared.reactions import MODEL_ORIGIN

NOW = 10_000_000


class _Decider:
    def __init__(self, verdict: ShortReplyVerdict) -> None:
        self.verdict = verdict
        self.calls = []

    async def decide(self, request):
        self.calls.append(request)
        return self.verdict


class _History:
    """Same contract as ProcessingStore: durable claim, burst guard, confirmed history."""

    def __init__(self, rows=()) -> None:
        self.rows = list(rows)
        self.claims: set[tuple[str, str, str]] = set()

    def claim_short_reply(self, *, channel, chat_id, message_id, now_ms,
                          count, window_seconds, cooldown_seconds, mode="live"):
        key = (channel, chat_id, message_id)
        if key in self.claims:
            return ShortReplyClaim("duplicate")
        self.claims.add(key)
        if cooldown_active(
            [row.created_ms for row in self.rows], now_ms=now_ms, count=count,
            window_seconds=window_seconds, cooldown_seconds=cooldown_seconds,
        ):
            return ShortReplyClaim("cooldown")
        return ShortReplyClaim("claimed")

    def complete_short_reply(self, **kwargs):
        return None

    def recent_reactions(self, *, channel, chat_id, since_ms, limit):
        return tuple(self.rows)[:limit]


def _reactor(verdict=None, *, mode="live", rows=(), **settings) -> ShortReplyReactor:
    # mode_for is fail-closed without chats; bootstrap injects processing.chats ∩ shortReply.chats.
    return ShortReplyReactor(
        decider=_Decider(verdict or ShortReplyVerdict(action="react", emojis=("😎", "😄"))),
        history=_History(rows),
        settings=ProcessingShortReplyConfig.model_validate(
            {"mode": mode, "chats": ["whatsapp:group@g.us"], **settings}
        ),
        clock=lambda: NOW,
    )


def _ctx(content="Jepp, hatte Glück 😊", *, bot_text="Starker Trade.", **extra) -> PipelineContext:
    metadata = dict(extra.pop("raw_metadata", {}))
    event = InboundEvent(
        channel="whatsapp",
        chat_id="group@g.us",
        sender_id="user@s.whatsapp.net",
        content=content,
        message_id=extra.pop("message_id", "msg-1"),
        timestamp=datetime(2026, 9, 22, 20, 6, tzinfo=UTC),
        participant="user@s.whatsapp.net",
        is_group=True,
        reply_to_bot=True,
        reply_to_message_id="bot-1",
        reply_to_text=bot_text,
        raw_metadata={"thread_id": "th_1", "turn_id": "tu_1", **metadata},
        **extra,
    )
    decision = PolicyDecision(
        accept_message=True,
        should_respond=True,
        allowed_tools=frozenset(),
        reason="when_to_reply:mention_only_group",
        when_to_reply_mode="mention_only",
    )
    return PipelineContext(event=event, decision=decision)


async def _downstream(ctx: PipelineContext) -> None:
    ctx.reply = "downstream reached"


def _reactions(ctx: PipelineContext) -> list[SendReactionIntent]:
    return [intent for intent in ctx.intents if isinstance(intent, SendReactionIntent)]


async def test_live_short_reply_gets_a_varied_model_reaction() -> None:
    reactor = _reactor(rows=[RecentReaction("😎", NOW - 60_000)])
    ctx = _ctx()
    await ImplicitBotAddressMiddleware(short_reply_reactor=reactor)(ctx, _downstream)

    reactions = _reactions(ctx)
    assert ctx.halted is True and ctx.reply is None
    assert [(r.emoji, r.origin, r.reason) for r in reactions] == [
        ("😄", MODEL_ORIGIN, "short_reply")
    ]


@pytest.mark.parametrize("content", ["lol that was pure luck", "ありがとう！", "شكرا", "😂😂"])
async def test_live_path_is_language_neutral(content: str) -> None:
    reactor = _reactor()
    ctx = _ctx(content)
    await ImplicitBotAddressMiddleware(short_reply_reactor=reactor)(ctx, _downstream)
    assert len(reactor._decider.calls) == 1
    assert len(_reactions(ctx)) == 1


async def test_live_answer_verdict_reaches_the_answer_path() -> None:
    reactor = _reactor(ShortReplyVerdict(action="answer"))
    ctx = _ctx("Es gab nichts dergleichen, es war buy and hold")
    await ImplicitBotAddressMiddleware(short_reply_reactor=reactor)(ctx, _downstream)
    assert ctx.reply == "downstream reached" and _reactions(ctx) == []
    state = ctx.event.raw_metadata["conversation_state"]
    assert state["preferred_action"] == "answer"


async def test_live_none_verdict_stays_silent() -> None:
    ctx = _ctx()
    reactor = _reactor(ShortReplyVerdict(action="none"))
    await ImplicitBotAddressMiddleware(short_reply_reactor=reactor)(ctx, _downstream)
    assert ctx.halted is True and _reactions(ctx) == [] and ctx.reply is None


async def test_live_error_uses_the_neutral_fallback() -> None:
    ctx = _ctx()
    reactor = _reactor(ShortReplyVerdict(action="error", error="timeout"))
    await ImplicitBotAddressMiddleware(short_reply_reactor=reactor)(ctx, _downstream)
    assert [(r.emoji, r.reason) for r in _reactions(ctx)] == [("👍", "short_reply_fallback")]


@pytest.mark.parametrize("bot_text", ["Wie lief der Trade?", "How did it go?", "どうでしたか？"])
async def test_a_reply_to_arvids_question_is_answered_without_a_model_call(bot_text: str) -> None:
    reactor = _reactor()
    ctx = _ctx("ok", bot_text=bot_text)
    await ImplicitBotAddressMiddleware(short_reply_reactor=reactor)(ctx, _downstream)
    assert reactor._decider.calls == []
    assert ctx.reply == "downstream reached" and _reactions(ctx) == []


async def test_bot_question_bypass_is_logged_without_message_text() -> None:
    records = []
    sink = logger.add(lambda message: records.append(message.record["message"]), level="INFO")
    try:
        reactor = _reactor()
        await ImplicitBotAddressMiddleware(short_reply_reactor=reactor)(
            _ctx("ok", bot_text="How did it go?"), _downstream
        )
    finally:
        logger.remove(sink)
    assert any("mode=live" in line and "reason=bot_asked" in line for line in records)
    assert all("How did it go" not in line for line in records)


async def test_cooldown_uses_the_bait_cooldown_state() -> None:
    rows = [RecentReaction("👍", NOW - 10_000), RecentReaction("👀", NOW - 50_000)]
    ctx = _ctx()
    await ImplicitBotAddressMiddleware(short_reply_reactor=_reactor(rows=rows))(ctx, _downstream)
    assert ctx.event.raw_metadata["conversation_state"]["address_mode"] == "bait_cooldown"


async def test_a_bare_image_reply_is_answered_not_reacted_to() -> None:
    reactor = _reactor()
    ctx = _ctx("[Image]", raw_metadata={"media_kind": "image"})
    await ImplicitBotAddressMiddleware(short_reply_reactor=reactor)(ctx, _downstream)
    assert reactor._decider.calls == []
    assert ctx.reply == "downstream reached"


async def test_repair_feedback_keeps_its_own_path() -> None:
    reactor = _reactor()
    ctx = _ctx("falsch")
    await ImplicitBotAddressMiddleware(short_reply_reactor=reactor)(ctx, _downstream)
    assert reactor._decider.calls == []
    assert ctx.event.raw_metadata["conversation_state"]["address_mode"] == "repair_feedback"


async def test_processing_decisions_win_over_the_short_reply_path() -> None:
    reactor = _reactor()
    ctx = _ctx(raw_metadata={"processing_reacted": True})
    await ImplicitBotAddressMiddleware(short_reply_reactor=reactor)(ctx, _downstream)
    assert reactor._decider.calls == [] and _reactions(ctx) == []


async def test_shadow_keeps_the_legacy_reaction_and_decides_in_the_background() -> None:
    reactor = _reactor(mode="shadow")
    ctx = _ctx("Jepp, hatte Glück")
    await ImplicitBotAddressMiddleware(short_reply_reactor=reactor)(ctx, _downstream)
    await reactor.drain()
    assert [(r.emoji, r.reason) for r in _reactions(ctx)] == [("👀", "low_content_reply")]
    assert len(reactor._decider.calls) == 1


async def test_off_and_unlisted_chats_behave_exactly_as_before() -> None:
    for reactor in (_reactor(mode="off"), _reactor(chats=["whatsapp:other@g.us"])):
        ctx = _ctx("Jepp, hatte Glück")
        await ImplicitBotAddressMiddleware(short_reply_reactor=reactor)(ctx, _downstream)
        assert reactor._decider.calls == []
        assert [r.emoji for r in _reactions(ctx)] == ["👀"]


async def test_every_in_scope_reply_is_logged_once_with_its_batch_ids() -> None:
    records = []
    sink = logger.add(lambda message: records.append(message.record["message"]), level="INFO")
    try:
        reactor = _reactor(mode="shadow")
        ctx = _ctx("x" * 81, raw_metadata={"source_event_ids": ["m-a", "msg-1"]})
        await ImplicitBotAddressMiddleware(short_reply_reactor=reactor)(ctx, _downstream)
    finally:
        logger.remove(sink)
    lines = [line for line in records if line.startswith("reaction_decision")]
    assert len(lines) == 1
    assert "reason=not_candidate" in lines[0] and "source_ids=m-a,msg-1" in lines[0]
    assert "xxxx" not in lines[0]


async def test_a_duplicate_delivery_reacts_only_once() -> None:
    reactor = _reactor()
    middleware = ImplicitBotAddressMiddleware(short_reply_reactor=reactor)
    first, second = _ctx(), _ctx()
    await middleware(first, _downstream)
    await middleware(second, _downstream)
    assert len(_reactions(first)) == 1 and _reactions(second) == []
    assert len(reactor._decider.calls) == 1
