"""Policy adapter bridging legacy middleware to typed core policy port."""

from __future__ import annotations

from typing import override

from nanobot.bus.events import InboundMessage
from nanobot.core.models import InboundEvent, PolicyDecision
from nanobot.core.ports import PolicyPort
from nanobot.policy.middleware import PolicyMiddleware


class LegacyPolicyAdapter(PolicyPort):
    """Adapter from `PolicyMiddleware` to typed `PolicyPort`."""

    def __init__(self, middleware: PolicyMiddleware | None) -> None:
        self._middleware = middleware

    @override
    def evaluate(self, event: InboundEvent) -> PolicyDecision:
        if self._middleware is None:
            return PolicyDecision(
                accept_message=True,
                should_respond=True,
                allowed_tools=frozenset(),
                reason="policy_disabled",
                persona_text=None,
                persona_file=None,
                source="disabled",
            )

        msg = InboundMessage(
            channel=event.channel,
            sender_id=event.sender_id,
            chat_id=event.chat_id,
            content=event.content,
            metadata={
                "is_group": event.is_group,
                "mentioned_bot": event.mentioned_bot,
                "reply_to_bot": event.reply_to_bot,
                "reply_to_message_id": event.reply_to_message_id,
                "reply_to_participant": event.reply_to_participant,
                "reply_to_text": event.reply_to_text,
                "message_id": event.message_id,
            },
            media=list(event.media),
            # Keep original inbound timestamp for deterministic policy logs.
            timestamp=event.timestamp.replace(tzinfo=None),
        )
        context = self._middleware.evaluate_message(msg)
        return PolicyDecision(
            accept_message=context.decision.accept_message,
            should_respond=context.decision.should_respond,
            allowed_tools=frozenset(context.decision.allowed_tools),
            reason=context.decision.reason,
            persona_text=context.persona_text,
            persona_file=context.decision.persona_file,
            source=context.source,
        )
