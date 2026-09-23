from __future__ import annotations

import asyncio

import pytest
from loguru import logger
from yeoman_gateway.processing.models import RecentReaction, ShortReplyClaim
from yeoman_gateway.short_reply.decider import ShortReplyVerdict
from yeoman_gateway.short_reply.reactor import ReactorRequest, ShortReplyReactor
from yeoman_gateway.short_reply.signals import compute_signals
from yeoman_gateway.short_reply.variety import cooldown_active
from yeoman_shared.config.schema import ProcessingShortReplyConfig

NOW = 10_000_000


class _Decider:
    def __init__(self, verdict: ShortReplyVerdict, *, delay: float = 0.0) -> None:
        self.verdict = verdict
        self.delay = delay
        self.calls = []

    async def decide(self, request):
        self.calls.append(request)
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.verdict


class _History:
    def __init__(self, rows=(), *, broken: bool = False) -> None:
        self.rows = list(rows)
        self.broken = broken
        self.claims = set()

    def claim_short_reply(
        self,
        *,
        channel,
        chat_id,
        message_id,
        now_ms,
        count,
        window_seconds,
        cooldown_seconds,
        mode="live",
    ):
        key = (channel, chat_id, message_id)
        if key in self.claims:
            return ShortReplyClaim("duplicate")
        self.claims.add(key)
        if cooldown_active(
            [row.created_ms for row in self.rows],
            now_ms=now_ms,
            count=count,
            window_seconds=window_seconds,
            cooldown_seconds=cooldown_seconds,
        ):
            return ShortReplyClaim("cooldown")
        return ShortReplyClaim("claimed")

    def complete_short_reply(self, **kwargs):
        self.completed = getattr(self, "completed", []) + [kwargs]

    def recent_reactions(self, *, channel, chat_id, since_ms, limit):
        if self.broken:
            raise RuntimeError("db locked")
        return tuple(row for row in self.rows if row.created_ms >= since_ms)[:limit]


def _request(text: str = "Jepp, hatte Glück 😊", message_id: str = "m1") -> ReactorRequest:
    return ReactorRequest(
        channel="whatsapp",
        chat_id="g@g.us",
        message_id=message_id,
        thread_id="th_1",
        turn_id="tu_1",
        signals=compute_signals(
            content=text, reply_to_bot=True, reply_to_text="Starker Trade.", metadata={}
        ),
        bot_text="Starker Trade.",
    )


def _reactor(verdict, *, rows=(), broken=False, delay=0.0, **settings) -> ShortReplyReactor:
    return ShortReplyReactor(
        decider=_Decider(verdict, delay=delay),
        history=_History(rows, broken=broken),
        settings=ProcessingShortReplyConfig.model_validate({"mode": "live", **settings}),
        clock=lambda: NOW,
    )


REACT = ShortReplyVerdict(action="react", emojis=("😎", "😄", "🤙"))


async def test_a_reaction_avoids_the_emoji_used_just_before() -> None:
    reactor = _reactor(REACT, rows=[RecentReaction("😎", NOW - 60_000)])
    outcome = await reactor.decide(_request())
    assert (outcome.kind, outcome.emoji, outcome.source) == ("react", "😄", "model")
    assert reactor._decider.calls[0].recent_emojis == ("😎",)


async def test_a_triple_blocked_model_reaction_stays_silent() -> None:
    verdict = ShortReplyVerdict(action="react", emojis=("😎",))
    rows = [RecentReaction("😎", NOW - 900_000), RecentReaction("😎", NOW - 950_000)]
    outcome = await _reactor(verdict, rows=rows).decide(_request())
    assert (outcome.kind, outcome.emoji, outcome.source) == ("silence", None, "none")


async def test_a_burst_in_the_durable_history_silences_without_a_model_call() -> None:
    rows = [RecentReaction("👍", NOW - 10_000), RecentReaction("👀", NOW - 50_000)]
    first = _reactor(REACT, rows=rows)
    assert (await first.decide(_request())).reason == "cooldown"
    assert first._decider.calls == []
    # A restarted gateway sees the same history and stays quiet as well.
    second = _reactor(REACT, rows=rows)
    assert (await second.decide(_request())).reason == "cooldown"


async def test_unreadable_history_is_treated_as_cooldown() -> None:
    reactor = _reactor(REACT, broken=True)
    outcome = await reactor.decide(_request())
    assert outcome.kind == "silence" and outcome.reason == "history_unavailable"


async def test_answer_is_escalated_only_when_allowed() -> None:
    answer = ShortReplyVerdict(action="answer", emojis=("🤙",))
    assert (await _reactor(answer).decide(_request())).kind == "answer"
    capped = await _reactor(answer, allowAnswer=False).decide(_request())
    assert (capped.kind, capped.emoji) == ("react", "🤙")


async def test_none_is_silence() -> None:
    outcome = await _reactor(ShortReplyVerdict(action="none")).decide(_request())
    assert (outcome.kind, outcome.reason) == ("silence", "decided_none")


