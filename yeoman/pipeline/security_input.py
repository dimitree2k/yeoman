"""Input security check middleware.

Corresponds to orchestrator stage 10: validate user input against security
rules before the LLM is invoked.  Blocks suspicious content with an emoji
reaction or text message.

Defence layers:
1. Fast regex-based rules (synchronous) — catches known patterns.
2. Optional LLM classifier (async) — catches subtle / multilingual injection.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger

from nanobot.core.intents import SendOutboundIntent, SendReactionIntent
from nanobot.core.models import OutboundEvent, SecurityDecision
from nanobot.core.pipeline import NextFn, PipelineContext
from nanobot.core.ports import SecurityPort

if TYPE_CHECKING:
    from nanobot.security.classifier import InputClassifier


class InputSecurityMiddleware:
    """Check inbound text against security rules; halt if blocked."""

    def __init__(
        self,
        *,
        security: SecurityPort | None = None,
        classifier: "InputClassifier | None" = None,
        block_message: str = "😂",
    ) -> None:
        self._security = security
        self._classifier = classifier
        self._block_message = block_message

    async def __call__(self, ctx: PipelineContext, next: NextFn) -> None:
        if self._security is None:
            await next(ctx)
            return

        # Layer 1: fast regex rules (sync).
        result = self._security.check_input(
            ctx.event.content,
            context={
                "channel": ctx.event.channel,
                "chat_id": ctx.event.chat_id,
                "sender_id": ctx.event.sender_id,
                "message_id": ctx.event.message_id or "",
            },
        )

        if result.decision.action == "block":
            self._block(ctx, result.decision)
            return

        # Layer 2: LLM classifier (async, only when regex allowed).
        if self._classifier is not None and ctx.event.content:
            try:
                llm_decision = await self._classifier.classify(ctx.event.content)
            except Exception as exc:
                logger.debug("security classifier error (fail-open): {}", exc)
                llm_decision = None

            if llm_decision is not None and llm_decision.action == "block":
                self._block(ctx, llm_decision)
                return

        await next(ctx)

    def _block(self, ctx: PipelineContext, decision: SecurityDecision) -> None:
        ctx.metric(
            "security_input_blocked",
            labels=(
                ("channel", ctx.event.channel),
                ("reason", decision.reason),
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
