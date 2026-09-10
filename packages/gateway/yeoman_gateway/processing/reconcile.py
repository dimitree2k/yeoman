"""Bounded reconciliation of effects whose outcome is unproven (Plan 04, R07).

The reconciler only ever *looks*. It reads durable evidence, records what it found, and
moves the effect state accordingly - it never calls a transport, never re-executes an
effect and never guesses. Re-execution happens exclusively through the normal dispatch
path, with a fresh policy, revision and expiry check.

Probe order (each stage ends with confirmed / not_executed / inconclusive):

1. a transport receipt that carries a provider message id proves acceptance,
2. delivery/read/played signals for that provider message prove it arrived,
3. a provider lookup - only when a handler is registered and a provider id exists,
4. a recorded pre-dispatch failure proves the effect never ran.

Absence of evidence is never proof: an empty result is ``inconclusive``, not
``not_executed``.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from yeoman_gateway.processing.models import (
    DecisionRecord,
    EffectEvidence,
    ProcessingError,
    RetainedEffectMeta,
    TransportReceipt,
)
from yeoman_gateway.processing.models import (
    now_ms as _now_ms,
)

#: Signal statuses that prove a message reached the recipient.
DELIVERED_STATUSES = frozenset({"delivered", "read", "played", "read-self"})


class ProbeOutcome(StrEnum):
    """What one probe could establish."""

    CONFIRMED = "confirmed"
    NOT_EXECUTED = "not_executed"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True, slots=True)
class ProbeRequest:
    """Everything a probe may look at; payload-free by construction."""

    effect: RetainedEffectMeta
    transport: TransportReceipt | None
    attempt_number: int
    deadline_ms: int
    signals: tuple[Any, ...] = ()


@dataclass(frozen=True, slots=True)
class ProbeResult:
    outcome: ProbeOutcome
    detail: str | None = None


@runtime_checkable
class ReconciliationProbe(Protocol):
    async def probe(self, request: ProbeRequest) -> ProbeResult: ...


async def no_provider_lookup(*_args: Any, **_kwargs: Any) -> None:
    """Default provider lookup: nothing is looked up, so nothing can be claimed."""
    return None


class LocalEvidenceProbe:
    """Reconciles from durable local evidence only."""

    def __init__(
        self,
        store: Any,
        *,
        provider_lookup: Callable[[ProbeRequest], Any] | None = None,
        provider_lookup_enabled: bool = False,
    ) -> None:
        self._store = store
        self._provider_lookup = provider_lookup
        self._provider_lookup_enabled = bool(provider_lookup_enabled)

    async def probe(self, request: ProbeRequest) -> ProbeResult:
        transport = request.transport

        # 1. a receipt with a provider id proves the transport accepted the message
        if transport is not None and transport.provider_message_id:
            return ProbeResult(
                ProbeOutcome.CONFIRMED,
                f"transport reported provider id ({transport.channel})",
            )

        # 2. delivery/read evidence for that provider message
        if transport is not None and transport.provider_message_id:
            for signal in request.signals:
                payload = dict(getattr(signal, "payload", None) or {})
                status = str(payload.get("status") or getattr(signal, "kind", "")).lower()
                if status in DELIVERED_STATUSES:
                    return ProbeResult(
                        ProbeOutcome.CONFIRMED, f"provider signal {status}"
                    )

        # 3. provider lookup - off by default and never invented
        if (
            self._provider_lookup_enabled
            and self._provider_lookup is not None
            and transport is not None
            and transport.provider_message_id
        ):
            try:
                result = await self._provider_lookup(request)
            except Exception as exc:  # a failed lookup proves nothing
                return ProbeResult(
                    ProbeOutcome.INCONCLUSIVE, f"provider lookup failed: {type(exc).__name__}"
                )
            if result is not None:
                return result

        # 4. a recorded pre-dispatch failure proves the effect never ran
        for attempt in getattr(request.effect, "attempts", ()) or ():
            if attempt.outcome == "failed":
                return ProbeResult(
                    ProbeOutcome.NOT_EXECUTED, "transport refused before dispatch"
                )

        return ProbeResult(
            ProbeOutcome.INCONCLUSIVE, "no durable evidence for this effect"
        )


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    """What one reconciliation pass did."""

    effect_id: str
    outcome: ProbeOutcome | None = None
    state: str | None = None
    detail: str | None = None
    probe_id: str | None = None
    next_due_ms: int | None = None

    @property
    def changed(self) -> bool:
        return self.state is not None


def _effect_meta(store: Any, effect_id: str) -> RetainedEffectMeta | None:
    for effect in store.list_effects(states=None, limit=500):
        if effect.effect_id == effect_id:
            return effect
    return None


async def reconcile_effect(
    store: Any,
    effect_id: str,
    *,
    probe: ReconciliationProbe,
    now_ms: int | None = None,
    attempt_number: int = 1,
    deadline_ms: int | None = None,
    worker_id: str = "reconciler",
    probe_timeout_s: float | None = None,
) -> ReconciliationResult:
    """Run one probe and apply its consequence through the state machine."""
    moment = int(now_ms) if now_ms is not None else _now_ms()
    effect = _effect_meta(store, effect_id)
    if effect is None:
        raise ProcessingError(f"unknown effect: {effect_id}")
    state = store.effect_state(effect_id)
    if state not in ("unknown", "unknown_nonrepeatable"):
        return ReconciliationResult(effect_id, state=state, detail="not reconcilable")

    transport = store.effect_transport_receipt(effect_id)
    signals: tuple[Any, ...] = ()
    if transport is not None and transport.provider_message_id:
        signals = store.delivery_signals(
            chat_id=transport.chat_id, message_id=transport.provider_message_id
        )
    request = ProbeRequest(
        effect=effect,
        transport=transport,
        attempt_number=int(attempt_number),
        deadline_ms=int(deadline_ms) if deadline_ms is not None else moment,
        signals=signals,
    )
    result = await probe.probe(request)

    if result.outcome is ProbeOutcome.CONFIRMED:
        store.transition(
            effect_id,
            expected=(state,),
            target="sent",
            now_ms=moment,
            evidence={"kind": "probe", "detail": result.detail or "confirmed"},
            worker_id=worker_id if state == "executing" else None,
        )
        return ReconciliationResult(effect_id, result.outcome, "sent", result.detail)

    if result.outcome is ProbeOutcome.NOT_EXECUTED:
        store.transition(
            effect_id,
            expected=(state,),
            target="queued",
            now_ms=moment,
            evidence={"kind": "not_executed", "detail": result.detail or "proven not executed"},
        )
        return ReconciliationResult(effect_id, result.outcome, "queued", result.detail)

    # inconclusive: escalate only when the deadline is reached, otherwise probe later
    if deadline_ms is not None and moment >= int(deadline_ms):
        store.transition(
            effect_id,
            expected=(state,),
            target="unknown_nonrepeatable",
            now_ms=moment,
            evidence={
                "kind": "probe",
                "detail": "deadline reached; operator decision required",
            },
        )
        store.record_evidence(
            effect_id,
            kind="operator",
            now_ms=moment,
            detail="pending local operator decision",
        )
        return ReconciliationResult(
            effect_id, result.outcome, "unknown_nonrepeatable", result.detail
        )
    return ReconciliationResult(effect_id, result.outcome, None, result.detail)


def operator_decision(
    store: Any,
    effect_id: str,
    *,
    decision: str,
    operator_id: str,
    policy_version: str,
    policy_hash: str,
    now_ms: int | None = None,
) -> ReconciliationResult:
    """Record an administrative decision about an escalated effect.

    ``confirmed_sent`` and ``confirmed_absent`` leave the escalated state through the
    normal transition rules; ``abandon`` stays terminal. A requeue out of
    ``unknown_nonrepeatable`` is deliberately not claimed here - the state machine would
    refuse it without an explicit extension.
    """
    if decision not in {"confirmed_sent", "confirmed_absent", "abandon"}:
        raise ValueError(f"unknown operator decision: {decision}")
    moment = int(now_ms) if now_ms is not None else _now_ms()
    state = store.effect_state(effect_id)
    if state != "unknown_nonrepeatable":
        raise ProcessingError(f"effect {effect_id} is not escalated (state={state})")

    record = DecisionRecord(
        decision_id=uuid.uuid4().hex,
        trace_id="",
        policy_version=policy_version,
        policy_hash=policy_hash,
        principal=operator_id,
        target="",
        capability="reconciliation.operator",
        turn_revision=1,
        outcome="allow" if decision == "confirmed_sent" else "deny",
        reason=f"operator_decision:{decision}",
        created_ms=moment,
        stage="admin",
        effect_id=effect_id,
    )
    store.record_decision(record)
    store.record_evidence(
        effect_id, kind="operator", now_ms=moment, detail=f"{decision} by {operator_id}"
    )
    if decision == "confirmed_sent":
        store.transition(
            effect_id,
            expected="unknown_nonrepeatable",
            target="sent",
            now_ms=moment,
            evidence=EffectEvidence(kind="operator", detail=f"confirmed_sent by {operator_id}"),
        )
        return ReconciliationResult(effect_id, ProbeOutcome.CONFIRMED, "sent", decision)
    return ReconciliationResult(effect_id, None, state, decision)


def probe_due_ms(
    unknown_ms: int, *, attempt_number: int, backoff_seconds: tuple[int, ...]
) -> int:
    """When probe *attempt_number* (1-based) becomes due."""
    if attempt_number < 1:
        raise ValueError("attempt_number must be positive")
    steps = backoff_seconds[:attempt_number]  # due(k) = unknown + sum(backoff[:k])
    return int(unknown_ms) + sum(int(step) for step in steps) * 1000


__all__ = [
    "DELIVERED_STATUSES",
    "LocalEvidenceProbe",
    "ProbeOutcome",
    "ProbeRequest",
    "ProbeResult",
    "ReconciliationProbe",
    "ReconciliationResult",
    "no_provider_lookup",
    "operator_decision",
    "probe_due_ms",
    "reconcile_effect",
]
