"""Immutable domain models for state-aware message processing.

Contracts follow ``docs/superpowers/specs/2026-09-10-message-processing-evolution-spec.md``
(R01, R05, R06, R10). Everything in this module is frozen: the journal stores canonical
JSON plus hashes, never live objects, and never process-local times as deadlines.

Identifier rules:

* ids are opaque strings,
* times are UTC epoch milliseconds,
* revisions are positive integers.

Hashes are correlation and dedup devices. They are **not** encryption and give no
anonymity guarantee: a hash over a short chat message can be brute-forced.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, ClassVar, Literal

DAY_MS = 86_400_000

EventKind = Literal["message", "edit", "reaction", "delete", "receipt"]
EffectState = Literal[
    "planned",
    "queued",
    "executing",
    "sent",
    "blocked",
    "expired",
    "failed",
    "cancelled",
    "unknown",
    "unknown_nonrepeatable",
]
EffectPayloadKind = Literal["text", "media", "reaction", "delete", "external_action"]
DecisionOutcome = Literal["allow", "deny"]
DecisionStage = Literal["fast", "final", "admin"]

EVENT_KINDS: tuple[str, ...] = ("message", "edit", "reaction", "delete", "receipt")
EFFECT_STATES: tuple[str, ...] = (
    "planned",
    "queued",
    "executing",
    "sent",
    "blocked",
    "expired",
    "failed",
    "cancelled",
    "unknown",
    "unknown_nonrepeatable",
)
TERMINAL_EFFECT_STATES = frozenset(
    {"sent", "expired", "failed", "cancelled", "unknown_nonrepeatable"}
)
#: States an effect may be claimed from. ``unknown`` is deliberately absent: an
#: unproven external effect is never re-executed without reconciliation evidence.
CLAIMABLE_EFFECT_STATES = frozenset({"queued"})
#: Evidence kinds that justify putting a not-yet-executed effect back in the queue.
REQUEUE_EVIDENCE_KINDS = frozenset({"not_executed", "safe_retry", "operator"})


class ProcessingError(RuntimeError):
    """Base class for processing-core failures."""


class JournalConflictError(ProcessingError, ValueError):
    """Same provider identity (or event id) with a different payload."""


class EffectConflictError(ProcessingError, ValueError):
    """Same ``operation_key`` with a different payload, target or turn identity."""


class InvalidTransitionError(ProcessingError, ValueError):
    """State change that the effect state machine does not allow."""


def now_ms() -> int:
    """Current UTC time in epoch milliseconds."""
    return int(time.time() * 1000)


def canonical_json(value: Any) -> str:
    """Canonical JSON used for every stored hash and payload blob."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_hash(value: Any) -> str:
    """Deterministic SHA-256 over the canonical JSON form of *value*."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------------------
# Effect payload variants
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TextPayload:
    """Plain text transport command, optionally as a reply."""

    text: str
    reply_to: str | None = None

    kind: ClassVar[str] = "text"

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"kind": self.kind, "text": self.text}
        if self.reply_to is not None:
            data["reply_to"] = self.reply_to
        return data


@dataclass(frozen=True, slots=True)
class MediaPayload:
    """Media transport command. Several items are several transport units."""

    media: tuple[str, ...]
    caption: str | None = None

    kind: ClassVar[str] = "media"

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"kind": self.kind, "media": list(self.media)}
        if self.caption is not None:
            data["caption"] = self.caption
        return data


@dataclass(frozen=True, slots=True)
class ReactionPayload:
    """Semantic reaction on an existing message."""

    message_id: str
    emoji: str

    kind: ClassVar[str] = "reaction"

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "message_id": self.message_id, "emoji": self.emoji}


@dataclass(frozen=True, slots=True)
class DeletePayload:
    """Deletion of an existing own message."""

    message_id: str

    kind: ClassVar[str] = "delete"

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "message_id": self.message_id}


@dataclass(frozen=True, slots=True)
class ExternalActionPayload:
    """Non-chat side effect (voice, admin reply, delegated write, ...)."""

    action: str
    arguments: Mapping[str, Any] = field(default_factory=dict)

    kind: ClassVar[str] = "external_action"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "action": self.action,
            "arguments": dict(self.arguments),
        }


EffectPayload = (
    TextPayload | MediaPayload | ReactionPayload | DeletePayload | ExternalActionPayload
)

_KIND_INFERENCE: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("external_action", ("action",)),
    ("reaction", ("emoji",)),
    ("delete", ("message_id",)),
    ("media", ("media",)),
    ("text", ("text",)),
)


def payload_from_mapping(data: Mapping[str, Any]) -> EffectPayload:
    """Build a typed payload from a mapping.

    The explicit ``kind`` key wins; otherwise the payload is inferred from its keys.
    Anything ambiguous is rejected instead of being smuggled through as a dict:
    different payload kinds need different validators (spec R05).
    """
    kind = data.get("kind")
    if kind is None:
        for candidate, keys in _KIND_INFERENCE:
            if any(key in data for key in keys):
                kind = candidate
                break
    match kind:
        case "text":
            return TextPayload(
                text=str(data.get("text") or ""),
                reply_to=_opt_str(data.get("reply_to") or data.get("replyTo")),
            )
        case "media":
            raw_media = data.get("media") or data.get("media_paths") or ()
            if isinstance(raw_media, str):
                raw_media = (raw_media,)
            if not isinstance(raw_media, Iterable):
                raise ValueError("media payload requires a list of media references")
            return MediaPayload(
                media=tuple(str(item) for item in raw_media),
                caption=_opt_str(data.get("caption")),
            )
        case "reaction":
            return ReactionPayload(
                message_id=str(data.get("message_id") or ""),
                emoji=str(data.get("emoji") or ""),
            )
        case "delete":
            return DeletePayload(message_id=str(data.get("message_id") or ""))
        case "external_action":
            arguments = data.get("arguments") or {}
            if not isinstance(arguments, Mapping):
                raise ValueError("external_action arguments must be a mapping")
            return ExternalActionPayload(
                action=str(data.get("action") or ""),
                arguments={str(k): v for k, v in arguments.items()},
            )
        case _:
            raise ValueError(f"unsupported effect payload: {sorted(data)}")


def payload_to_mapping(payload: EffectPayload | Mapping[str, Any]) -> dict[str, Any]:
    """Canonical mapping form of a payload."""
    if isinstance(payload, Mapping):
        return payload_from_mapping(payload).to_dict()
    return payload.to_dict()


def payload_hash(payload: EffectPayload | Mapping[str, Any]) -> str:
    return canonical_hash(payload_to_mapping(payload))


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None


# --------------------------------------------------------------------------------------
# Targets, turns, events, decisions
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EffectTarget:
    """Where an effect lands. Part of the effect's stable identity."""

    channel: str
    chat_id: str
    thread_id: str | None = None
    message_id: str | None = None

    def __post_init__(self) -> None:
        if not self.channel or not self.chat_id:
            raise ValueError("effect target requires channel and chat_id")

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"channel": self.channel, "chat_id": self.chat_id}
        if self.thread_id is not None:
            data["thread_id"] = self.thread_id
        if self.message_id is not None:
            data["message_id"] = self.message_id
        return data

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> EffectTarget:
        return cls(
            channel=str(data.get("channel") or ""),
            chat_id=str(data.get("chat_id") or ""),
            thread_id=_opt_str(data.get("thread_id")),
            message_id=_opt_str(data.get("message_id")),
        )

    @property
    def target_hash(self) -> str:
        return canonical_hash(self.to_dict())

    def key(self) -> str:
        """Canonical ``channel:chat`` string used in policy decisions."""
        return f"{self.channel}:{self.chat_id}"


