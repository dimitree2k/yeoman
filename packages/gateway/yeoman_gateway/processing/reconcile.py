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
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from loguru import logger

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
    """Direct lookup by id.

    Scanning ``list_effects(limit=500)`` missed every effect past the first 500 rows, so a
    new ``unknown`` effect was never probed and never escalated.
    """
    return store.effect_meta(effect_id)


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
    "ReconciliationService",
    "no_provider_lookup",
    "operator_decision",
    "probe_due_ms",
    "reconcile_effect",
]


class ReconciliationService:
    """Bounded reconciliation loop. It probes; it never executes an effect."""

    def __init__(
        self,
        store: Any,
        *,
        probe: ReconciliationProbe,
        config: Any,
        worker_id: str = "reconciler",
        clock: Callable[[], int] | None = None,
        tick_seconds: float = 1.0,
    ) -> None:
        self._store = store
        self._probe = probe
        self._config = config
        self._worker_id = worker_id
        self._clock = clock or _now_ms
        self._tick_seconds = max(0.05, float(tick_seconds))
        self._task: Any = None
        self._stopping = False
        self._counters: dict[str, int] = {
            "ticks": 0,
            "probes_run": 0,
            "confirmed": 0,
            "not_executed": 0,
            "inconclusive": 0,
            "escalated": 0,
            "recovered": 0,
        }

    # -- lifecycle ---------------------------------------------------------------------

    async def start(self) -> None:
        if self._task is not None:
            return
        import asyncio

        self._stopping = False
        self._task = asyncio.create_task(self._run_loop())
        # One startup line so an operator can tell the loop is live; the loop itself
        # stays quiet because it ticks every second.
        logger.info("reconciliation loop started tick_seconds={}", self._tick_seconds)

    async def stop(self) -> None:
        import asyncio

        self._stopping = True
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def _run_loop(self) -> None:
        import asyncio

        while not self._stopping:
            try:
                await self.tick_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # one bad tick must not end the loop
                logger.warning("reconciliation tick failed error_type={}", type(exc).__name__)
            await asyncio.sleep(self._tick_seconds)

    def stats(self) -> Mapping[str, int]:
        return dict(self._counters)

    # -- work --------------------------------------------------------------------------

    async def tick_once(self) -> tuple[ReconciliationResult, ...]:
        import asyncio

        now = self._clock()
        self._counters["ticks"] += 1

        recovered = self._store.recover_executing(now)
        self._counters["recovered"] += len(recovered)
        for effect_id in recovered:
            # The outcome was already open before the restart, so the first probe is due now.
            self._store.schedule_probe(effect_id, attempt_number=1, due_ms=now, now_ms=now)

        candidates = [
            effect.effect_id
            for effect in self._store.list_effects(
                states=("unknown", "unknown_nonrepeatable"), limit=200
            )
        ]
        if not candidates:
            return ()

        semaphore = asyncio.Semaphore(max(1, int(self._probe_concurrency())))
        results: list[ReconciliationResult] = []

        async def _one(effect_id: str) -> None:
            async with semaphore:
                result = await self._reconcile_candidate(effect_id, now)
                if result is not None:
                    results.append(result)

        await asyncio.gather(*(_one(effect_id) for effect_id in candidates))
        return tuple(results)

    async def reconcile_effect(self, effect_id: str) -> ReconciliationResult | None:
        return await self._reconcile_candidate(effect_id, self._clock(), force=True)

    # -- internals ---------------------------------------------------------------------

    def _probe_concurrency(self) -> int:
        return int(getattr(self._config, "probe_concurrency", 2) or 1)

    def _backoff(self) -> tuple[int, ...]:
        return tuple(int(step) for step in getattr(self._config, "backoff_seconds", (5, 15, 45)))

    async def _reconcile_candidate(
        self, effect_id: str, now: int, *, force: bool = False
    ) -> ReconciliationResult | None:
        import asyncio

        effect = self._effect_meta(effect_id)
        if effect is None:
            return None
        unknown_ms = int(effect.updated_ms or now)
        deadline_ms = unknown_ms + int(getattr(self._config, "deadline_seconds", 600)) * 1000
        max_probes = int(getattr(self._config, "max_probes", 6))
        backoff = self._backoff()

        probe_record = self._store.open_probe(effect_id)
        if probe_record is None:
            attempt = self._store.next_probe_number(effect_id)
            due_ms = probe_due_ms(unknown_ms, attempt_number=attempt, backoff_seconds=backoff)
            # Past the deadline no further probe is planned at all; the plan fixes the
            # deadline as a product decision, not a provider statement.
            if attempt > max_probes or now >= deadline_ms or due_ms > deadline_ms:
                return self._escalate(effect_id, now, reason="deadline reached; operator decision required")
            self._store.schedule_probe(
                effect_id, attempt_number=attempt, due_ms=due_ms, now_ms=now
            )
            probe_record = self._store.open_probe(effect_id)
            if probe_record is None:
                return None

        if now >= deadline_ms:
            # A plan that is still open when the deadline passes must be closed and
            # escalated - returning here left the same plan open on every later tick.
            self._store.finish_probe(
                probe_record.probe_id,
                outcome=ProbeOutcome.INCONCLUSIVE.value,
                now_ms=now,
                worker_id=self._worker_id,
                detail="deadline_passed",
            )
            return self._escalate(effect_id, now, reason="probe_deadline")
        if not force and probe_record.due_ms > now:
            return None

        lease_ms = int(getattr(self._config, "claim_lease_seconds", 30)) * 1000
        if not self._store.claim_probe(probe_record.probe_id, self._worker_id, now, lease_ms):
            return None

        timeout_s = max(0.1, int(getattr(self._config, "probe_timeout_ms", 10_000)) / 1000)
        transport = self._store.effect_transport_receipt(effect_id)
        if transport is not None and transport.provider_message_id:
            # Late delivery/read evidence is attached before probing, so a probe sees the
            # full picture and no evidence is lost to the correlation gap.
            from yeoman_gateway.processing.signals import attach_receipt_evidence

            self._counters["evidence_attached"] = self._counters.get("evidence_attached", 0) + len(
                attach_receipt_evidence(self._store, effect_id, now_ms=now)
            )
        signals = ()
        if transport is not None and transport.provider_message_id:
            signals = self._store.delivery_signals(
                chat_id=transport.chat_id, message_id=transport.provider_message_id
            )
        request = ProbeRequest(
            effect=effect,
            transport=transport,
            attempt_number=probe_record.attempt_number,
            deadline_ms=deadline_ms,
            signals=signals,
        )
        try:
            result = await asyncio.wait_for(self._probe.probe(request), timeout=timeout_s)
        except asyncio.CancelledError:
            self._store.finish_probe(
                probe_record.probe_id,
                outcome=ProbeOutcome.INCONCLUSIVE.value,
                now_ms=self._clock(),
                worker_id=self._worker_id,
                detail="cancelled",
            )
            raise
        except Exception as exc:
            result = ProbeResult(
                ProbeOutcome.INCONCLUSIVE, f"probe raised {type(exc).__name__}"
            )

        self._store.finish_probe(
            probe_record.probe_id,
            outcome=result.outcome.value,
            now_ms=self._clock(),
            worker_id=self._worker_id,
            detail=result.detail,
        )
        self._counters["probes_run"] += 1
        self._counters[result.outcome.value] = self._counters.get(result.outcome.value, 0) + 1

        reconciled = await reconcile_effect(
            self._store,
            effect_id,
            probe=_StaticProbe(result),
            now_ms=self._clock(),
            attempt_number=probe_record.attempt_number,
            deadline_ms=deadline_ms,
            worker_id=self._worker_id,
        )
        return reconciled

    def _escalate(self, effect_id: str, now: int, *, reason: str) -> ReconciliationResult:
        state = self._store.effect_state(effect_id)
        if state in ("unknown", "unknown_nonrepeatable"):
            if state == "unknown":
                self._store.transition(
                    effect_id,
                    expected="unknown",
                    target="unknown_nonrepeatable",
                    now_ms=now,
                    evidence={"kind": "probe", "detail": reason},
                )
            self._store.record_evidence(
                effect_id, kind="operator", now_ms=now, detail="pending local operator decision"
            )
            self._counters["escalated"] += 1
        return ReconciliationResult(
            effect_id, ProbeOutcome.INCONCLUSIVE, "unknown_nonrepeatable", reason
        )

    def _effect_meta(self, effect_id: str) -> RetainedEffectMeta | None:
        return self._store.effect_meta(effect_id)


@dataclass(frozen=True, slots=True)
class _StaticProbe:
    """Replays one already computed result without looking again."""

    result: ProbeResult

    async def probe(self, request: ProbeRequest) -> ProbeResult:
        return self.result
