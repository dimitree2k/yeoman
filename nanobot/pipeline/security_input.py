"""Input security check middleware.

Corresponds to orchestrator stage 10: validate user input against security
rules before the LLM is invoked.  Blocks suspicious content with an emoji
reaction or text message.
"""

from __future__ import annotations

from nanobot.core.intents import SendOutboundIntent, SendReactionIntent
from nanobot.core.models import OutboundEvent
from nanobot.core.pipeline import NextFn, PipelineContext
from nanobot.core.ports import SecurityPort


class InputSecurityMiddleware:
    """Check inbound text against security rules; halt if blocked."""

    def __init__(
        self,
        *,
        security: SecurityPort | None = None,
        block_message: str = "😂",
    ) -> None:
        self._security = security
        self._block_message = block_message

    async def __call__(self, ctx: PipelineContext, next: NextFn) -> None:
        if self._security is None:
            await next(ctx)
            return

        result = self._security.check_input(
            ctx.event.content,
            context={
                "channel": ctx.event.channel,
                "chat_id": ctx.event.chat_id,
                "sender_id": ctx.event.sender_id,
                "message_id": ctx.event.message_id or "",
            },
        )

        if result.decision.action != "block":
            await next(ctx)
            return

        ctx.metric(
            "security_input_blocked",
            labels=(
                ("channel", ctx.event.channel),
                ("reason", result.decision.reason),
            ),
        )

        if ctx.event.message_id:
            ctx.intents.append(
                SendReactionIntent(
                    channel=ctx.event.channel,
                    chat_id=ctx.event.chat_id,
                    message_id=ctx.event.message_id,
                    emoji=self._block_message,
                    participant_jid=ctx.event.participant,
                )
            )
        else:
            ctx.intents.append(
                SendOutboundIntent(
                    event=OutboundEvent(
                        channel=ctx.event.channel,
                        chat_id=ctx.event.chat_id,
                        content=self._block_message,
                    )
                )
            )

        ctx.halt()