@dataclass(frozen=True, slots=True)
class TurnRef:
    """Identity and revision of one turn of a thread."""

    turn_id: str
    thread_id: str
    chat_id: str
    channel: str
    principal: str
    revision: int = 1
    opened_ms: int | None = None
    closed_ms: int | None = None

    def __post_init__(self) -> None:
        if self.revision < 1:
            raise ValueError("turn revision must be a positive integer")


@dataclass(frozen=True, slots=True)
class CanonicalEvent:
    """One canonical journal event (spec R01).

    ``payload`` is ``None`` once journal retention removed the raw payload; the hash
    and the tombstone timestamp survive so lineage and dedup stay diagnosable.
    """

    event_id: str
    event_key: str
    trace_id: str
    kind: str = "message"
    origin: str = "unknown"
    principal: str = ""
    channel: str = ""
    chat_id: str = ""
    occurred_ms: int | None = None
    created_ms: int | None = None
    source_message_id: str | None = None
    target_message_id: str | None = None
    thread_id: str | None = None
    turn_id: str | None = None
    payload: Mapping[str, Any] | None = field(default=None)
    payload_hash: str = ""
    payload_purged_ms: int | None = None

    def __post_init__(self) -> None:
        if self.kind not in EVENT_KINDS:
            raise ValueError(f"unknown canonical event kind: {self.kind}")

    @property
    def payload_available(self) -> bool:
        return self.payload is not None

    def relations(self) -> tuple[tuple[str, str], ...]:
        """Declared references of this event, in a stable order.

        Missing references are kept as relations with ``resolved = 0``. No parent id
        is ever invented (spec R01).
        """
        found: list[tuple[str, str]] = []
        if self.source_message_id:
            found.append(("source", str(self.source_message_id)))
        if self.target_message_id:
            found.append(("target", str(self.target_message_id)))
        payload = self.payload or {}
        for key, relation in (
            ("reply_to_event_id", "reply_to"),
            ("parent_event_id", "parent"),
            ("reply_to_message_id", "reply_to"),
        ):
            value = payload.get(key)
            if value:
                found.append((relation, str(value)))
        deduped: list[tuple[str, str]] = []
        for item in found:
            if item not in deduped:
                deduped.append(item)
        return tuple(deduped)


