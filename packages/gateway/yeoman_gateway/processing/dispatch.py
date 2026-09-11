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

import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator, Protocol, runtime_checkable

from loguru import logger
from yeoman_shared.reactions import SYSTEM_ORIGIN, allowed_reaction

from yeoman_gateway.bus.events import OutboundMessage, ReactionMessage
from yeoman_gateway.core.intents import SendOutboundIntent, SendReactionIntent
from yeoman_gateway.processing.budget import ChatBudget, ThreadBudget
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
    TransportReceipt,
    TurnBinding,
    canonical_hash,
    payload_to_mapping,
)
from yeoman_gateway.processing.models import (
    now_ms as _now_ms,
)
from yeoman_gateway.processing.timings import PhaseTimings

#: Provenance marker set by the effect transport. The managed-chat guard only lets
#: outbound messages carrying it through.
EFFECT_PROVENANCE_KEY = "processing_effect"

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
        mark_provenance: bool = False,
        security: Any | None = None,
        security_block_message: str = "\U0001f602",
        direct_sender: Callable[[OutboundMessage], Awaitable[None]] | None = None,
        direct_reaction_sender: Callable[[ReactionMessage], Awaitable[None]] | None = None,
    ) -> None:
        self._bus = bus
        self._mark_provenance = mark_provenance
        self._security = security
        self._security_block_message = security_block_message
        self._direct_sender = direct_sender
        self._direct_reaction_sender = direct_reaction_sender
        self._confirm = confirm
        self._delete_handler = delete_handler
        self._external_handler = external_handler

    def set_direct_senders(
        self,
        outbound: Callable[[OutboundMessage], Awaitable[None]] | None,
        reaction: Callable[[ReactionMessage], Awaitable[None]] | None,
    ) -> None:
        """Install the channel transport adapter that can confirm a real send."""
        self._direct_sender = outbound
        self._direct_reaction_sender = reaction

    async def _deliver(self, message: OutboundMessage) -> "TransportReceipt | None":
        """Hand one message to the transport; return the provider receipt if it reported one.

        ``None`` means either the bus path (no confirmation possible) or an adapter that
        reported no provider id - in both cases the effect stays unproven beyond local
        acceptance.
        """
        if self._direct_sender is not None:
            reported = await self._direct_sender(message)
            return _receipt_from_report(reported, message)
        await self._bus.publish_outbound(message)
        return None

    async def _deliver_reaction(self, message: ReactionMessage) -> "TransportReceipt | None":
        """Reaction counterpart of :meth:`_deliver`."""
        if self._direct_reaction_sender is not None:
            reported = await self._direct_reaction_sender(message)
            return _receipt_from_report(reported, message)
        await self._bus.publish_reaction(message)
        return None

    def _guard_text(self, envelope: EffectEnvelope, text: str) -> str:
        """Shared outbound control for text-bearing effects.

        Respects the existing security settings (enabled/stages) and mirrors the legacy
        outbound stage: sanitize replaces the text, block falls back to the configured
        block message. Media, reaction and delete payloads have their own validators.
        """
        if self._security is None:
            return text
        result = self._security.check_output(
            text,
            context={
                "channel": envelope.target.channel,
                "chat_id": envelope.target.chat_id,
                "effect_id": envelope.effect_id,
                "capability": envelope.capability,
            },
        )
        action = result.decision.action
        if action in ("sanitize", "block"):
            return result.sanitized_text or self._security_block_message
        return text

    async def execute(self, envelope: EffectEnvelope) -> EffectReceipt:
        validate_payload(envelope)
        payload = envelope.payload
        target = envelope.target

        provenance = {EFFECT_PROVENANCE_KEY: envelope.effect_id} if self._mark_provenance else {}
        receipt: TransportReceipt | None = None

        if isinstance(payload, TextPayload):
            receipt = await self._deliver(
                OutboundMessage(
                    channel=target.channel,
                    chat_id=target.chat_id,
                    content=self._guard_text(envelope, payload.text),
                    reply_to=payload.reply_to,
                    metadata=dict(provenance),
                )
            )
        elif isinstance(payload, MediaPayload):
            receipt = await self._deliver(
                OutboundMessage(
                    channel=target.channel,
                    chat_id=target.chat_id,
                    content=self._guard_text(envelope, payload.caption or ""),
                    media=list(payload.media),
                    metadata=dict(provenance),
                )
            )
        elif isinstance(payload, ReactionPayload):
            receipt = await self._deliver_reaction(
                ReactionMessage(
                    channel=target.channel,
                    chat_id=target.chat_id,
                    message_id=payload.message_id,
                    emoji=payload.emoji,
                    metadata=dict(provenance),
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
        if self._direct_sender is not None or self._direct_reaction_sender is not None:
            return EffectReceipt(
                effect_id=envelope.effect_id,
                state="sent",
                detail="accepted by the channel transport adapter",
                transport_receipt=receipt,
            )
        return EffectReceipt(
            effect_id=envelope.effect_id,
            state="unknown",
            detail="accepted by the outbound queue; transport outcome unproven",
        )


class EffectNotDeliveredError(ProcessingError):
    """A managed effect did not reach a proven delivered state.

    Producers must surface this instead of claiming delivery: an accepted queue entry is
    not a sent message (spec R05).
    """


#: Principal that caused the tool call currently running. Set by the responder, never
#: read from model arguments.
CURRENT_PRINCIPAL: ContextVar[str] = ContextVar("yeoman_effect_principal", default="")

#: Turn a producer is currently working for. Set by the turn pipeline; producers never
#: invent a turn, and a turn-bound producer refuses to queue an effect without one.
CURRENT_TURN: ContextVar[Any] = ContextVar("yeoman_current_turn", default=None)


def classify_outbound(message: OutboundMessage) -> tuple[str, Any]:
    """Map one outbound message to its capability and typed payload."""
    metadata = dict(message.metadata or {})
    delete = metadata.get("delete_message")
    if isinstance(delete, Mapping) and delete.get("message_id"):
        return "delete_message", DeletePayload(message_id=str(delete["message_id"]))
    if message.media:
        return "send_media", MediaPayload(
            media=tuple(str(item) for item in message.media),
            caption=message.content or None,
        )
    return "send_text", TextPayload(text=message.content, reply_to=message.reply_to)


class ManagedOutboundDispatcher:
    """Drop-in replacement for ``bus.publish_outbound`` on managed chats.

    Unmanaged chats keep the legacy publish byte-for-byte. Managed chats go through the
    effect gateway, and an unproven outcome raises instead of pretending success.
    """

    def __init__(
        self,
        *,
        router: "IntentEffectRouter",
        bus: EffectTransport,
        principal: Callable[[], str] | None = None,
        timings: "PhaseTimings | None" = None,
    ) -> None:
        self._router = router
        self._bus = bus
        self._principal = principal or CURRENT_PRINCIPAL.get
        self._timings = timings

    async def __call__(self, message: OutboundMessage) -> None:
        if not self._router.manages(message.channel, message.chat_id):
            await self._bus.publish_outbound(message)
            return
        capability, payload = classify_outbound(message)
        with self._phase("effect_queue"):
            receipt = await self._router.submit_message(
                message, principal=self._principal(), capability=capability, payload=payload
            )
        if receipt.state != "sent":
            if self._timings is not None:
                self._timings.note_deferral(str(receipt.detail or receipt.state))
            raise EffectNotDeliveredError(
                f"effect not delivered (state={receipt.state}, detail={receipt.detail or '-'})"
            )

    @contextmanager
    def _phase(self, name: str) -> Iterator[None]:
        """Time a phase when instrumentation is attached, otherwise do nothing."""
        if self._timings is None:
            yield
            return
        with self._timings.phase(name):
            yield


#: Capabilities that must not run in the new mode: they write outside the chat
#: transport without a proven idempotency or containment contract (spec R05, G06).
#: The value names the concrete re-enable condition.
NON_MIGRATED_CAPABILITIES: Mapping[str, str] = {
    # `a2a_delegate` was fenced here for "remote write without an idempotency contract".
    # That contract now exists in the tool itself: every delegation is claimed in the
    # processing journal under a key derived from the turn, the worker and the task, and a
    # repeated claim is refused instead of being sent to the peer again. The journal is
    # open exactly while this fence is active, so the two cannot drift apart.
    "exec": (
        "shell writes are not contained by default and the sandbox keeps network access; "
        "re-enable when isolation is enforced rather than lexical"
    ),
    "browse": (
        "browser clicks/fills/JS have no capability check; re-enable with a read-only "
        "enforced capability"
    ),
    "calendar": (
        "remote CalDAV writes retry blindly after an unknown outcome; re-enable when the "
        "transport reports an idempotent result"
    ),
}


def disable_non_migrated_tools(registry: Any, *, only: tuple[str, ...] | None = None) -> dict[str, str]:
    """Refuse the non-migrated write capabilities while the new mode is active.

    Existing sandbox and tool settings are untouched; this only removes capabilities that
    have no proven effect contract.
    """
    disabled: dict[str, str] = {}
    for name, reason in NON_MIGRATED_CAPABILITIES.items():
        if only is not None and name not in only:
            continue
        if getattr(registry, "has", lambda _name: False)(name):
            registry.disable(name, reason)
            disabled[name] = reason
    if disabled:
        logger.warning(
            "new processing mode disables non-migrated capabilities: {}", sorted(disabled)
        )
    return disabled


#: Explicit service principals for system producers. A source that is not listed here
#: must not produce effects in the new mode - no fictional owner (spec R02).
SERVICE_PRINCIPALS: Mapping[str, str] = {
    "cron": "service:cron",
    "admin": "service:admin",
    "ipc": "service:ipc",
    "speakup": "service:speakup",
    "heartbeat": "service:heartbeat",
}


class SendBudget:
    """Hard per-chat send budget and waiting-outbox cap (spec R08 start values).

    Every real transport command counts, media items count individually, and the
    reservation is atomic within the process. Capacity that was consumed stays consumed
    until the sliding window ends - including for effects whose outcome is unknown.
    """

    def __init__(
        self,
        *,
        units: int,
        window_seconds: int,
        waiting_cap: int,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if units < 1 or window_seconds < 1:
            raise ValueError("budget needs at least one unit and a positive window")
        self._units = int(units)
        self._window = float(window_seconds)
        self._waiting_cap = int(waiting_cap)
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._spent: dict[str, list[float]] = {}

    @property
    def waiting_cap(self) -> int:
        return self._waiting_cap

    def reserve(self, key: str, units: int = 1) -> bool:
        """Reserve capacity for one transport command. False = over budget."""
        wanted = max(1, int(units))
        now = self._clock()
        with self._lock:
            window = [stamp for stamp in self._spent.get(key, []) if now - stamp < self._window]
            if len(window) + wanted > self._units:
                self._spent[key] = window
                return False
            window.extend([now] * wanted)
            self._spent[key] = window
        return True

    def spent(self, key: str) -> int:
        now = self._clock()
        with self._lock:
            window = [stamp for stamp in self._spent.get(key, []) if now - stamp < self._window]
            self._spent[key] = window
        return len(window)


def budget_key(channel: str, chat_id: str) -> str:
    return f"{channel}:{chat_id}"


def payload_units(payload: Any) -> int:
    """Media items are separate transport commands; everything else is one unit."""
    if isinstance(payload, MediaPayload):
        return max(1, len(payload.media))
    return 1


class ServiceEffectProducer:
    """Effect entry point for system producers without a chat participant.

    Cron runs, admin notices, IPC commands and speakups all use it. An unknown source is
    refused loudly; a service principal whose policy rights do not cover the target is
    blocked by the authorizer, exactly like any other principal.
    """

    def __init__(
        self,
        *,
        router: "IntentEffectRouter",
        bus: EffectTransport,
        deadline_key: str = "proactive_ms",
    ) -> None:
        self._router = router
        self._bus = bus
        self._deadline_key = deadline_key

    async def send(
        self,
        *,
        source: str,
        operation_ref: str,
        channel: str,
        chat_id: str,
        content: str,
        capability: str = "send_text",
        reply_to: str | None = None,
    ) -> EffectReceipt | None:
        """Submit one system-produced effect. ``None`` means the legacy path was used."""
        if not self._router.manages(channel, chat_id):
            await self._bus.publish_outbound(
                OutboundMessage(
                    channel=channel, chat_id=chat_id, content=content, reply_to=reply_to
                )
            )
            return None
        principal = SERVICE_PRINCIPALS.get(source)
        if not principal:
            logger.error(
                "refusing system producer without a service principal source={} chat={}",
                source,
                chat_id,
            )
            raise EffectNotDeliveredError(
                f"system source {source!r} has no registered service principal"
            )
        payload = TextPayload(text=content, reply_to=reply_to)
        return await self._router.submit_message(
            OutboundMessage(
                channel=channel,
                chat_id=chat_id,
                content=content,
                reply_to=reply_to,
                metadata={"message_id": operation_ref, "service_source": source},
            ),
            principal=principal,
            capability=capability,
            payload=payload,
        )


def managed_outbound_guard(
    router: "IntentEffectRouter",
) -> Callable[[OutboundMessage], tuple[bool, str]]:
    """Refuse legacy outbound for managed chats unless it carries effect provenance.

    This is the runtime half of "one producer per chat and turn": a producer that was not
    migrated cannot silently keep sending for an activated chat.
    """

    def _guard(message: OutboundMessage) -> tuple[bool, str]:
        if not router.manages(message.channel, message.chat_id):
            return True, "unmanaged"
        metadata = dict(message.metadata or {})
        effect_id = str(metadata.get(EFFECT_PROVENANCE_KEY) or "")
        if effect_id and router.effect_covers(
            effect_id, channel=message.channel, chat_id=message.chat_id
        ):
            return True, "effect"
        if effect_id:
            return False, "forged_effect_provenance"
        return False, "legacy_outbound_without_effect"

    return _guard


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
        budget: SendBudget | None = None,
        turn_provider: Callable[[str, str], Any] | None = None,
    ) -> None:
        self._gateway = gateway
        self._config = config
        self._clock = clock or _now_ms
        self._worker_id = worker_id
        self._turn_provider = turn_provider
        # Review F02: a final reply is dispatched after the generation scope closed, so the
        # chat's *active* turn may already belong to a newer thread. The turn a generation
        # was frozen with is remembered per source message and preferred over that heuristic.
        self._frozen_turns: OrderedDict[str, Any] = OrderedDict()
        self._frozen_turn_cap = 256
        processing = getattr(config, "processing", None)
        budgets = getattr(processing, "budgets", None)
        self._budget = budget or (
            SendBudget(
                units=int(budgets.chat_hard_units),
                window_seconds=int(budgets.chat_hard_window_seconds),
                waiting_cap=int(budgets.outbox_waiting_per_chat),
            )
            if budgets is not None
            else None
        )
        # Plan 06: soft fairness per thread. Measuring is always on; refusing is opt-in,
        # because without a re-queue worker a refusal would drop a reply rather than
        # delay it (`processing.budgets.threadSoftEnforce`).
        self._thread_budget = (
            ThreadBudget(
                limit=int(budgets.thread_soft_units),
                window_ms=int(budgets.thread_soft_window_seconds) * 1000,
            )
            if budgets is not None
            else None
        )
        self._soft_enforce = bool(getattr(budgets, "thread_soft_enforce", False))
        # Plan 06: the durable reservation is authoritative when a store is available -
        # it survives a restart and cannot spend the same attempt twice.
        store = getattr(self._gateway, "store", None)
        self._durable_budget = (
            ChatBudget(
                store,
                limit=int(budgets.chat_hard_units),
                window_ms=int(budgets.chat_hard_window_seconds) * 1000,
                waiting_cap=int(budgets.outbox_waiting_per_chat),
            )
            if budgets is not None and store is not None and budget is None
            else None
        )

    @property
    def store(self) -> Any:
        """The processing store behind this router, or ``None`` when there is none.

        Callers that must journal something before acting — the A2A delegation tool claims
        its idempotency key here — resolve the store through this property instead of
        reaching into the gateway, so the wiring cannot silently degrade to "no contract".
        """
        return getattr(self._gateway, "store", None)

    def set_direct_transport(self, outbound: Any, reaction: Any) -> None:
        """Install the confirming channel transport on the gateway's executor."""
        self._gateway.set_direct_senders(outbound, reaction)

    def effect_covers(self, effect_id: str, channel: str, chat_id: str) -> bool:
        """True only for a real persisted effect whose target is this chat."""
        stored = self._gateway.store.get_effect(effect_id)
        target = getattr(stored, "target", None)
        return bool(
            stored is not None
            and target is not None
            and target.channel == channel
            and target.chat_id == chat_id
        )

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
        """Take a reaction over - or refuse it.

        The return value means "do not publish this yourself", not "it was sent". A
        model-chosen emoji the owner has not approved is consumed here *without* an
        effect, so the caller's direct-publish fallback cannot leak an unapproved face
        onto the wire (routing spec; owner decision: no guessed emoji, no text instead).
        """
        emoji = intent.emoji
        if intent.origin != SYSTEM_ORIGIN:
            emoji = allowed_reaction(intent.emoji, self._allowed_reaction_emojis())
            if emoji is None:
                logger.warning(
                    "reaction_dropped stage=dispatcher channel={} chat={} message_id={} "
                    "origin={} value={}",
                    intent.channel,
                    intent.chat_id,
                    intent.message_id,
                    intent.origin,
                    str(intent.emoji)[:16],
                )
                return True
        if not self.manages(intent.channel, intent.chat_id):
            return False
        await self._run(
            channel=intent.channel,
            chat_id=intent.chat_id,
            principal=principal,
            payload=ReactionPayload(message_id=intent.message_id, emoji=emoji),
            operation_key=f"reaction:{intent.channel}:{intent.chat_id}:{intent.message_id}:{emoji}",
            trace_id=intent.message_id,
            deadline_key="semantic_reaction_ms",
            own_lineage=True,
            source_message_id=intent.message_id,
        )
        return True

    async def submit_message(
        self,
        message: OutboundMessage,
        *,
        principal: str,
        capability: str,
        payload: Any,
    ) -> EffectReceipt:
        """One entry point for tool/turn producers that used to publish directly."""
        metadata = dict(message.metadata or {})
        source = str(metadata.get("message_id") or "turn")
        return await self._run(
            channel=message.channel,
            chat_id=message.chat_id,
            source_message_id=str(metadata.get("message_id") or "") or None,
            principal=principal,
            payload=payload,
            operation_key=(
                f"{capability}:{message.channel}:{message.chat_id}:{source}:"
                f"{canonical_hash(payload_to_mapping(payload))[:12]}"
            ),
            trace_id=str(metadata.get("trace_id") or source),
            deadline_key=(
                "semantic_reaction_ms" if capability == "send_reaction" else "reactive_ms"
            ),
        )

    def remember_turn_for_source(self, source_message_id: str, binding: Any) -> None:
        """Record which turn a generation was frozen with, for its final reply."""
        key = str(source_message_id or "")
        if not key or binding is None:
            return
        self._frozen_turns[key] = binding
        self._frozen_turns.move_to_end(key)
        while len(self._frozen_turns) > self._frozen_turn_cap:
            self._frozen_turns.popitem(last=False)

    def _allowed_reaction_emojis(self) -> tuple[str, ...]:
        """The owner's reaction vocabulary, read per call so a config change takes effect."""
        processing = getattr(self._config, "processing", None)
        approved = getattr(processing, "reaction_emojis", None)
        return tuple(str(item) for item in approved) if approved is not None else ()

    def frozen_turn_for_source(self, source_message_id: str) -> Any | None:
        return self._frozen_turns.get(str(source_message_id or ""))

    async def _run(
        self,
        *,
        channel: str,
        source_message_id: str | None = None,
        chat_id: str,
        principal: str,
        payload: Any,
        operation_key: str,
        trace_id: str,
        deadline_key: str,
        own_lineage: bool = False,
    ) -> EffectReceipt:
        now = self._clock()
        processing = self._config.processing
        deadlines = getattr(processing, "deadlines", None) or getattr(
            processing, "Deadlines", None
        )
        deadline_ms = int(getattr(deadlines, deadline_key))

        binding = CURRENT_TURN.get()
        frozen = None
        if binding is None and source_message_id:
            frozen = self.frozen_turn_for_source(source_message_id)
            binding = frozen
        if own_lineage and source_message_id and binding is None:
            # A reaction belongs to the message it reacts to. That message's own recorded
            # assignment is its lineage - never the chat's currently active turn, which may
            # belong to an unrelated order (routing spec, criterion 8).
            assignment = self._gateway.store.event_assignment(source_message_id)
            if assignment is not None and assignment[1]:
                target_turn = self._gateway.store.get_turn(str(assignment[1]))
                if target_turn is not None:
                    binding = TurnBinding(
                        turn=target_turn, trace_id=source_message_id, generation_id=None
                    )
        turn = getattr(binding, "turn", None)
        if turn is None and frozen is None and not own_lineage and self._turn_provider is not None:
            # Only when nothing is known about this source may the chat's active turn be
            # used; otherwise a newer thread would silently adopt an older answer.
            turn = self._turn_provider(channel, chat_id)
        if binding is not None and getattr(binding, "trace_id", ""):
            trace_id = binding.trace_id
        turn_id = getattr(turn, "turn_id", "") or ""
        turn_revision = int(getattr(turn, "revision", 1) or 1)

        if (
            not turn_id
            and not own_lineage
            and self._turn_provider is not None
            and principal not in SERVICE_PRINCIPALS.values()
        ):
            # A turn-bound producer without a turn must not queue anything: autorisation
            # would otherwise be checked without any revision to compare against.
            raise EffectNotDeliveredError(
                "no turn bound to this producer; refusing to queue an effect"
            )

        self._note_soft_thread_limit(
            thread_id=str(getattr(turn, "thread_id", "") or ""), now_ms=now
        )

        envelope = EffectEnvelope(
            effect_id=uuid.uuid4().hex,
            operation_key=f"{operation_key}:{turn_id}:{turn_revision}",
            payload=payload,
            target=EffectTarget(channel=channel, chat_id=chat_id),
            trace_id=trace_id,
            turn_id=turn_id,
            turn_revision=turn_revision,
            principal=principal,
            capability=CAPABILITY_BY_KIND[payload.kind],
            expires_at_ms=now + deadline_ms,
            created_ms=now,
        )
        receipt = self._gateway.submit(envelope)
        self._plan_quotable_message(envelope, turn=turn, now=now)

        blocked = self._capacity_block(channel, chat_id, payload, receipt.effect_id)
        if blocked is not None:
            return blocked

        result = await self._gateway.execute_ready(receipt.effect_id)
        self._confirm_quotable_message(envelope, result, now=now)
        logger.info(
            "routing_effect effect_id={} state={} chat={} turn_id={} revision={} detail={}",
            receipt.effect_id,
            result.state,
            chat_id,
            turn_id or "-",
            turn_revision,
            getattr(result, "detail", None) or "-",
        )
        self._close_ambient_turn(turn_id, now=now)
        return self._log_undelivered(result, envelope, chat_id)

    def _plan_quotable_message(self, envelope: EffectEnvelope, *, turn: Any, now: int) -> None:
        """Reserve the anchor a later reply to this message has to resolve to.

        A reply can only continue a thread when the replied-to bot message is a *confirmed*
        anchor (spec: bot messages must be proven as sent). The row is created with the
        effect id and filled in with the provider id once the transport reports it, so a
        planned effect never becomes quotable.
        """
        thread_id = str(getattr(turn, "thread_id", "") or "")
        if not thread_id or envelope.payload.kind not in {"text", "media"}:
            return
        store = getattr(self._gateway, "store", None)
        register = getattr(store, "register_thread_message", None)
        if register is None:
            return
        try:
            register(
                thread_id=thread_id,
                turn_id=str(getattr(turn, "turn_id", "") or "") or None,
                direction="out",
                effect_id=envelope.effect_id,
                now_ms=int(now),
            )
        except Exception as exc:  # pragma: no cover - defensive, like other store hooks
            logger.debug("thread_message_plan_skipped effect={} error={}", envelope.effect_id, exc)

    def _confirm_quotable_message(self, envelope: EffectEnvelope, result: Any, *, now: int) -> None:
        """Attach the provider message id the transport reported, if it reported one.

        The id is read from the persisted receipt: the gateway records it before it reports
        ``sent``, while the receipt it hands back to the caller does not carry it.
        """
        store = getattr(self._gateway, "store", None)
        attach = getattr(store, "attach_confirmed_message_id", None)
        if attach is None:
            return
        provider_id = str(
            getattr(getattr(result, "transport_receipt", None), "provider_message_id", "") or ""
        )
        if not provider_id:
            reader = getattr(store, "effect_transport_receipt", None)
            if reader is not None:
                try:
                    stored_receipt = reader(envelope.effect_id)
                except Exception:  # pragma: no cover - defensive
                    stored_receipt = None
                provider_id = str(getattr(stored_receipt, "provider_message_id", "") or "")
        if not provider_id:
            return
        try:
            attach(envelope.effect_id, provider_id, int(now))
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("thread_message_confirm_skipped effect={} error={}", envelope.effect_id, exc)

    def _close_ambient_turn(self, turn_id: str, *, now: int) -> None:
        """An ambient order ends with its answer: turn and thread close right away.

        Without this, ambient turns would stay open and look like durable work (routing
        spec: an ambient answer is short-lived and never accumulates).
        """
        if not turn_id:
            return
        try:
            turn = self._gateway.store.get_turn(turn_id)
            if turn is None:
                return
            thread = self._gateway.store.get_thread(turn.thread_id)
            if thread is None or str(getattr(thread, "kind", "")) != "ambient":
                return
            self._gateway.store.close_turn(turn_id, now_ms=int(now), state="closed")
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("ambient turn close skipped turn={} error={}", turn_id, exc)

    def _note_soft_thread_limit(self, *, thread_id: str, now_ms: int) -> None:
        """Soft fairness: measure what the limit would defer, and enforce only on request.

        The limit is not a permission decision - it decides *when* a thread may speak.
        Enforcement stays off until a due effect can be re-queued: refusing here without a
        re-queue worker would drop a reply from a chatty thread instead of delaying it.
        """
        budget = self._thread_budget
        if budget is None or not thread_id:
            return
        ready_at = budget.defer_until(thread_id=thread_id, now_ms=now_ms)
        timings = getattr(self, "_timings", None)
        if ready_at > now_ms:
            if timings is not None:
                timings.note_deferral("thread_soft_limit")
            if self._soft_enforce:
                raise EffectNotDeliveredError(
                    f"thread soft limit reached (thread={thread_id} ready_at_ms={ready_at})"
                )
        budget.note_send(thread_id=thread_id, now_ms=now_ms)

    def _capacity_block(
        self, channel: str, chat_id: str, payload: Any, effect_id: str
    ) -> EffectReceipt | None:
        """Refuse to dispatch when the chat is over its budget or its outbox is full.

        The effect stays recorded as ``blocked`` with a reason instead of disappearing.
        """
        if self._budget is None:
            return None
        now = self._clock()
        waiting = self._gateway.store.count_waiting_effects(
            channel=channel, chat_id=chat_id
        )
        units = payload_units(payload)
        if waiting > self._budget.waiting_cap:
            reason = "queue_capacity"
        elif self._durable_budget is not None:
            record = self._gateway.store.get_effect(effect_id)
            stamp = int(getattr(record, "updated_ms", 0) or 0)
            decision = self._durable_budget.reserve(
                channel=channel,
                chat_id=chat_id,
                effect_id=effect_id,
                attempt_id=f"{effect_id}:{stamp}",
                units=units,
                now_ms=now,
            )
            if decision.allowed:
                return None
            reason = decision.reason or "budget_exhausted"
        elif not self._budget.reserve(budget_key(channel, chat_id), units):
            reason = "budget_exhausted"
        else:
            return None
        self._gateway.store.transition(
            effect_id,
            expected="queued",
            target="blocked",
            now_ms=now,
            evidence={"kind": "policy", "detail": reason},
        )
        logger.warning(
            "effect blocked effect_id={} reason={} chat={}", effect_id, reason, chat_id
        )
        return self._gateway.store.get_effect(effect_id) and self._gateway._receipt(
            effect_id,
            "blocked",
            "",
            detail=reason,
        )

    def _log_undelivered(self, result: Any, envelope: Any, chat_id: str) -> None:
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


def _receipt_from_report(
    reported: Any, message: Any
) -> "TransportReceipt | None":
    """Normalise what a channel reported into a transport receipt."""
    if not isinstance(reported, Mapping):
        return None
    provider_message_id = reported.get("provider_message_id")
    client_message_id = reported.get("client_message_id")
    if not provider_message_id and not client_message_id:
        return None
    return TransportReceipt(
        channel=str(getattr(message, "channel", "") or ""),
        chat_id=str(getattr(message, "chat_id", "") or ""),
        provider_message_id=(
            str(provider_message_id) if provider_message_id else None
        ),
        client_message_id=str(client_message_id) if client_message_id else None,
        confirmed_ms=0,
        detail=str(reported.get("detail") or "") or None,
    )
