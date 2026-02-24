"""Responder middleware — LLM reply generation with typing indicator.

Corresponds to orchestrator stages 11-12: show typing indicator, delegate
to ``ResponderPort.generate_reply()``, and store the reply in ``ctx.reply``.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable

from nanobot.core.pipeline import NextFn, PipelineContext
from nanobot.core.ports import ResponderPort

_REACTION_RE = re.compile(r"^\s*::reaction::(.+?)\s*$", re.DOTALL)


class ResponderMiddleware:
    """Generate an LLM reply and place it in ``ctx.reply``."""

    def __init__(
        self,
        *,
        responder: ResponderPort,
        typing_notifier: Callable[[str, str, bool], Awaitable[None]] | None = None,
    ) -> None:
        self._responder = responder
        self._typing_notifier = typing_notifier

    async def __call__(self, ctx: PipelineContext, next: NextFn) -> None:
        if ctx.decision is None:
            await next(ctx)
            return

        typing_started = False
        try:
            # Start typing indicator.
            if ctx.event.channel == "whatsapp":
                if self._typing_notifier is not None:
                    await self._typing_notifier(ctx.event.channel, ctx.event.chat_id, True)
                else:
                    from nanobot.core.intents import SetTypingIntent

                    ctx.intents.append(
                        SetTypingIntent(
                            channel=ctx.event.channel,
                            chat_id=ctx.event.chat_id,
                            enabled=True,
                        )
                    )
                typing_started = True

            reply = await self._responder.generate_reply(ctx.event, ctx.decision)

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
                    from nanobot.core.intents import SetTypingIntent

                    ctx.intents.append(
                        SetTypingIntent(
                            channel=ctx.event.channel,
                            chat_id=ctx.event.chat_id,
                            enabled=False,
                        )
                    )