@dataclass(frozen=True, slots=True)
class RetainedEventMeta:
    """Lineage projection of an event: metadata only, never raw text."""

    event_id: str
    event_key: str
    trace_id: str
    kind: str
    origin: str
    principal: str
    channel: str
    chat_id: str
    occurred_ms: int | None
    created_ms: int | None
    payload_hash: str
    payload_available: bool
    payload_purged_ms: int | None
    source_message_id: str | None
    target_message_id: str | None
    thread_id: str | None
    turn_id: str | None
    relations: tuple[RelationMeta, ...] = ()


@dataclass(frozen=True, slots=True)
class RelationMeta:
    """One journal relation edge; ``resolved`` is 0 while the referent is unknown."""

    event_id: str
    relation: str
    ref_id: str
    resolved: int
    created_ms: int


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    """Immutable policy decision bound to the policy snapshot that produced it."""

    decision_id: str
    trace_id: str
    policy_version: str
    policy_hash: str
    principal: str
    target: str
    capability: str
    turn_revision: int
    outcome: DecisionOutcome
    reason: str
    created_ms: int
    stage: DecisionStage = "final"
    effect_id: str | None = None

    def __post_init__(self) -> None:
        if not self.policy_version:
            raise ValueError("decision record requires a policy version")
        if not self.policy_hash:
            raise ValueError("decision record requires a policy hash")
        if not self.decision_id:
            raise ValueError("decision record requires a decision id")
        if self.turn_revision < 1:
            raise ValueError("turn revision must be a positive integer")
        if self.outcome not in ("allow", "deny"):
            raise ValueError(f"unknown decision outcome: {self.outcome}")


