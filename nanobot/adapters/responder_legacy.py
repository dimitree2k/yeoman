"""Responder adapter that delegates generation to the legacy AgentLoop."""

from __future__ import annotations

from typing import override

from nanobot.agent.loop import AgentLoop
from nanobot.bus.events import InboundMessage
from nanobot.core.models import InboundEvent, PolicyDecision
from nanobot.core.ports import ResponderPort


class LegacyResponderAdapter(ResponderPort):
    """Bridge typed core events into the existing tool/LLM execution engine."""

    def __init__(self, agent_loop: AgentLoop) -> None:
        self._agent_loop = agent_loop

    @override
    async def generate_reply(self, event: InboundEvent, decision: PolicyDecision) -> str | None:
        metadata: dict[str, object] = {
            "is_group": event.is_group,
            "mentioned_bot": event.mentioned_bot,
            "reply_to_bot": event.reply_to_bot,
            "reply_to_message_id": event.reply_to_message_id,
            "reply_to_participant": event.reply_to_participant,
            "reply_to_text": event.reply_to_text,
            "message_id": event.message_id,
            # Make persona decision available as explicit metadata override for vNext.
            "persona_text_override": decision.persona_text,
            "persona_file_override": decision.persona_file,
            "policy_reason_override": decision.reason,
            "policy_source_override": decision.source,
        }
        msg = InboundMessage(
            channel=event.channel,
            sender_id=event.sender_id,
            chat_id=event.chat_id,
            content=event.content,
            metadata=metadata,
            media=list(event.media),
            timestamp=event.timestamp.replace(tzinfo=None),
        )
        out = await self._agent_loop._process_message(msg)
        if out is None:
            return None
        return out.content
