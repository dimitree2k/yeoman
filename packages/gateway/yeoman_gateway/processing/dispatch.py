"""Single dispatch line for approved effects (spec R05, R08).

Envelope -> final policy -> payload validation -> revision check/claim -> transport ->
receipt. Producers never call a transport directly in the new mode; they plan an effect
and hand it to :class:`yeoman_gateway.processing.effects.EffectGateway`.

Honest reporting rule: acceptance by the local outbound queue is **not** proof of a
transport send. Without a transport confirmation the executor reports ``unknown``, which
is resolved later by reconciliation (Plan 04) - it is never silently upgraded to
``sent``.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol, runtime_checkable

from loguru import logger

from yeoman_gateway.bus.events import OutboundMessage, ReactionMessage
from yeoman_gateway.core.intents import SendOutboundIntent, SendReactionIntent
from yeoman_gateway.processing.models import (
    DeletePayload,
    EffectEnvelope,
    EffectReceipt,
    EffectTarget,
    ExternalActionPayload,
    MediaPayload,
    ProcessingError,
    ReactionPayload,
    TextPayload,
    canonical_hash,
    payload_to_mapping,
)
from yeoman_gateway.processing.models import (
    now_ms as _now_ms,
)

#: Capability per payload kind. Enforced again by the authorizer before dispatch.
CAPABILITY_BY_KIND: Mapping[str, str] = {
    "text": "send_text",
    "media": "send_media",
    "reaction": "send_reaction",
    "delete": "delete_message",
    "external_action": "external_action",
}


@runtime_checkable
class EffectTransport(Protocol):
    """Minimal transport surface the executor may use."""

    async def publish_outbound(self, message: OutboundMessage) -> None: ...

    async def publish_reaction(self, message: ReactionMessage) -> None: ...


class EffectPayloadRejectedError(ProcessingError):
    """Payload failed validation for its declared capability."""


def validate_payload(envelope: EffectEnvelope) -> None:
    """Per-kind payload validation. Different kinds need different validators."""
    payload = envelope.payload
    match payload:
        case TextPayload():
            if not payload.text.strip():
                raise EffectPayloadRejectedError("text payload is empty")
        case MediaPayload():
            if not payload.media:
                raise EffectPayloadRejectedError("media payload has no media references")
            if any(not str(item).strip() for item in payload.media):
                raise EffectPayloadRejectedError("media payload contains an empty reference")
        case ReactionPayload():
            if not payload.message_id or not payload.emoji:
                raise EffectPayloadRejectedError("reaction payload requires message_id and emoji")
        case DeletePayload():
            if not payload.message_id:
                raise EffectPayloadRejectedError("delete payload requires a message_id")
        case ExternalActionPayload():
            if not payload.action:
                raise EffectPayloadRejectedError("external action payload requires an action")


class BusEffectExecutor:
    """Transport executor for bus-backed chat channels.

    ``confirm`` is the optional transport acknowledgement hook. Without it a successful
    queue write is reported as ``unknown``: the effect may still be in flight, so it must
    not be recorded as a proven success.
    """

    def __init__(
        self,
        *,
        bus: EffectTransport,
        confirm: Callable[[EffectEnvelope], Awaitable[bool]] | None = None,
        delete_handler: Callable[[EffectEnvelope], Awaitable[bool]] | None = None,
        external_handler: Callable[[EffectEnvelope], Awaitable[bool]] | None = None,
    ) -> None:
        self._bus = bus
        self._confirm = confirm
        self._delete_handler = delete_handler
        self._external_handler = external_handler

    async def execute(self, envelope: EffectEnvelope) -> EffectReceipt:
        validate_payload(envelope)
        payload = envelope.payload
        target = envelope.target

        if isinstance(payload, TextPayload):
            await self._bus.publish_outbound(
                OutboundMessage(
                    channel=target.channel,
                    chat_id=target.chat_id,
                    content=payload.text,
                    reply_to=payload.reply_to,
                )
            )
        elif isinstance(payload, MediaPayload):
            await self._bus.publish_outbound(
                OutboundMessage(
                    channel=target.channel,
                    chat_id=target.chat_id,
                    content=payload.caption or "",
                    media=list(payload.media),
                )
            )
        elif isinstance(payload, ReactionPayload):
            await self._bus.publish_reaction(
                ReactionMessage(
                    channel=target.channel,
                    chat_id=target.chat_id,
                    message_id=payload.message_id,
                    emoji=payload.emoji,
                )
            )
        elif isinstance(payload, DeletePayload):
            handler = self._delete_handler
            if handler is None:
                raise EffectPayloadRejectedError("no delete transport registered")
            if not await handler(envelope):
                return EffectReceipt(
                    effect_id=envelope.effect_id,
                    state="not_executed",
                    detail="delete transport refused",
                )
        elif isinstance(payload, ExternalActionPayload):
            handler = self._external_handler
            if handler is None:
                raise EffectPayloadRejectedError(
                    f"no external transport registered for action {payload.action!r}"
                )
            if not await handler(envelope):
                return EffectReceipt(
                    effect_id=envelope.effect_id,
                    state="not_executed",
                    detail=f"external transport refused {payload.action!r}",
                )
        else:  # pragma: no cover - typed payload union is exhaustive
            raise EffectPayloadRejectedError(f"unsupported payload: {type(payload).__name__}")

        if self._confirm is not None:
            confirmed = await self._confirm(envelope)
            return EffectReceipt(
                effect_id=envelope.effect_id,
                state="sent" if confirmed else "unknown",
                detail=(
                    "confirmed by transport"
                    if confirmed
                    else "queued to transport; delivery unconfirmed"
                ),
            )
        return EffectReceipt(
            effect_id=envelope.effect_id,
            state="unknown",
            detail="accepted by the outbound queue; transport outcome unproven",
        )


class IntentEffectRouter:
    """Turns orchestrator intents into planned effects for managed chats.

    Returns ``True`` when the intent was handled by the effect path. ``False`` means the
    chat is not managed and the caller must keep using the legacy path - never both for
    the same turn.
    """

    def __init__(
        self,
        *,
        gateway: Any,
        config: Any,
        clock: Callable[[], int] | None = None,
        worker_id: str = "effect-gateway",
    ) -> None:
        self._gateway = gateway
        self._config = config
        self._clock = clock or _now_ms
        self._worker_id = worker_id

    def manages(self, channel: str, chat_id: str) -> bool:
        processing = getattr(self._config, "processing", None)
        if processing is None or not getattr(processing, "enabled", False):
            return False
        checker = getattr(processing, "is_chat_enabled", None)
        if checker is None:
            return bool(getattr(processing, "enabled", False))
        return bool(checker(channel, chat_id))

    async def submit_outbound(self, intent: SendOutboundIntent, *, principal: str) -> bool:
        event = intent.event
        if not self.manages(event.channel, event.chat_id):
            return False
        media = list(event.media or [])
        payload: Any = (
            MediaPayload(media=tuple(media), caption=event.content or None)
            if media
            else TextPayload(text=event.content, reply_to=event.reply_to)
        )
        metadata = dict(event.metadata or {})
        source = str(metadata.get("message_id") or "turn")
        await self._run(
            channel=event.channel,
            chat_id=event.chat_id,
            principal=principal,
            payload=payload,
            # Stable per logical action: source identity plus a deterministic content
            # digest, never a per-retry random value and never bare text content.
            operation_key=(
                f"outbound:{event.channel}:{event.chat_id}:{source}:"
                f"{canonical_hash(payload_to_mapping(payload))[:12]}"
            ),
            trace_id=str(metadata.get("trace_id") or source),
            deadline_key="reactive_ms",
        )
        return True

    async def submit_reaction(self, intent: SendReactionIntent, *, principal: str) -> bool:
        if not self.manages(intent.channel, intent.chat_id):
            return False
        await self._run(
            channel=intent.channel,
            chat_id=intent.chat_id,
            principal=principal,
            payload=ReactionPayload(message_id=intent.message_id, emoji=intent.emoji),
            operation_key=f"reaction:{intent.channel}:{intent.chat_id}:{intent.message_id}:{intent.emoji}",
            trace_id=intent.message_id,
            deadline_key="semantic_reaction_ms",
        )
        return True

    async def _run(
        self,
        *,
        channel: str,
        chat_id: str,
        principal: str,
        payload: Any,
        operation_key: str,
        trace_id: str,
        deadline_key: str,
    ) -> EffectReceipt:
        now = self._clock()
        processing = self._config.processing
        deadline_ms = int(getattr(processing.deadlines, deadline_key))
        envelope = EffectEnvelope(
            effect_id=uuid.uuid4().hex,
            operation_key=operation_key,
            payload=payload,
            target=EffectTarget(channel=channel, chat_id=chat_id),
            trace_id=trace_id,
            turn_id="",
            turn_revision=1,
            principal=principal,
            capability=CAPABILITY_BY_KIND[payload.kind],
            expires_at_ms=now + deadline_ms,
            created_ms=now,
        )
        receipt = self._gateway.submit(envelope)
        result = await self._gateway.execute_ready(receipt.effect_id)
        if result.state != "sent":
            logger.warning(
                "effect not delivered effect_id={} state={} detail={} capability={} chat={}",
                result.effect_id,
                result.state,
                result.detail,
                envelope.capability,
                chat_id,
            )
        return result