@dataclass(frozen=True, slots=True)
class PolicySnapshot:
    """Evaluated policy content plus the identity it was loaded as.

    The version and the policy object are published together so a decision can never
    hash the file on disk while a different in-memory engine decides (spec R02).
    """

    version: str
    policy_hash: str
    policy: Any = None
    loaded_ms: int | None = None
    healthy: bool = True
    source: str | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if not self.version:
            raise ValueError("policy snapshot requires a version")


@dataclass(frozen=True, slots=True)
class EffectEnvelope:
    """Common effect envelope (spec R05).

    A planned action is submitted as one of these; the same ``operation_key`` with a
    different payload or target is a conflict, not a retry.
    """

    effect_id: str
    operation_key: str
    payload: EffectPayload
    target: EffectTarget
    trace_id: str = ""
    turn_id: str = ""
    turn_revision: int = 1
    principal: str = ""
    capability: str = ""
    expires_at_ms: int | None = None
    policy_version: str | None = None
    policy_hash: str | None = None
    created_ms: int | None = None

    def __post_init__(self) -> None:
        if not self.effect_id:
            raise ValueError("effect envelope requires an effect id")
        if not self.operation_key:
            raise ValueError("effect envelope requires an operation key")
        if self.turn_revision < 1:
            raise ValueError("turn revision must be a positive integer")

    @property
    def payload_kind(self) -> str:
        return str(self.payload.kind)

    @property
    def payload_hash(self) -> str:
        return payload_hash(self.payload)

    @property
    def target_hash(self) -> str:
        return self.target.target_hash


@dataclass(frozen=True, slots=True)
class StoredEffect:
    """Persisted effect as read back from the outbox.

    ``payload`` is ``None`` once retention removed it; the hashes and the state survive
    as tombstones so lineage and dedup stay diagnosable. Such an effect can no longer be
    executed: :meth:`to_envelope` refuses.
    """

    effect_id: str
    operation_key: str
    payload_kind: str
    payload_hash: str
    target_hash: str
    state: str
    created_ms: int
    updated_ms: int
    trace_id: str = ""
    turn_id: str = ""
    turn_revision: int = 1
    principal: str = ""
    capability: str = ""
    payload: EffectPayload | None = None
    target: EffectTarget | None = None
    payload_purged_ms: int | None = None
    expires_at_ms: int | None = None
    policy_version: str | None = None
    policy_hash: str | None = None
    lease_owner: str | None = None
    lease_until_ms: int | None = None

    @property
    def payload_available(self) -> bool:
        return self.payload is not None

    def to_envelope(self) -> EffectEnvelope:
        if self.payload is None:
            raise ProcessingError(
                f"effect {self.effect_id} payload was purged by retention; refusing to execute"
            )
        if self.target is None:
            raise ProcessingError(f"effect {self.effect_id} has no target; refusing to execute")
        return EffectEnvelope(
            effect_id=self.effect_id,
            operation_key=self.operation_key,
            payload=self.payload,
            target=self.target,
            trace_id=self.trace_id,
            turn_id=self.turn_id,
            turn_revision=self.turn_revision,
            principal=self.principal,
            capability=self.capability,
            expires_at_ms=self.expires_at_ms,
            policy_version=self.policy_version,
            policy_hash=self.policy_hash,
            created_ms=self.created_ms,
        )


@dataclass(frozen=True, slots=True)
class EffectReceipt:
    """Result of a submit, a claim or one transport attempt.

    ``state`` is the persisted effect state. A receipt is evidence about the local
    effect record; it is not proof that a message was read (spec R07).
    """

    effect_id: str
    state: str
    operation_key: str = ""
    attempt_id: str | None = None
    detail: str | None = None
    accepted: bool = False
    updated_ms: int | None = None

    @property
    def sent(self) -> bool:
        return self.state == "sent"

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_EFFECT_STATES


