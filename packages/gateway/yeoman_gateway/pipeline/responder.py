"""Responder middleware — LLM reply generation with typing indicator.

Corresponds to orchestrator stages 11-12: show typing indicator, delegate
to ``ResponderPort.generate_reply()``, and store the reply in ``ctx.reply``.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable

from yeoman_gateway.core.models import InboundEvent
from yeoman_gateway.core.pipeline import NextFn, PipelineContext
from yeoman_gateway.core.ports import ResponderPort

_REACTION_RE = re.compile(r"^\s*::reaction::(.+?)\s*$", re.DOTALL)


class ResponderMiddleware:
    """Generate an LLM reply and place it in ``ctx.reply``."""

    def __init__(
        self,
        *,
        responder: ResponderPort,
        typing_notifier: Callable[[str, str, bool], Awaitable[None]] | None = None,
        reply_admission: Callable[[InboundEvent], bool] | None = None,
        report_lookup: Callable[[InboundEvent], str | None] | None = None,
    ) -> None:
        self._responder = responder
        self._typing_notifier = typing_notifier
        self._reply_admission = reply_admission
        self._report_lookup = report_lookup

    async def __call__(self, ctx: PipelineContext, next: NextFn) -> None:
        if ctx.decision is None:
            await next(ctx)
            return

        if (
            ctx.decision.should_respond
            and self._reply_admission is not None
            and not self._reply_admission(ctx.event)
        ):
            ctx.metric("reply_admission_failed", labels=(("channel", ctx.event.channel),))
            ctx.halt()
            return

        typing_started = False
        try:
            # Start typing indicator.
            if ctx.event.channel == "whatsapp":
                if self._typing_notifier is not None:
                    await self._typing_notifier(ctx.event.channel, ctx.event.chat_id, True)
                else:
                    from yeoman_gateway.core.intents import SetTypingIntent

                    ctx.intents.append(
                        SetTypingIntent(
                            channel=ctx.event.channel,
                            chat_id=ctx.event.chat_id,
                            enabled=True,
                        )
                    )
                typing_started = True

            report = None
            quoted_full = bool(
                ctx.event.reply_to_bot
                and ctx.event.reply_to_message_id
                and re.fullmatch(r"(?i)\s*(?:langfassung|vollbericht)(?:\s+bitte)?[.!?]?\s*", ctx.event.content)
            )
            stock_summary = bool(
                re.search(r"\([A-Z]{1,5}\)|\$[A-Z]{1,5}\b", ctx.event.content)
                and re.search(r"(?i)\b(?:aktie|stock|tradingguru|tradingagents|analyse)\b", ctx.event.content)
            )
            if self._report_lookup is not None and ctx.decision.is_owner and ctx.event.channel == "whatsapp" and (quoted_full or stock_summary):
                report = self._report_lookup(ctx.event)
            reply = (
                report or "Die Langfassung zu dieser Nachricht wurde leider nicht gespeichert."
                if report is not None or quoted_full
                else await self._responder.generate_reply(ctx.event, ctx.decision)
            )

            if not reply:
                ctx.metric("responder_empty", labels=(("channel", ctx.event.channel),))
                ctx.halt()
                return

            ctx.reply = reply
            await next(ctx)

        finally:
            if typing_started:
                if self._typing_notifier is not None:
                    await self._typing_notifier(ctx.event.channel, ctx.event.chat_id, False)
                else:
                    from yeoman_gateway.core.intents import SetTypingIntent

                    ctx.intents.append(
                        SetTypingIntent(
                            channel=ctx.event.channel,
                            chat_id=ctx.event.chat_id,
                            enabled=False,
                        )
                    )