async def test_errors_rotate_through_the_neutral_fallback() -> None:
    error = ShortReplyVerdict(action="error", error="timeout")
    rows = [RecentReaction("👍", NOW - 900_000)]
    outcome = await _reactor(error, rows=rows).decide(_request())
    assert (outcome.kind, outcome.emoji, outcome.source) == ("react", "🤙", "fallback")
    assert outcome.reason == "error:timeout"


async def test_errors_can_be_configured_to_stay_silent() -> None:
    error = ShortReplyVerdict(action="error", error="invalid_json")
    outcome = await _reactor(error, fallback="silence").decide(_request())
    assert (outcome.kind, outcome.reason) == ("silence", "fallback_silence")


async def test_a_message_is_decided_once() -> None:
    reactor = _reactor(REACT)
    assert (await reactor.decide(_request())).kind == "react"
    again = await reactor.decide(_request())
    assert (again.kind, again.reason) == ("silence", "duplicate")
    assert len(reactor._decider.calls) == 1


async def test_shadow_runs_in_the_background_and_is_bounded() -> None:
    reactor = ShortReplyReactor(
        decider=_Decider(REACT, delay=0.05),
        history=_History(),
        settings=ProcessingShortReplyConfig.model_validate({"mode": "shadow"}),
        clock=lambda: NOW,
        max_shadow_tasks=1,
    )
    assert reactor.shadow(_request(message_id="m1")) is True
    assert reactor.shadow(_request(message_id="m2")) is False
    await reactor.drain()
    assert len(reactor._decider.calls) == 1


async def test_shadow_task_cap_never_exceeds_four() -> None:
    reactor = ShortReplyReactor(
        decider=_Decider(REACT, delay=0.05),
        history=_History(),
        settings=ProcessingShortReplyConfig.model_validate({"mode": "shadow"}),
        clock=lambda: NOW,
        max_shadow_tasks=5,
    )
    accepted = [reactor.shadow(_request(message_id=f"m{i}")) for i in range(5)]
    await reactor.drain()
    assert sum(accepted) == 4
    assert len(reactor._decider.calls) == 4


async def test_unreadable_history_logs_error_code_without_message_text() -> None:
    records: list[str] = []
    sink = logger.add(lambda message: records.append(message.record["message"]), level="INFO")
    try:
        await _reactor(REACT, broken=True).decide(_request("privater Inhalt 😊"))
    finally:
        logger.remove(sink)
    lines = [line for line in records if line.startswith("reaction_decision")]
    assert len(lines) == 1
    assert "error=history_unavailable" in lines[0]
    assert "privater" not in lines[0] and "db locked" not in lines[0]


async def test_the_decision_log_has_numbers_but_no_text() -> None:
    records: list[str] = []
    sink = logger.add(lambda message: records.append(message.record["message"]), level="INFO")
    try:
        await _reactor(REACT).decide(_request("geheimer Inhalt 😊"))
    finally:
        logger.remove(sink)
    lines = [line for line in records if line.startswith("reaction_decision")]
    assert len(lines) == 1
    assert "chosen=😎" in lines[0] and "source=model" in lines[0]
    assert "geheimer" not in lines[0]


async def test_unexpected_decider_errors_use_the_configured_fallback() -> None:
    reactor = _reactor(REACT)

    class _BrokenDecider:
        async def decide(self, request):
            raise RuntimeError("provider failed")

    reactor._decider = _BrokenDecider()
    outcome = await reactor.decide(_request())
    assert (outcome.kind, outcome.emoji, outcome.source) == ("react", "👍", "fallback")
    assert outcome.reason == "error:provider_error"


async def test_cancellation_from_the_decider_propagates() -> None:
    reactor = _reactor(REACT)

    class _CancelledDecider:
        async def decide(self, request):
            raise asyncio.CancelledError

    reactor._decider = _CancelledDecider()
    with pytest.raises(asyncio.CancelledError):
        await reactor.decide(_request())
    assert reactor._history.completed == [
        {
            "channel": "whatsapp",
            "chat_id": "g@g.us",
            "message_id": "m1",
            "outcome": "silence",
            "emoji": None,
            "now_ms": NOW,
        }
    ]


async def test_cancellation_still_propagates_if_claim_cleanup_fails() -> None:
    reactor = _reactor(REACT)

    class _CancelledDecider:
        async def decide(self, request):
            raise asyncio.CancelledError

    class _FailingCleanupHistory(_History):
        def complete_short_reply(self, **kwargs):
            self.cleanup_attempt = kwargs
            raise RuntimeError("cleanup failed")

    history = _FailingCleanupHistory()
    reactor._history = history
    reactor._decider = _CancelledDecider()
    with pytest.raises(asyncio.CancelledError):
        await reactor.decide(_request())
    assert history.cleanup_attempt["outcome"] == "silence"
    assert history.cleanup_attempt["emoji"] is None