@dataclass(frozen=True, slots=True)
class EffectEvidence:
    """One durable evidence entry attached to an effect."""

    kind: str
    detail: str | None = None
    observed_ms: int | None = None
    worker_id: str | None = None
    state: str | None = None


@dataclass(frozen=True, slots=True)
class RetainedAttemptMeta:
    attempt_id: str
    effect_id: str
    started_ms: int
    finished_ms: int | None
    outcome: str | None
    policy_version: str | None
    detail: str | None


@dataclass(frozen=True, slots=True)
class RetainedEvidenceMeta:
    effect_id: str
    kind: str
    state: str | None
    detail: str | None
    observed_ms: int
    worker_id: str | None


@dataclass(frozen=True, slots=True)
class RetainedEffectMeta:
    """Lineage projection of an effect: ids, hashes and states, no payload text."""

    effect_id: str
    operation_key: str
    trace_id: str
    turn_id: str
    turn_revision: int
    principal: str
    capability: str
    target_hash: str
    payload_kind: str
    payload_hash: str
    payload_available: bool
    payload_purged_ms: int | None
    state: str
    expires_at_ms: int | None
    policy_version: str | None
    policy_hash: str | None
    created_ms: int
    updated_ms: int
    attempts: tuple[RetainedAttemptMeta, ...] = ()
    evidence: tuple[RetainedEvidenceMeta, ...] = ()


@dataclass(frozen=True, slots=True)
class LineageView:
    """Administrative lineage projection for one trace (spec R10)."""

    trace_id: str
    events: tuple[RetainedEventMeta, ...] = ()
    decisions: tuple[DecisionRecord, ...] = ()
    effects: tuple[RetainedEffectMeta, ...] = ()
    unresolved: tuple[RelationMeta, ...] = ()


