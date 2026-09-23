"""Short replies to the bot: one decision, a varied face, or silence (spec 2026-09-22).

Order of work: one atomic durable claim and burst guard (before any model call),
confirmed history read, decider, then variety or the configured fallback. ``shadow`` runs
the same decision in the background and only logs it; ``live`` returns it for middleware.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from loguru import logger

from yeoman_gateway.short_reply.decider import ShortReplyInput, ShortReplyVerdict
from yeoman_gateway.short_reply.signals import ShortReplySignals
from yeoman_gateway.short_reply.variety import choose_varied


@dataclass(frozen=True, slots=True)
class ReactorRequest:
    channel: str
    chat_id: str
    message_id: str
    thread_id: str
    turn_id: str
    signals: ShortReplySignals
    bot_text: str
    #: Every provider id a debounced batch carries; logged so shadow coverage reconciles.
    source_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ReactorOutcome:
    kind: Literal["react", "answer", "silence"]
    emoji: str | None = None
    source: Literal["model", "fallback", "none"] = "none"
    reason: str = ""


def _now_ms() -> int:
    return int(time.time() * 1000)


class ShortReplyReactor:
    def __init__(
        self,
        *,
        decider: Any,
        history: Any,
        settings: Any,
        clock: Callable[[], int] | None = None,
        max_shadow_tasks: int = 4,
    ) -> None:
        self._decider = decider
        self._history = history
        self._settings = settings
        self._clock = clock or _now_ms
        self._max_shadow_tasks = min(4, max(1, int(max_shadow_tasks)))
        self._shadow_tasks: set[asyncio.Task[None]] = set()
        self.shadow_dropped = 0

    @property
    def max_chars(self) -> int:
        return int(self._settings.max_chars)

    def mode_for(self, channel: str, chat_id: str) -> str:
        return str(self._settings.mode_for(channel, chat_id))

    async def decide(self, request: ReactorRequest, *, mode: str = "live") -> ReactorOutcome:
        started = time.monotonic()
        settings = self._settings
        now = int(self._clock())
        rate = settings.rate_limit
        try:
            claim = self._history.claim_short_reply(
                channel=request.channel,
                chat_id=request.chat_id,
                message_id=request.message_id,
                now_ms=now,
                count=rate.count,
                window_seconds=rate.window_seconds,
                cooldown_seconds=rate.cooldown_seconds,
                mode=mode,
            )
        except Exception:  # noqa: BLE001 - claim failure must not call the model
            return self._log(
                request, mode, ReactorOutcome("silence", reason="history_unavailable")
            )
        if claim.status == "duplicate":
            return self._log(request, mode, ReactorOutcome("silence", reason="duplicate"))
        if claim.status == "cooldown":
            return self._log(request, mode, ReactorOutcome("silence", reason="cooldown"))

        window_ms = max(
            settings.variety_window_minutes * 60_000,
            (rate.window_seconds + rate.cooldown_seconds) * 1000,
        )
        try:
            history = tuple(
                self._history.recent_reactions(
                    channel=request.channel,
                    chat_id=request.chat_id,
                    since_ms=now - window_ms,
                    limit=max(settings.variety_history, rate.count, 1),
                )
            )
        except Exception:  # noqa: BLE001 - unknown history must not become spam
            try:
                self._history.complete_short_reply(
                    channel=request.channel,
                    chat_id=request.chat_id,
                    message_id=request.message_id,
                    outcome="silence",
                    emoji=None,
                    now_ms=now,
                )
            except Exception:  # noqa: BLE001 - the original claim remains fail-closed
                pass
            return self._log(
                request, mode, ReactorOutcome("silence", reason="history_unavailable")
            )

        variety_since = now - settings.variety_window_minutes * 60_000
        recent = tuple(
            item.emoji for item in history if item.created_ms >= variety_since
        )[: settings.variety_history]
        try:
            verdict: ShortReplyVerdict = await self._decider.decide(
                ShortReplyInput(
                    text=request.signals.text,
                    bot_text=request.bot_text,
                    media_text=request.signals.media_text,
                    recent_emojis=recent,
                )
            )
        except asyncio.CancelledError:
            try:
                self._history.complete_short_reply(
                    channel=request.channel,
                    chat_id=request.chat_id,
                    message_id=request.message_id,
                    outcome="silence",
                    emoji=None,
                    now_ms=int(self._clock()),
                )
            except Exception:  # noqa: BLE001 - preserve cancellation if claim cleanup fails
                pass
            raise
        except Exception:  # noqa: BLE001 - spec §8 maps unexpected failures to fallback
            verdict = ShortReplyVerdict(action="error", error="provider_error")

        outcome = self._outcome(verdict, recent)
        try:
            self._history.complete_short_reply(
                channel=request.channel,
                chat_id=request.chat_id,
                message_id=request.message_id,
                outcome=outcome.kind,
                emoji=outcome.emoji,
                now_ms=int(self._clock()),
            )
        except Exception:  # noqa: BLE001 - do not emit an unrecorded reaction
            outcome = ReactorOutcome("silence", reason="history_unavailable")
        return self._log(
            request, mode, outcome, verdict=verdict, recent=recent, started=started
        )

    def _outcome(self, verdict: ShortReplyVerdict, recent: tuple[str, ...]) -> ReactorOutcome:
        if verdict.action == "answer" and self._settings.allow_answer:
            return ReactorOutcome("answer", reason="answer")
        if verdict.action in {"react", "answer"} and verdict.emojis:
            emoji = choose_varied(verdict.emojis, recent)
            if emoji:
                return ReactorOutcome("react", emoji=emoji, source="model", reason="model")
            if verdict.action == "react":
                return ReactorOutcome("silence", reason="triple_repeat")
        if verdict.action == "none":
            return ReactorOutcome("silence", reason="decided_none")
        reason = f"error:{verdict.error or 'no_candidates'}"
        if self._settings.fallback == "neutral_rotation":
            emoji = choose_varied(self._settings.fallback_emojis, recent)
            if emoji:
                return ReactorOutcome("react", emoji=emoji, source="fallback", reason=reason)
        return ReactorOutcome("silence", reason="fallback_silence")

    def _log(
        self,
        request: ReactorRequest,
        mode: str,
        outcome: ReactorOutcome,
        *,
        verdict: ShortReplyVerdict | None = None,
        recent: tuple[str, ...] = (),
        started: float | None = None,
    ) -> ReactorOutcome:
        usage = dict(getattr(verdict, "usage", {}) or {})
        fields = request.signals.log_fields()
        error = (
            "history_unavailable"
            if outcome.reason == "history_unavailable"
            else getattr(verdict, "error", "") or ""
        )
        logger.info(
            "reaction_decision mode={} chat={} message_id={} source_ids={} thread_id={} turn_id={} "
            "graphemes={} emoji_only={} has_media={} bot_asked={} decider_action={} "
            "candidates={} recent={} chosen={} source={} outcome={} reason={} "
            "latency_ms={} model_latency_ms={} prompt_tokens={} completion_tokens={} model={} error={}",
            mode,
            request.chat_id,
            request.message_id,
            ",".join(request.source_ids) or request.message_id,
            request.thread_id or "-",
            request.turn_id or "-",
            fields["graphemes"],
            fields["emoji_only"],
            fields["has_media"],
            fields["bot_asked"],
            getattr(verdict, "action", "-"),
            "".join(getattr(verdict, "emojis", ()) or ()) or "-",
            "".join(recent) or "-",
            outcome.emoji or "-",
            outcome.source,
            outcome.kind,
            outcome.reason or "-",
            int((time.monotonic() - started) * 1000) if started is not None else 0,
            int(getattr(verdict, "latency_ms", 0) or 0),
            usage.get("prompt_tokens", "-"),
            usage.get("completion_tokens", "-"),
            getattr(verdict, "model", "") or "-",
            error,
        )
        return outcome

    def shadow(self, request: ReactorRequest) -> bool:
        """Decide in the background and only log it. False when the cap drops it."""
        if len(self._shadow_tasks) >= self._max_shadow_tasks:
            self.shadow_dropped += 1
            logger.info(
                "reaction_decision mode=shadow chat={} message_id={} source_ids={} "
                "outcome=silence reason=shadow_dropped dropped={}",
                request.chat_id,
                request.message_id,
                ",".join(request.source_ids) or request.message_id,
                self.shadow_dropped,
            )
            return False
        task = asyncio.get_running_loop().create_task(self._shadow_run(request))
        self._shadow_tasks.add(task)
        task.add_done_callback(self._shadow_tasks.discard)
        return True

    async def _shadow_run(self, request: ReactorRequest) -> None:
        try:
            await self.decide(request, mode="shadow")
        except Exception as exc:  # noqa: BLE001 - shadow must never disturb the chat
            logger.warning("reaction_decision_shadow_failed error_type={}", type(exc).__name__)

    async def drain(self) -> None:
        if self._shadow_tasks:
            await asyncio.gather(*list(self._shadow_tasks), return_exceptions=True)


__all__ = ["ReactorOutcome", "ReactorRequest", "ShortReplyReactor"]
