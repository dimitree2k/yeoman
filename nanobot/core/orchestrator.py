"""Typed vNext orchestrator pipeline."""

from __future__ import annotations

import time
from dataclasses import replace
from typing import Awaitable, Callable

from nanobot.core.intents import (
    OrchestratorIntent,
    PersistSessionIntent,
    RecordMetricIntent,
    SendOutboundIntent,
    SetTypingIntent,
)
from nanobot.core.models import InboundEvent, OutboundEvent
from nanobot.core.ports import PolicyPort, ReplyArchivePort, ResponderPort


class Orchestrator:
    """Deterministic pipeline for inbound processing."""

    def __init__(
        self,
        *,
        policy: PolicyPort,
        responder: ResponderPort,
        reply_archive: ReplyArchivePort | None = None,
        dedupe_ttl_seconds: int = 20 * 60,
        typing_notifier: Callable[[str, str, bool], Awaitable[None]] | None = None,
    ) -> None:
        self._policy = policy
        self._responder = responder
        self._reply_archive = reply_archive
        self._typing_notifier = typing_notifier
        self._dedupe_ttl_seconds = max(1, int(dedupe_ttl_seconds))
        self._recent_message_keys: dict[str, float] = {}
        self._next_dedupe_cleanup_at = 0.0

    async def handle(self, event: InboundEvent) -> list[OrchestratorIntent]:
        """Process one inbound event and return executable intents."""
        intents: list[OrchestratorIntent] = []
        normalized = event.normalized_content()
        if not normalized:
            intents.append(
                RecordMetricIntent(
                    name="event_drop_empty",
                    labels=(("channel", event.channel),),
                )
            )
            return intents
        event = replace(event, content=normalized)

        if self._is_duplicate(event):
            intents.append(
                RecordMetricIntent(
                    name="event_drop_duplicate",
                    labels=(("channel", event.channel),),
                )
            )
            return intents

        self._record_archive(event)
        event, archive_hit = self._resolve_reply_context(event)
        if event.channel == "whatsapp":
            intents.append(
                RecordMetricIntent(
                    name="reply_ctx_archive_hit" if archive_hit else "reply_ctx_archive_miss",
                    labels=(("channel", event.channel),),
                )
            )

        decision = self._policy.evaluate(event)
        if not decision.accept_message:
            intents.append(
                RecordMetricIntent(
                    name="policy_drop_access",
                    labels=(("channel", event.channel), ("reason", decision.reason)),
                )
            )
            return intents
        if not decision.should_respond:
            intents.append(
                RecordMetricIntent(
                    name="policy_drop_reply",
                    labels=(("channel", event.channel), ("reason", decision.reason)),
                )
            )
            return intents

        typing_started = False
        try:
            if event.channel == "whatsapp":
                if self._typing_notifier is None:
                    intents.append(
                        SetTypingIntent(
                            channel=event.channel,
                            chat_id=event.chat_id,
                            enabled=True,
                        )
                    )
                else:
                    await self._typing_notifier(event.channel, event.chat_id, True)
                typing_started = True

            reply = await self._responder.generate_reply(event, decision)
            if not reply:
                intents.append(
                    RecordMetricIntent(
                        name="responder_empty",
                        labels=(("channel", event.channel),),
                    )
                )
                return intents

            outbound_channel = event.channel
            outbound_chat_id = event.chat_id
            if event.channel == "system" and ":" in event.chat_id:
                route_channel, route_chat_id = event.chat_id.split(":", 1)
                if route_channel and route_chat_id:
                    outbound_channel = route_channel
                    outbound_chat_id = route_chat_id

            outbound = OutboundEvent(
                channel=outbound_channel,
                chat_id=outbound_chat_id,
                content=reply,
            )
            intents.append(SendOutboundIntent(event=outbound))
            intents.append(
                PersistSessionIntent(
                    session_key=f"{event.channel}:{event.chat_id}",
                    user_content=event.content,
                    assistant_content=reply,
                )
            )
            intents.append(
                RecordMetricIntent(
                    name="response_sent",
                    labels=(("channel", event.channel),),
                )
            )
            return intents
        finally:
            if typing_started:
                if self._typing_notifier is None:
                    intents.append(
                        SetTypingIntent(
                            channel=event.channel,
                            chat_id=event.chat_id,
                            enabled=False,
                        )
                    )
                else:
                    await self._typing_notifier(event.channel, event.chat_id, False)

    def _dedupe_key(self, event: InboundEvent) -> str | None:
        if not event.message_id:
            return None
        return f"{event.channel}:{event.chat_id}:{event.message_id}"

    def _is_duplicate(self, event: InboundEvent) -> bool:
        key = self._dedupe_key(event)
        if key is None:
            return False
        now = time.monotonic()
        if now >= self._next_dedupe_cleanup_at:
            expired = [k for k, expires_at in self._recent_message_keys.items() if expires_at <= now]
            for expired_key in expired:
                self._recent_message_keys.pop(expired_key, None)
            self._next_dedupe_cleanup_at = now + 30.0
        if key in self._recent_message_keys:
            return True
        self._recent_message_keys[key] = now + float(self._dedupe_ttl_seconds)
        return False

    def _record_archive(self, event: InboundEvent) -> None:
        if self._reply_archive is None:
            return
        self._reply_archive.record_inbound(event)
        if event.reply_to_message_id and event.reply_to_text:
            seeded = replace(
                event,
                message_id=event.reply_to_message_id,
                content=event.reply_to_text,
            )
            self._reply_archive.record_inbound(seeded)

    def _resolve_reply_context(self, event: InboundEvent) -> tuple[InboundEvent, bool]:
        if self._reply_archive is None:
            return event, False
        if event.channel != "whatsapp":
            return event, False
        if event.reply_to_text:
            return event, False
        reply_to_message_id = (event.reply_to_message_id or "").strip()
        if not reply_to_message_id:
            return event, False

        row = self._reply_archive.lookup_message(event.channel, event.chat_id, reply_to_message_id)
        if row is None:
            row = self._reply_archive.lookup_message_any_chat(
                event.channel,
                reply_to_message_id,
                preferred_chat_id=event.chat_id,
            )
        if row is None:
            return event, False

        text = row.text.strip()
        if not text:
            return event, False
        return replace(event, reply_to_text=text), True