@dataclass(frozen=True, slots=True)
class RetentionSettings:
    """Retention windows in milliseconds (spec section 4)."""

    journal_payload_ms: int = 7 * DAY_MS
    metadata_ms: int = 30 * DAY_MS
    unresolved_ms: int = 90 * DAY_MS

    def __post_init__(self) -> None:
        for name in ("journal_payload_ms", "metadata_ms", "unresolved_ms"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must not be negative")
        if self.journal_payload_ms > self.metadata_ms:
            raise ValueError("journal payload retention must not exceed metadata retention")
        if self.metadata_ms > self.unresolved_ms:
            raise ValueError("metadata retention must not exceed unresolved retention")


@dataclass(frozen=True, slots=True)
class PurgeReport:
    """Counts of one retention pass."""

    event_payloads_purged: int = 0
    effect_payloads_purged: int = 0
    events_deleted: int = 0
    relations_deleted: int = 0
    decisions_deleted: int = 0
    attempts_deleted: int = 0
    evidence_deleted: int = 0


# --------------------------------------------------------------------------------------
# Effect state machine
# --------------------------------------------------------------------------------------

_TRANSITIONS: Mapping[str, frozenset[str]] = {
    "planned": frozenset({"queued", "blocked", "cancelled", "expired"}),
    "queued": frozenset({"executing", "blocked", "cancelled", "expired", "failed"}),
    "executing": frozenset({"sent", "failed", "unknown"}),
    "blocked": frozenset({"queued", "cancelled", "expired", "failed"}),
    "unknown": frozenset({"sent", "queued", "expired", "unknown_nonrepeatable"}),
    "unknown_nonrepeatable": frozenset({"sent"}),
    "failed": frozenset({"queued"}),
    "sent": frozenset(),
    "expired": frozenset(),
    "cancelled": frozenset(),
}


def allowed_transitions(state: str) -> frozenset[str]:
    """States reachable from *state*. Unknown states have no transitions."""
    return _TRANSITIONS.get(state, frozenset())


def validate_transition(
    current: str,
    target: str,
    *,
    evidence_kinds: Iterable[str] = (),
    worker_id: str | None = None,
    lease_owner: str | None = None,
) -> None:
    """Raise :class:`InvalidTransitionError` unless ``current -> target`` is allowed.

    Deliberately conservative rules:

    * ``sent`` is a proven success and never downgraded by a late failure event,
    * ``unknown``/``failed`` return to ``queued`` only with explicit evidence,
    * a transition out of ``executing`` requires the claim-holding worker.
    """
    if target not in _TRANSITIONS:
        raise InvalidTransitionError(f"unknown effect state: {target}")
    if current not in _TRANSITIONS:
        raise InvalidTransitionError(f"unknown effect state: {current}")
    if target not in _TRANSITIONS[current]:
        raise InvalidTransitionError(f"illegal effect transition: {current} -> {target}")
    if current == "executing":
        if worker_id is None:
            raise InvalidTransitionError("transition out of executing requires the claiming worker")
        if lease_owner is not None and worker_id != lease_owner:
            raise InvalidTransitionError("only the claiming worker may leave executing")
    if target == "queued" and current in ("unknown", "failed"):
        if not REQUEUE_EVIDENCE_KINDS.intersection(evidence_kinds):
            raise InvalidTransitionError(
                f"{current} may only be requeued with evidence "
                f"{sorted(REQUEUE_EVIDENCE_KINDS)}, got {sorted(evidence_kinds)}"
            )


# --------------------------------------------------------------------------------------
# Threads, turns and generations (Plan 03)
# --------------------------------------------------------------------------------------

THREAD_STATES: tuple[str, ...] = ("open", "idle", "closed")
TURN_STATES: tuple[str, ...] = ("open", "awaiting", "closed", "superseded")


class TurnStateError(ProcessingError, ValueError):
    """A turn change that the turn state machine does not allow."""


class UpdateEffect(StrEnum):
    """What one incoming update does to the running turn."""

    APPEND = "append"
    SUPERSEDE = "supersede"
    OBSERVE = "observe"


@dataclass(frozen=True, slots=True)
class StoredThread:
    thread_id: str
    channel: str
    chat_id: str
    root_principal: str
    kind: str
    state: str
    opened_ms: int
    last_activity_ms: int
    closed_ms: int | None = None
    close_reason: str | None = None
    reopen_count: int = 0
    turn_seq: int = 0

    @property
    def is_open(self) -> bool:
        return self.state == "open"


@dataclass(frozen=True, slots=True)
class StoredTurn:
    turn_id: str
    thread_id: str
    principal: str
    revision: int = 1
    context_version: int = 1
    state: str = "open"
    opened_ms: int | None = None
    updated_ms: int | None = None
    closed_ms: int | None = None
    last_generation_id: str | None = None

    def to_ref(self, *, channel: str, chat_id: str) -> TurnRef:
        return TurnRef(
            turn_id=self.turn_id,
            thread_id=self.thread_id,
            chat_id=chat_id,
            channel=channel,
            principal=self.principal,
            revision=self.revision,
            opened_ms=self.opened_ms,
            closed_ms=self.closed_ms,
        )


@dataclass(frozen=True, slots=True)
class SourceRef:
    """One source revision a turn or generation saw."""

    event_id: str
    source_message_id: str | None = None
    role: str = "trigger"
    revision_at_join: int = 1
    removed_ms: int | None = None


@dataclass(frozen=True, slots=True)
class GenerationSnapshot:
    """Immutable record of what one provider request saw."""

    generation_id: str
    turn_id: str
    thread_id: str
    revision: int
    context_version: int
    source_refs: tuple[SourceRef, ...] = ()
    snapshot_hash: str = ""
    created_ms: int | None = None


@dataclass(frozen=True, slots=True)
class TurnBinding:
    """The turn a producer is currently working for."""

    turn: StoredTurn
    trace_id: str = ""
    generation_id: str | None = None
