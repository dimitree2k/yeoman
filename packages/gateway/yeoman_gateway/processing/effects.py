"""Durable effect gateway: submit, authorize, claim, execute, record.

The gateway is the single durable entry point for planned user-visible actions. It never
invents success: an executor reports ``sent``, ``not_executed`` or ``unknown``, and there
is no generic timeout retry (spec R05, R06, R07).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

from yeoman_gateway.processing.models import (
    DecisionRecord,
    EffectEnvelope,
    EffectReceipt,
    ProcessingError,
    TurnRef,
)
from yeoman_gateway.processing.models import (
    now_ms as _now_ms,
)
from yeoman_gateway.processing.store import ProcessingStore

#: Transport errors that provably happened before anything was dispatched. Everything
#: else is treated as an unproven outcome, because a failure after the frame was written
#: cannot be distinguished from a success (spec R06, R07).
PRE_DISPATCH_ERROR_MARKERS: tuple[str, ...] = (
    "not connected",
    "channel not available",
    "channel does not support",
    "unknown channel",
    "not running",
)


def is_pre_dispatch_error(exc: BaseException) -> bool:
    """True when the transport clearly refused before dispatching."""
    text = str(exc).lower()
    return any(marker in text for marker in PRE_DISPATCH_ERROR_MARKERS)


#: Reasons that mean "not now, re-evaluate later" instead of "never send this".
_BLOCKING_REASONS = frozenset({"policy_unhealthy", "permission_denied", "queue_capacity"})
#: Reasons that mean the planned action itself became invalid.
_CANCELLING_REASONS = frozenset({"superseded"})
_EXPIRING_REASONS = frozenset({"expired"})


@runtime_checkable
class EffectAuthorizer(Protocol):
    """Final synchronous policy check before any external execution."""

    def check(
        self, envelope: EffectEnvelope, current_turn: TurnRef | None
    ) -> DecisionRecord: ...


@runtime_checkable
class EffectExecutor(Protocol):
    """The only registered transport call for one effect kind."""

    async def execute(self, envelope: EffectEnvelope) -> EffectReceipt: ...


class EffectGateway:
    """Durable accept-and-execute path for effects.

    ``submit`` is durable acceptance, not a delivery claim. ``execute_ready`` is the only
    place that may call a transport.
    """

    def __init__(
        self,
        store: ProcessingStore,
        *,
        authorizer: EffectAuthorizer | None = None,
        executor: EffectExecutor | None = None,
        worker_id: str | None = None,
        lease_ms: int = 30_000,
        clock: Callable[[], int] | None = None,
    ) -> None:
        if lease_ms <= 0:
            raise ValueError("lease_ms must be positive")
        self._store = store
        self._authorizer = authorizer
        self._executor = executor
        self._worker_id = worker_id or "effect-gateway"
        self._lease_ms = lease_ms
        self._clock = clock or _now_ms

    @property
    def store(self) -> ProcessingStore:
        return self._store

    @property
    def wired(self) -> bool:
        return self._authorizer is not None and self._executor is not None

    def set_authorizer(self, authorizer: EffectAuthorizer | None) -> None:
        self._authorizer = authorizer

    def set_executor(self, executor: EffectExecutor | None) -> None:
        self._executor = executor

    def set_direct_senders(self, outbound: Any, reaction: Any) -> None:
        """Give the executor a channel transport adapter that can confirm a real send."""
        setter = getattr(self._executor, "set_direct_senders", None)
        if setter is None:
            raise ProcessingError("current executor cannot accept a direct transport")
        setter(outbound, reaction)

    # -- acceptance --------------------------------------------------------------------

    def submit(self, envelope: EffectEnvelope) -> EffectReceipt:
        """Durably accept one planned effect.

        Idempotent per ``operation_key``: an identical retry returns the original effect
        id and its persisted state. The returned receipt is **not** evidence of delivery.
        """
        now = self._clock()
        effect_id = self._store.enqueue_effect(
            effect_id=envelope.effect_id,
            operation_key=envelope.operation_key,
            payload=envelope.payload,
            target=envelope.target,
            trace_id=envelope.trace_id,
            turn_id=envelope.turn_id,
            turn_revision=envelope.turn_revision,
            principal=envelope.principal,
            capability=envelope.capability,
            expires_at_ms=envelope.expires_at_ms,
            policy_version=envelope.policy_version,
            policy_hash=envelope.policy_hash,
            now_ms=now,
            state="queued",
        )
        state = self._store.effect_state(effect_id) or "queued"
        return EffectReceipt(
            effect_id=effect_id,
            state=state,
            operation_key=envelope.operation_key,
            accepted=effect_id == envelope.effect_id,
            updated_ms=now,
        )

    # -- execution ---------------------------------------------------------------------

    async def execute_ready(self, effect_id: str) -> EffectReceipt:
        """Authorize, claim and execute one queued effect at most once.

        Refuses loudly when the authorizer or executor is missing: a half-wired gateway
        must never become an unguarded transport path.
        """
        if self._authorizer is None or self._executor is None:
            raise ProcessingError(
                "effect gateway is not wired: both an authorizer and an executor are required"
            )
        stored = self._store.get_effect(effect_id)
        if stored is None:
            raise ProcessingError(f"unknown effect: {effect_id}")
        now = self._clock()

        if stored.state == "unknown":
            return self._receipt(
                stored.effect_id,
                stored.state,
                stored.operation_key,
                detail="outcome unproven; reconciliation required before any re-execution",
            )
        if stored.state == "unknown_nonrepeatable":
            return self._receipt(
                stored.effect_id,
                stored.state,
                stored.operation_key,
                detail="escalated: requires operator decision or later evidence",
            )
        if stored.state != "queued":
            return self._receipt(
                stored.effect_id,
                stored.state,
                stored.operation_key,
                detail="effect is not executable in its current state",
            )
        if stored.expires_at_ms is not None and stored.expires_at_ms <= now:
            self._store.transition(
                effect_id,
                expected="queued",
                target="expired",
                now_ms=now,
                evidence={"kind": "expiry", "detail": "deadline passed before execution"},
            )
            return self._receipt(
                effect_id, "expired", stored.operation_key, detail="expired before execution"
            )

        envelope = stored.to_envelope()
        decision = self._authorizer.check(envelope, None)
        self._store.record_decision(decision)
        if decision.outcome != "allow":
            target = self._state_for_reason(decision.reason)
            self._store.transition(
                effect_id,
                expected="queued",
                target=target,
                now_ms=now,
                evidence={"kind": "policy", "detail": decision.reason},
            )
            return self._receipt(
                effect_id, target, stored.operation_key, detail=decision.reason
            )

        if not self._store.claim_effect(
            effect_id,
            self._worker_id,
            now,
            self._lease_ms,
            policy_version=decision.policy_version,
        ):
            current = self._store.effect_state(effect_id) or stored.state
            return self._receipt(
                effect_id, current, stored.operation_key, detail="claim not acquired"
            )
        attempt_id = self._store.open_attempt_id(effect_id)

        try:
            result = await self._executor.execute(envelope)
        except asyncio.CancelledError:
            # Best-effort cancellation only. Whether the transport ran is unproven.
            self._store.transition(
                effect_id,
                expected="executing",
                target="unknown",
                now_ms=self._clock(),
                evidence={"kind": "cancelled", "detail": "execution cancelled; outcome unproven"},
                worker_id=self._worker_id,
            )
            raise
        except Exception as exc:
            proven_not_executed = is_pre_dispatch_error(exc)
            target = "failed" if proven_not_executed else "unknown"
            self._store.transition(
                effect_id,
                expected="executing",
                target=target,
                now_ms=self._clock(),
                evidence={
                    "kind": "not_executed" if proven_not_executed else "dispatch_unknown",
                    "detail": f"{type(exc).__name__}: {str(exc)[:200]}",
                },
                worker_id=self._worker_id,
            )
            return self._receipt(
                effect_id,
                target,
                stored.operation_key,
                attempt_id=attempt_id,
                detail=(
                    f"transport refused before dispatch ({type(exc).__name__})"
                    if proven_not_executed
                    else f"transport raised {type(exc).__name__}; outcome unproven"
                ),
            )

        reported = result.state if result is not None else "unknown"
        detail = result.detail if result is not None else None
        match reported:
            case "sent":
                self._store.transition(
                    effect_id,
                    expected="executing",
                    target="sent",
                    now_ms=self._clock(),
                    evidence={"kind": "transport", "detail": detail or "accepted by transport"},
                    worker_id=self._worker_id,
                )
            case "not_executed":
                self._store.transition(
                    effect_id,
                    expected="executing",
                    target="failed",
                    now_ms=self._clock(),
                    evidence={
                        "kind": "not_executed",
                        "detail": detail or "transport proved the effect did not run",
                    },
                    worker_id=self._worker_id,
                )
            case "unknown" | _:
                self._store.transition(
                    effect_id,
                    expected="executing",
                    target="unknown",
                    now_ms=self._clock(),
                    evidence={
                        "kind": "dispatch_unknown",
                        "detail": detail or f"executor reported {reported}",
                    },
                    worker_id=self._worker_id,
                )
        final_state = self._store.effect_state(effect_id) or reported
        return self._receipt(
            effect_id,
            final_state,
            stored.operation_key,
            attempt_id=attempt_id,
            detail=detail,
        )

    def recover_expired_claims(self) -> tuple[str, ...]:
        """Turn expired ``executing`` claims into ``unknown`` after a restart."""
        return self._store.recover_executing(self._clock())

    # -- helpers -----------------------------------------------------------------------

    def _state_for_reason(self, reason: str) -> str:
        if reason in _CANCELLING_REASONS:
            return "cancelled"
        if reason in _EXPIRING_REASONS:
            return "expired"
        if reason in _BLOCKING_REASONS:
            return "blocked"
        return "blocked"

    def _receipt(
        self,
        effect_id: str,
        state: str,
        operation_key: str,
        *,
        attempt_id: str | None = None,
        detail: str | None = None,
    ) -> EffectReceipt:
        stored = self._store.get_effect(effect_id)
        return EffectReceipt(
            effect_id=effect_id,
            state=state,
            operation_key=operation_key,
            attempt_id=attempt_id,
            detail=detail,
            accepted=True,
            updated_ms=stored.updated_ms if stored is not None else None,
        )
