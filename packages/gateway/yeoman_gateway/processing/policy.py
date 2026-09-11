"""Versioned policy snapshots, the fast gate and final effect authorization.

Spec R02: *Fast Policy* separates **access denial** from **no reactive reason**. Access
denial stops enrichment, the response model and tools. "No reactive reason" may continue
permitted ambient observation without producing a reactive turn.

Every check returns a decision id, the hash of the policy that was actually loaded, the
principal, target, capability, turn revision and a reason. The version is published
together with the validated engine - never by re-reading the file while a different
in-memory engine decides (spec R02).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from loguru import logger

from yeoman_gateway.core.models import InboundEvent, PolicyDecision
from yeoman_gateway.processing.models import (
    CanonicalEvent,
    DecisionRecord,
    EffectEnvelope,
    EffectTarget,
    PolicySnapshot,
    ProcessingError,
    TurnRef,
)
from yeoman_gateway.processing.models import (
    now_ms as _now_ms,
)
from yeoman_gateway.processing.store import ProcessingStore
from yeoman_gateway.processing.threads import JoinRule

if TYPE_CHECKING:
    from yeoman_gateway.policy.engine import ActorContext, PolicyEngine

#: Transport capabilities are gated by chat admission alone.
TRANSPORT_CAPABILITIES = frozenset({"send_text", "send_media", "send_reaction"})
#: Capabilities that additionally require an explicit tool permission in policy.
TOOL_CAPABILITIES: Mapping[str, str] = {
    "send_voice": "send_voice",
    "delete_message": "delete_message",
    "message": "message",
}
#: Everything else must be named here or it is refused: no capability falls through.
EXTERNAL_CAPABILITIES: Mapping[str, str] = {}


def effect_guard(
    *,
    policy_healthy: bool,
    permitted: bool,
    revision_matches: bool,
    unexpired: bool,
) -> str:
    """Final pre-dispatch gate. Returns ``allow`` or the blocking reason."""
    if not policy_healthy:
        return "policy_unhealthy"
    if not revision_matches:
        return "superseded"
    if not unexpired:
        return "expired"
    if not permitted:
        return "permission_denied"
    return "allow"


# --------------------------------------------------------------------------------------
# Snapshots
# --------------------------------------------------------------------------------------


@runtime_checkable
class PolicySnapshotProvider(Protocol):
    """Delivers the currently effective policy content and its identity."""

    def snapshot(self) -> PolicySnapshot: ...


class AdapterSnapshotProvider:
    """Snapshot provider backed by the live policy adapter.

    The adapter owns the loaded engine; this provider never touches the policy file, so
    a decision can never be attributed to bytes that were not the ones evaluated.
    """

    def __init__(self, adapter: Any) -> None:
        self._adapter = adapter

    def snapshot(self) -> PolicySnapshot:
        provider = getattr(self._adapter, "policy_snapshot", None)
        if provider is None:
            raise ProcessingError("policy adapter does not expose policy_snapshot()")
        return provider()


# --------------------------------------------------------------------------------------
# Fast gate
# --------------------------------------------------------------------------------------


class FastGateOutcome(StrEnum):
    """Outcome of the pre-enrichment check."""

    DENY = "deny"
    OBSERVE = "observe"
    REACT = "react"


@dataclass(frozen=True, slots=True)
class IngestRequest:
    """Canonical base data of one inbound provider event, before enrichment."""

    event_key: str
    event_id: str
    trace_id: str
    event: InboundEvent
    payload_extra: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class FastGateResult:
    """Result of the fast gate including the decision that produced it."""

    outcome: FastGateOutcome
    reason: str
    snapshot: PolicySnapshot
    decision: DecisionRecord | None = None
    policy_decision: PolicyDecision | None = None
    journaled_event_id: str | None = None
    shadow: bool = False
    assignment: Any = None
    #: The chat's configured reply action, so the channel knows whether this message is
    #: answered, reacted to or silenced without re-reading the configuration.
    reply_action: str = "answer"
    #: True when this message would have been answered and the action turned that answer
    #: into a reaction. Observed messages stay observed - no reaction, no acknowledgement.
    react: bool = False
    #: True when this unaddressed message passed the ambient brake and now waits for the
    #: judge's verdict. Nothing is generated until that verdict is a yes.
    ambient_candidate: bool = False

    @property
    def denied(self) -> bool:
        return self.outcome is FastGateOutcome.DENY

    @property
    def proceed(self) -> bool:
        return self.outcome is not FastGateOutcome.DENY


class IngestGate:
    """Canonical ingest -> journal -> fast policy, all before expensive work.

    Only canonical base data and limited text/payload data are accepted here. No
    transcription, vision or model call happens before this gate has decided.
    """

    def __init__(
        self,
        *,
        config: Any,
        store: ProcessingStore | None,
        snapshots: PolicySnapshotProvider,
        evaluate: Callable[[IngestRequest], PolicyDecision],
        threads: Any = None,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self._config = config
        self._store = store
        self._snapshots = snapshots
        self._evaluate = evaluate
        self._threads = threads
        self._clock = clock or _now_ms
        #: Ambient brake state, per chat: when the last unaddressed answer went out and how
        #: much the chat has moved since. In memory on purpose - after a restart the brake
        #: simply starts cold, which is the conservative direction.
        self._ambient_state: dict[str, dict[str, int]] = {}
        #: Ambient messages waiting for the judge: observed, not answerable until cleared.
        self._ambient_pending: dict[str, int] = {}

    def enabled_for(self, channel: str, chat_id: str) -> bool:
        """True when the new mode owns this chat; unmanaged chats stay on legacy."""
        checker = getattr(self._config, "is_chat_enabled", None)
        if checker is None:
            return bool(getattr(self._config, "enabled", False))
        return bool(checker(channel, chat_id))

    def shadowed(self, channel: str, chat_id: str) -> bool:
        """True when the chat is observed only: decide and journal, never act."""
        checker = getattr(self._config, "is_chat_shadowed", None)
        if checker is None:
            return False
        return bool(checker(channel, chat_id))

    def admit(self, request: IngestRequest) -> FastGateResult | None:
        """Journal the event and decide before any enrichment. ``None`` = not managed.

        A shadow chat is journaled and decided like a managed one, but the verdict never
        stops or redirects the traffic: shadow comparison must not change what the user
        sees and must not create effects (spec section 5).
        """
        event = request.event
        shadow = self.shadowed(event.channel, event.chat_id)
        if not shadow and not self.enabled_for(event.channel, event.chat_id):
            return None

        now = self._clock()
        snapshot = self._snapshots.snapshot()
        journaled = self._journal(request, now=now)

        if shadow:
            decision = self._evaluate(request) if snapshot.healthy else None
            outcome = (
                FastGateOutcome.REACT
                if decision is not None and decision.accept_message and decision.should_respond
                else FastGateOutcome.OBSERVE
            )
            return self._record(
                request,
                snapshot,
                outcome=outcome,
                reason="shadow",
                policy_decision=decision,
                journaled=journaled,
                now=now,
                shadow=True,
            )

        if not snapshot.healthy:
            return self._record(
                request,
                snapshot,
                outcome=FastGateOutcome.DENY,
                reason="policy_unhealthy",
                policy_decision=None,
                journaled=journaled,
                now=now,
            )

        decision = self._evaluate(request)
        if not decision.accept_message:
            return self._record(
                request,
                snapshot,
                outcome=FastGateOutcome.DENY,
                reason="permission_denied",
                policy_decision=decision,
                journaled=journaled,
                now=now,
            )
        outcome = (
            FastGateOutcome.REACT if decision.should_respond else FastGateOutcome.OBSERVE
        )
        reply_action = self._reply_action(request)
        react = outcome is FastGateOutcome.REACT and reply_action == "react"
        if outcome is FastGateOutcome.REACT and reply_action != "answer":
            # `silence` and `react` both withdraw the answer turn: no turn, no effect from
            # the answer path, no typing indicator. Silence sends nothing at all; `react`
            # sends one reaction from its own path (routing spec, use cases 2 and 3). The
            # action can only take an answer away, never grant one.
            logger.debug(
                "reply_action_withdrawn chat={} event_id={} action={}",
                request.event.chat_id,
                request.event_id,
                reply_action,
            )
            outcome = FastGateOutcome.OBSERVE
        ambient_candidate = False
        if outcome is FastGateOutcome.REACT and self._is_ambient_chat(event.channel, event.chat_id):
            # An unaddressed message in a chat the owner released for ambient answers. Ask
            # the thread engine what it *would* decide, without persisting anything: only
            # the ambient fallback is subject to the brake, a real continuation is not.
            if self._ambient_rule_without_turn(request, now=now):
                self._note_ambient_message(event.channel, event.chat_id)
                named = self._mentions_bot_by_name(request)
                if named:
                    # Naming him is a soft address even when the sentence is no request: the
                    # brake is skipped and the judge decides right away (owner decision,
                    # 11.09.). Still one small call per name-drop - never an answer without
                    # a yes, and never a mechanical acknowledgement.
                    allowed, reason = True, "name"
                else:
                    allowed, reason = self._ambient_brake_allows(
                        event.channel, event.chat_id, now=now
                    )
                ambient_candidate = allowed
                # Observed either way; the judge may upgrade it after a yes.
                outcome = FastGateOutcome.OBSERVE
                self._mark_ambient_pending(request.event_id, now=now)
                if named:
                    logger.debug(
                        "ambient_name_bypass chat={} event_id={}",
                        event.chat_id,
                        request.event_id,
                    )
                elif not allowed:
                    logger.debug(
                        "ambient_brake chat={} event_id={} reason={}",
                        event.chat_id,
                        request.event_id,
                        reason,
                    )
        assignment = self._assign(request, now=now, allow_turn=outcome is FastGateOutcome.REACT)
        if assignment is not None:
            # One observation line per message (routing spec, criterion 12): what the
            # message was classified as, how many candidates existed, which continuity
            # signal proved the attachment, and what was decided. Never any content.
            logger.info(
                "routing_decision chat={} event_id={} classification={} candidates={} "
                "eligible={} topic_break={} continuity={} evidence={} outcome={} "
                "reply_action={} thread_id={} turn_id={} rule={} reason={}",
                event.chat_id,
                request.event_id,
                "assigned" if assignment.thread_id else "no_thread",
                getattr(assignment, "candidates_checked", 0),
                getattr(assignment, "candidates_eligible", 0),
                getattr(assignment, "topic_break", "unknown"),
                getattr(assignment, "continuity_kind", "none"),
                ",".join(getattr(assignment, "continuity_evidence", ()) or ()) or "-",
                outcome.value,
                self._reply_action(request),
                assignment.thread_id or "-",
                assignment.turn_id or "-",
                assignment.rule,
                getattr(assignment, "reason", "") or "-",
            )
        return self._record(
            request,
            snapshot,
            outcome=outcome,
            reason="allow" if decision.should_respond else "observe",
            policy_decision=decision,
            journaled=journaled,
            now=now,
            assignment=assignment,
            reply_action=reply_action,
            react=react,
            ambient_candidate=ambient_candidate,
        )

    # -- internals ---------------------------------------------------------------------

    def _reply_action(self, request: IngestRequest) -> str:
        """The configured action for this chat: ``answer`` unless configured otherwise."""
        return self._reply_action_for(request.event.channel, request.event.chat_id)
    def _reply_action_for(self, channel: str, chat_id: str) -> str:
        """``answer``, ``react`` or ``silence`` - never a silent fallback for a typo.

        An unknown value would be a switch that looks set but does nothing, so it degrades
        to ``answer`` and says so once per occurrence.
        """
        actions = getattr(self._config, "reply_actions", None) or {}
        if not isinstance(actions, Mapping):
            return "answer"
        key = f"{channel}:{chat_id}"
        value = str(actions.get(key, "answer") or "answer").strip().lower()
        if value in {"answer", "react", "silence"}:
            return value
        logger.warning("reply_action_unknown chat={} value={}", chat_id, value[:24])
        return "answer"

    # -- ambient brake ---------------------------------------------------------------

    def _ambient_settings(self) -> Any:
        return getattr(self._config, "ambient", None)

    def _is_ambient_chat(self, channel: str, chat_id: str) -> bool:
        """True when the owner released this chat for ambient answers at all."""
        chats = getattr(self._config, "ambient_chats", None) or ()
        wanted = {str(entry).strip() for entry in chats if str(entry).strip()}
        return f"{channel}:{chat_id}" in wanted

    def _ambient_rule_without_turn(self, request: IngestRequest, *, now: int) -> bool:
        """True when the thread engine would file this message as plain ambient.

        A dry classification: nothing is persisted, so a message that still has to earn its
        answer never gets a turn in the meantime. A continuation with a continuity signal
        is not ambient and is answered as before.
        """
        threads = self._threads
        if threads is None:
            return False
        try:
            data = threads.input_from_event(self._canonical_event(request))
            view = threads.view_for(data, now_ms=now)
            decision = threads.decide(data, view)
        except Exception as exc:  # pragma: no cover - defensive, like _assign
            logger.warning(
                "ambient_classification_failed event_id={} error_type={}",
                request.event_id,
                type(exc).__name__,
            )
            return False
        rule = getattr(decision, "rule", None)
        is_ambient = rule == JoinRule.AMBIENT or str(rule) == str(JoinRule.AMBIENT)
        return bool(is_ambient and getattr(decision, "thread_id", None) is None)

    @staticmethod
    def _mentions_bot_by_name(request: IngestRequest) -> bool:
        """True when the message names the bot, request or not (owner decision 11.09.).

        Deliberately narrower than an address: it does not turn the message into an order,
        it only lets the judge look at it without waiting for the brake window.
        """
        from yeoman_gateway.implicit_addressing import contains_bot_name

        text = str(getattr(request.event, "content", "") or "")
        return bool(text and contains_bot_name(text))

    def _note_ambient_message(self, channel: str, chat_id: str) -> None:
        """Count one more message the chat produced since its last ambient answer."""
        state = self._ambient_state.setdefault(f"{channel}:{chat_id}", {"messages": 0, "last": 0})
        state["messages"] = int(state.get("messages", 0)) + 1

    def _ambient_brake_allows(self, channel: str, chat_id: str, *, now: int) -> tuple[bool, str]:
        """Whether the judge may be asked at all: enough silence *and* enough new chatter."""
        settings = self._ambient_settings()
        min_seconds = int(getattr(settings, "min_seconds_between_answers", 300) or 0)
        min_messages = int(getattr(settings, "min_messages_since_answer", 6) or 0)
        state = self._ambient_state.setdefault(f"{channel}:{chat_id}", {"messages": 0, "last": 0})
        last = int(state.get("last", 0) or 0)
        if last and min_seconds:
            elapsed = max(0, now - last) / 1000.0
            if elapsed < min_seconds:
                return False, f"waiting:{int(min_seconds - elapsed)}s"
        if int(state.get("messages", 0)) < min_messages:
            return False, f"quiet:{state.get('messages', 0)}/{min_messages}"
        return True, "due"

    def _mark_ambient_pending(self, event_id: str, *, now: int) -> None:
        """Remember that this ambient message still needs its verdict before it may speak."""
        self._ambient_pending[str(event_id)] = now
        if len(self._ambient_pending) > 512:
            for key, _ in sorted(self._ambient_pending.items(), key=lambda item: item[1])[:128]:
                self._ambient_pending.pop(key, None)

    def is_ambient_pending(self, event_id: str) -> bool:
        return str(event_id) in self._ambient_pending

    def note_ambient_answer(self, event_id: str, *, now: int | None = None) -> None:
        """The judge said yes: clear the message, restart the brake window."""
        self._ambient_pending.pop(str(event_id), None)
        self._reset_ambient_window(now)

    def note_ambient_declined(self, event_id: str, *, now: int | None = None) -> None:
        """The judge said no: the message stays observed, and the window starts over.

        Restarting the window is what keeps the judge cheap: without it every further
        message of a busy chat would be judged again, because the thresholds stay met. The
        message itself stays pending - a declined message must never be answered later by
        the classic pipeline.
        """
        self._reset_ambient_window(now)

    def _reset_ambient_window(self, now: int | None = None) -> None:
        moment = int(now if now is not None else self._clock())
        for state in self._ambient_state.values():
            state["messages"] = 0
            state["last"] = moment

    def _assign(self, request: IngestRequest, *, now: int, allow_turn: bool) -> Any:
        """Attach the canonical event to its thread; never runs for a denied event.

        A failing assignment degrades this event to the chat-scoped path and logs
        ``threads_degraded``. It deliberately does not drop the message: answering without
        a thread is better for a live chat than silence, and no effect is created here.
        """
        if self._threads is None or self._store is None:
            return None
        try:
            return self._threads.assign(
                self._canonical_event(request), now_ms=now, allow_turn=allow_turn
            )
        except Exception as exc:
            logger.warning(
                "threads_degraded event_id={} chat={} error_type={}",
                request.event_id,
                request.event.chat_id,
                type(exc).__name__,
            )
            return None

    def reconcile_reply(self, event: InboundEvent) -> Any:
        """Upgrade a permitted reply event to a durable thread and turn."""
        if self._store is None or self._threads is None:
            return None
        message_id = str(event.message_id or "").strip()
        if not message_id:
            return None
        if self._reply_action_for(event.channel, event.chat_id) != "answer":
            # Second line of defence: nothing may quietly open a turn for a withdrawn
            # answer, whoever calls this.
            return None
        request = IngestRequest(
            event_key=f"{event.channel}:{event.chat_id}:{message_id}",
            event_id=message_id,
            trace_id=f"{event.channel}:{event.chat_id}:{message_id}",
            event=event,
        )
        assignment = self._assign(request, now=self._clock(), allow_turn=True)
        if assignment is not None and assignment.thread_id and assignment.turn_id:
            logger.info(
                "reply_assignment_reconciled chat={} event_id={} thread_id={} turn_id={}",
                event.chat_id,
                message_id,
                assignment.thread_id,
                assignment.turn_id,
            )
            return assignment
        logger.warning(
            "reply_assignment_failed chat={} event_id={} reason=no_turn",
            event.chat_id,
            message_id,
        )
        return None

    def admit_reply(self, event: InboundEvent) -> bool:
        """Ensure managed replies have a turn before generation or typing.

        This is the last station before the typing indicator and the provider call, so a
        withdrawn answer has to be refused *here*: reconciliation opens a turn on its own,
        and a veto that only lives in the fast gate would be answered anyway.
        """
        if not self.enabled_for(event.channel, event.chat_id) or self.shadowed(
            event.channel, event.chat_id
        ):
            return True
        action = self._reply_action_for(event.channel, event.chat_id)
        if action != "answer":
            logger.debug(
                "reply_admission_refused chat={} message_id={} action={}",
                event.chat_id,
                event.message_id,
                action,
            )
            return False
        if self.is_ambient_pending(str(event.message_id or "")):
            # Unaddressed in a chat with ambient answers: it may only speak once the judge
            # cleared it. Until then it is context, not an order - no typing, no call.
            logger.debug(
                "ambient_admission_refused chat={} message_id={}", event.chat_id, event.message_id
            )
            return False
        return self.reconcile_reply(event) is not None

    def _canonical_event(self, request: IngestRequest) -> CanonicalEvent:
        event = request.event
        payload: dict[str, Any] = {
            "kind": "message",
            "text": event.content,
            "is_group": event.is_group,
            "mentioned_bot": event.mentioned_bot,
            "reply_to_bot": event.reply_to_bot,
            "reply_to_message_id": event.raw_metadata.get("reply_to_message_id"),
            "media": list(event.media),
        }
        payload.update(dict(request.payload_extra))
        return CanonicalEvent(
            event_id=request.event_id,
            event_key=request.event_key,
            trace_id=request.trace_id,
            kind="message",
            origin=event.channel,
            principal=event.sender_id,
            channel=event.channel,
            chat_id=event.chat_id,
            occurred_ms=int(event.timestamp.timestamp() * 1000),
            source_message_id=event.message_id,
            payload=payload,
        )

    def _journal(self, request: IngestRequest, *, now: int) -> str | None:
        if self._store is None:
            return None
        canonical = self._canonical_event(request)
        return self._store.append_event(
            event_key=canonical.event_key,
            event_id=canonical.event_id,
            trace_id=canonical.trace_id,
            payload=canonical,
            now_ms=now,
        )

    def _record(
        self,
        request: IngestRequest,
        snapshot: PolicySnapshot,
        *,
        outcome: FastGateOutcome,
        reason: str,
        policy_decision: PolicyDecision | None,
        journaled: str | None,
        now: int,
        shadow: bool = False,
        assignment: Any = None,
        reply_action: str = "answer",
        react: bool = False,
        ambient_candidate: bool = False,
    ) -> FastGateResult:
        event = request.event
        decision = DecisionRecord(
            decision_id=uuid.uuid4().hex,
            trace_id=request.trace_id,
            policy_version=snapshot.version,
            policy_hash=snapshot.policy_hash,
            principal=event.sender_id,
            target=f"{event.channel}:{event.chat_id}",
            capability="inbound.message",
            turn_revision=1,
            outcome="allow" if outcome is not FastGateOutcome.DENY else "deny",
            reason=reason,
            created_ms=now,
            stage="fast",
        )
        if self._store is not None:
            self._store.record_decision(decision)
        return FastGateResult(
            outcome=outcome,
            reason=reason,
            snapshot=snapshot,
            decision=decision,
            policy_decision=policy_decision,
            journaled_event_id=journaled,
            shadow=shadow,
            assignment=assignment,
            reply_action=reply_action,
            react=react,
            ambient_candidate=ambient_candidate,
        )


# --------------------------------------------------------------------------------------
# Final effect authorization
# --------------------------------------------------------------------------------------


@runtime_checkable
class CapabilityResolver(Protocol):
    """Decides whether a principal may cause one capability at one target."""

    def resolve(
        self, *, principal: str, target: EffectTarget, capability: str
    ) -> tuple[bool, str]: ...


class DenyAllCapabilities:
    """Default resolver: without an explicit policy binding nothing is permitted."""

    def __init__(self, reason: str = "permission_denied") -> None:
        self._reason = reason

    def resolve(
        self, *, principal: str, target: EffectTarget, capability: str
    ) -> tuple[bool, str]:
        return False, self._reason


class PolicyCapabilityResolver:
    """Capability resolver backed by the loaded policy engine.

    Uses the existing policy engine methods: the actor is treated as an addressed
    participant of the target chat, so ``who_can_talk``/``blocked_senders`` decide
    admission and tool permissions decide capability-specific rights. A service
    principal is only admitted when policy names it explicitly - no fictional owner.
    """

    def __init__(
        self, *, engine_provider: Callable[[], "PolicyEngine | None"], known_tools: Callable[[], set[str]]
    ) -> None:
        self._engine_provider = engine_provider
        self._known_tools = known_tools

    def resolve(
        self, *, principal: str, target: EffectTarget, capability: str
    ) -> tuple[bool, str]:
        engine = self._engine_provider()
        if engine is None:
            return False, "policy_unavailable"
        actor = self._actor(principal, target)
        decision = engine.evaluate(actor, set(self._known_tools()))
        if not decision.accept_message:
            return False, "permission_denied"

        if capability in TRANSPORT_CAPABILITIES:
            return True, "allow"
        tool = TOOL_CAPABILITIES.get(capability)
        if tool is None:
            mapped = EXTERNAL_CAPABILITIES.get(capability)
            if mapped is None:
                return False, f"unmapped_capability:{capability}"
            tool = mapped
        allowed = decision.allowed_tools
        if allowed is not None and tool not in allowed:
            return False, f"capability_denied:{tool}"
        return True, "allow"

    @staticmethod
    def _actor(principal: str, target: EffectTarget) -> "ActorContext":
        from yeoman_gateway.policy.engine import ActorContext

        return ActorContext(
            channel=target.channel,
            chat_id=target.chat_id,
            sender_primary=principal,
            sender_aliases=[],
            is_group=_is_group_chat(target.chat_id),
            mentioned_bot=True,
            reply_to_bot=True,
        )


def operator_check(
    engine_provider: Callable[[], "PolicyEngine | None"],
) -> Callable[..., bool]:
    """Owner check for turn authority, sharing the actor construction with the resolver."""

    def _is_operator(*, principal: str, channel: str, chat_id: str) -> bool:
        engine = engine_provider()
        if engine is None or not principal:
            return False
        actor = PolicyCapabilityResolver._actor(
            principal, EffectTarget(channel=channel, chat_id=chat_id)
        )
        return bool(engine.is_owner(actor))

    return _is_operator


class SnapshotEffectAuthorizer:
    """Final synchronous policy check bound to one loaded snapshot.

    Checks principal, target, capability, turn revision and expiry, and reflects a
    known policy reload failure. Missing turn state is not silently accepted.
    """

    def __init__(
        self,
        *,
        snapshots: PolicySnapshotProvider,
        capabilities: CapabilityResolver | None = None,
        turn_lookup: Callable[[str], TurnRef | None] | None = None,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self._snapshots = snapshots
        self._capabilities: CapabilityResolver = capabilities or DenyAllCapabilities()
        self._turn_lookup = turn_lookup
        self._clock = clock or _now_ms

    def check(
        self, envelope: EffectEnvelope, current_turn: TurnRef | None
    ) -> DecisionRecord:
        now = self._clock()
        snapshot = self._snapshots.snapshot()
        healthy = bool(snapshot.healthy)

        permitted = False
        if healthy:
            permitted, permission_reason = self._capabilities.resolve(
                principal=envelope.principal,
                target=envelope.target,
                capability=envelope.capability,
            )
        else:
            permission_reason = "policy_unhealthy"

        revision_matches, revision_reason = self._revision_matches(envelope, current_turn)
        unexpired = envelope.expires_at_ms is None or envelope.expires_at_ms > now

        reason = effect_guard(
            policy_healthy=healthy,
            permitted=permitted,
            revision_matches=revision_matches,
            unexpired=unexpired,
        )
        if reason == "allow":
            detail = "allow"
        elif reason == "permission_denied":
            detail = f"{reason}:{permission_reason}"
        elif reason == "superseded":
            detail = f"{reason}:{revision_reason}"
        else:
            detail = reason

        return DecisionRecord(
            decision_id=uuid.uuid4().hex,
            trace_id=envelope.trace_id,
            policy_version=snapshot.version,
            policy_hash=snapshot.policy_hash,
            principal=envelope.principal,
            target=envelope.target.key(),
            capability=envelope.capability,
            turn_revision=envelope.turn_revision,
            outcome="allow" if reason == "allow" else "deny",
            reason=detail,
            created_ms=now,
            stage="final",
            effect_id=envelope.effect_id,
        )

    def _revision_matches(
        self, envelope: EffectEnvelope, current_turn: TurnRef | None
    ) -> tuple[bool, str]:
        turn = current_turn
        if turn is None and self._turn_lookup is not None and envelope.turn_id:
            turn = self._turn_lookup(envelope.turn_id)
        if turn is None:
            if not envelope.turn_id:
                return True, "no_turn_claim"
            return False, "turn_state_unavailable"
        if turn.turn_id != envelope.turn_id:
            return False, "turn_mismatch"
        if turn.revision != envelope.turn_revision:
            return False, "revision_stale"
        if turn.closed_ms is not None:
            return False, "turn_closed"
        return True, "match"


def _is_group_chat(chat_id: str) -> bool:
    return chat_id.endswith("@g.us")


__all__ = [
    "AdapterSnapshotProvider",
    "CapabilityResolver",
    "DenyAllCapabilities",
    "FastGateOutcome",
    "FastGateResult",
    "IngestGate",
    "IngestRequest",
    "PolicyCapabilityResolver",
    "PolicySnapshotProvider",
    "SnapshotEffectAuthorizer",
    "effect_guard",
]
