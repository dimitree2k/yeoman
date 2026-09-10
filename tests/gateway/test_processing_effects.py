"""Plan 01 / R05, R06, R07: idempotent effect outbox, claims and gateway behaviour."""

from __future__ import annotations

from typing import Any

import pytest
from yeoman_gateway.processing.effects import EffectGateway
from yeoman_gateway.processing.models import (
    DecisionRecord,
    EffectEnvelope,
    EffectReceipt,
    EffectTarget,
    InvalidTransitionError,
    ProcessingError,
    TextPayload,
)
from yeoman_gateway.processing.store import ProcessingStore

TARGET = {"channel": "whatsapp", "chat_id": "chat1"}


def _envelope(
    *,
    effect_id: str = "fx1",
    operation_key: str = "turn1:send1",
    text: str = "A",
    target: dict[str, Any] | None = None,
    **overrides: Any,
) -> EffectEnvelope:
    payload: dict[str, Any] = {
        "effect_id": effect_id,
        "operation_key": operation_key,
        "payload": TextPayload(text=text),
        "target": EffectTarget.from_mapping(target or TARGET),
        "trace_id": "tr1",
        "turn_id": "turn1",
        "principal": "owner",
        "capability": "send_text",
    }
    payload.update(overrides)
    return EffectEnvelope(**payload)


class _Clock:
    def __init__(self, value: int = 0) -> None:
        self.value = value

    def __call__(self) -> int:
        return self.value


class _Authorizer:
    def __init__(self, reason: str = "allow") -> None:
        self.reason = reason
        self.calls: list[str] = []

    def check(self, envelope: EffectEnvelope, current_turn: Any) -> DecisionRecord:
        self.calls.append(envelope.effect_id)
        return DecisionRecord(
            decision_id=f"dec-{envelope.effect_id}-{self.reason}",
            trace_id=envelope.trace_id,
            policy_version="policy-v1",
            policy_hash="policy-hash-1",
            principal=envelope.principal,
            target=envelope.target.key(),
            capability=envelope.capability,
            turn_revision=envelope.turn_revision,
            outcome="allow" if self.reason == "allow" else "deny",
            reason=self.reason,
            created_ms=0,
        )


class _Executor:
    def __init__(self, result: str = "sent", error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[str] = []
        self.envelopes: list[EffectEnvelope] = []

    async def execute(self, envelope: EffectEnvelope) -> EffectReceipt:
        self.calls.append(envelope.effect_id)
        self.envelopes.append(envelope)
        if self.error is not None:
            raise self.error
        return EffectReceipt(
            effect_id=envelope.effect_id,
            state=self.result,
            detail=f"executor reported {self.result}",
        )


def test_operation_key_is_not_a_payload_wildcard(tmp_path):
    db = ProcessingStore(tmp_path / "p.db")
    args = {"operation_key": "turn1:send1", "now_ms": 0}
    assert db.enqueue_effect(effect_id="fx1", payload={"text": "A"}, **args) == "fx1"
    assert db.enqueue_effect(effect_id="fx2", payload={"text": "A"}, **args) == "fx1"
    with pytest.raises(ValueError):
        db.enqueue_effect(effect_id="fx3", payload={"text": "B"}, **args)
    db.close()


def test_same_operation_key_with_other_identity_is_a_conflict(tmp_path):
    db = ProcessingStore(tmp_path / "p.db")
    db.enqueue_effect(
        effect_id="fx1",
        operation_key="turn1:send1",
        payload={"text": "A"},
        target=TARGET,
        principal="owner",
        now_ms=0,
    )
    with pytest.raises(ValueError):
        db.enqueue_effect(
            effect_id="fx2",
            operation_key="turn1:send1",
            payload={"text": "A"},
            target={"channel": "whatsapp", "chat_id": "other-chat"},
            principal="owner",
            now_ms=0,
        )
    with pytest.raises(ValueError):
        db.enqueue_effect(
            effect_id="fx3",
            operation_key="turn1:send1",
            payload={"text": "A"},
            target=TARGET,
            principal="someone-else",
            now_ms=0,
        )
    db.close()


def test_only_one_worker_can_claim_an_effect(tmp_path):
    db = ProcessingStore(tmp_path / "p.db")
    db.enqueue_effect(effect_id="fx1", operation_key="k1", payload={"text": "A"}, now_ms=0)

    assert db.claim_effect("fx1", "worker-a", 0, 30_000) is True
    assert db.claim_effect("fx1", "worker-b", 0, 30_000) is False
    assert db.claim_effect("fx1", "worker-a", 0, 30_000) is False
    assert db.effect_state("fx1") == "executing"
    db.close()


def test_restart_turns_expired_execution_into_unknown_never_queued(tmp_path):
    path = tmp_path / "p.db"
    db = ProcessingStore(path)
    db.enqueue_effect(effect_id="fx1", operation_key="k1", payload={"text": "A"}, now_ms=0)
    assert db.claim_effect("fx1", "worker-a", 0, 30_000) is True
    db.close()

    # Simulated restart: the claim is still live, so nothing is recovered yet.
    db = ProcessingStore(path)
    assert db.recover_executing(10_000) == ()
    assert db.effect_state("fx1") == "executing"
    # Once the lease expired the outcome is unproven, not "safe to send again".
    assert db.recover_executing(30_001) == ("fx1",)
    assert db.effect_state("fx1") == "unknown"
    assert db.claim_effect("fx1", "worker-b", 30_002, 30_000) is False
    db.close()


def test_restart_keeps_queued_effect_as_candidate(tmp_path):
    path = tmp_path / "p.db"
    db = ProcessingStore(path)
    db.enqueue_effect(effect_id="fx1", operation_key="k1", payload={"text": "A"}, now_ms=0)
    db.close()

    db = ProcessingStore(path)
    assert db.effect_state("fx1") == "queued"
    assert db.claim_effect("fx1", "worker-a", 1, 30_000) is True
    db.close()


def test_duplicate_confirmation_and_state_regression(tmp_path):
    db = ProcessingStore(tmp_path / "p.db")
    db.enqueue_effect(effect_id="fx1", operation_key="k1", payload={"text": "A"}, now_ms=0)
    assert db.claim_effect("fx1", "worker-a", 0, 30_000) is True

    assert db.transition(
        "fx1", expected="executing", target="sent", now_ms=1, worker_id="worker-a"
    ) is True
    # A duplicate confirmation is a no-op, not a second success.
    assert db.transition(
        "fx1", expected="executing", target="sent", now_ms=2, worker_id="worker-a"
    ) is False
    # A late failure event never downgrades a proven success.
    with pytest.raises(InvalidTransitionError):
        db.transition("fx1", expected="sent", target="failed", now_ms=3)
    assert db.effect_state("fx1") == "sent"
    db.close()


def test_state_machine_rejects_illegal_jumps(tmp_path):
    db = ProcessingStore(tmp_path / "p.db")
    db.enqueue_effect(effect_id="fx1", operation_key="k1", payload={"text": "A"}, now_ms=0)

    with pytest.raises(InvalidTransitionError):
        db.transition("fx1", expected="queued", target="sent", now_ms=0)
    # Leaving executing requires the claiming worker.
    assert db.claim_effect("fx1", "worker-a", 0, 30_000) is True
    with pytest.raises(InvalidTransitionError):
        db.transition("fx1", expected="executing", target="sent", now_ms=0, worker_id="worker-b")
    with pytest.raises(InvalidTransitionError):
        db.transition("fx1", expected="executing", target="sent", now_ms=0)
    assert db.effect_state("fx1") == "executing"
    db.close()


def test_unknown_is_not_requeued_without_evidence(tmp_path):
    db = ProcessingStore(tmp_path / "p.db")
    db.enqueue_effect(effect_id="fx1", operation_key="k1", payload={"text": "A"}, now_ms=0)
    db.claim_effect("fx1", "worker-a", 0, 30_000)
    db.recover_executing(30_001)
    assert db.effect_state("fx1") == "unknown"

    with pytest.raises(InvalidTransitionError):
        db.transition("fx1", expected="unknown", target="queued", now_ms=40_000)
    assert db.transition(
        "fx1",
        expected="unknown",
        target="queued",
        now_ms=40_000,
        evidence={"kind": "not_executed", "detail": "provider lookup proved no dispatch"},
    ) is True
    assert db.effect_state("fx1") == "queued"
    db.close()


def test_evidence_records_do_not_downgrade_state(tmp_path):
    db = ProcessingStore(tmp_path / "p.db")
    db.enqueue_effect(effect_id="fx1", operation_key="k1", payload={"text": "A"}, now_ms=0)
    db.claim_effect("fx1", "worker-a", 0, 30_000)
    db.transition("fx1", expected="executing", target="sent", now_ms=1, worker_id="worker-a")
    db.record_evidence("fx1", kind="delivered", now_ms=2, detail="receipt")
    db.record_evidence("fx1", kind="read", now_ms=3, detail="read receipt")

    view = db.get_lineage("")
    assert len(view.effects) == 1
    effect = view.effects[0]
    assert effect.state == "sent"
    assert [item.kind for item in effect.evidence] == [
        "submitted",
        "claim",
        "state",
        "delivered",
        "read",
    ]
    assert [(attempt.outcome, attempt.finished_ms) for attempt in effect.attempts] == [
        ("sent", 1)
    ]
    stored = db.get_effect("fx1")
    assert stored is not None and stored.state == "sent"
    db.close()


@pytest.mark.asyncio
async def test_gateway_refuses_execution_without_authorizer_or_executor(tmp_path):
    db = ProcessingStore(tmp_path / "p.db")
    gateway = EffectGateway(db, clock=_Clock(0))
    receipt = gateway.submit(_envelope())
    assert receipt.state == "queued"

    with pytest.raises(ProcessingError):
        await gateway.execute_ready(receipt.effect_id)
    with pytest.raises(ProcessingError):
        await EffectGateway(db, executor=_Executor(), clock=_Clock(0)).execute_ready("fx1")
    assert db.effect_state("fx1") == "queued"
    db.close()


@pytest.mark.asyncio
async def test_submit_is_idempotent_and_never_claims_delivery(tmp_path):
    db = ProcessingStore(tmp_path / "p.db")
    gateway = EffectGateway(db, clock=_Clock(0))

    first = gateway.submit(_envelope(effect_id="fx1"))
    second = gateway.submit(_envelope(effect_id="fx2"))
    assert first.effect_id == "fx1"
    assert first.accepted is True
    assert second.effect_id == "fx1"
    assert second.accepted is False
    assert second.state == "queued"
    assert second.sent is False
    assert db.count_effects() == 1
    db.close()


@pytest.mark.asyncio
async def test_execute_ready_authorizes_claims_and_reports_sent(tmp_path):
    db = ProcessingStore(tmp_path / "p.db")
    authorizer = _Authorizer()
    executor = _Executor("sent")
    gateway = EffectGateway(
        db, authorizer=authorizer, executor=executor, worker_id="worker-a", clock=_Clock(0)
    )
    gateway.submit(_envelope())

    receipt = await gateway.execute_ready("fx1")

    assert executor.calls == ["fx1"]
    assert receipt.state == "sent"
    assert receipt.sent is True
    assert receipt.attempt_id
    assert authorizer.calls == ["fx1"]
    decision = db.get_decision("dec-fx1-allow")
    assert decision is not None and decision.policy_version == "policy-v1"
    assert db.effect_state("fx1") == "sent"

    # Second pass: no second transport call for a proven effect.
    again = await gateway.execute_ready("fx1")
    assert again.state == "sent"
    assert executor.calls == ["fx1"]
    db.close()


@pytest.mark.asyncio
async def test_transport_exception_becomes_unknown_and_is_not_retried(tmp_path):
    db = ProcessingStore(tmp_path / "p.db")
    executor = _Executor(error=TimeoutError("bridge timeout"))
    gateway = EffectGateway(
        db,
        authorizer=_Authorizer(),
        executor=executor,
        worker_id="worker-a",
        clock=_Clock(0),
    )
    gateway.submit(_envelope())

    receipt = await gateway.execute_ready("fx1")

    assert receipt.state == "unknown"
    assert db.effect_state("fx1") == "unknown"
    # No generic timeout retry, and no automatic re-execution of an unproven effect.
    retry = await gateway.execute_ready("fx1")
    assert retry.state == "unknown"
    assert executor.calls == ["fx1"]
    db.close()


@pytest.mark.asyncio
async def test_executor_not_executed_never_claims_success(tmp_path):
    db = ProcessingStore(tmp_path / "p.db")
    gateway = EffectGateway(
        db,
        authorizer=_Authorizer(),
        executor=_Executor("not_executed"),
        worker_id="worker-a",
        clock=_Clock(0),
    )
    gateway.submit(_envelope())

    receipt = await gateway.execute_ready("fx1")

    assert receipt.state == "failed"
    assert receipt.sent is False
    assert db.effect_state("fx1") == "failed"
    db.close()


@pytest.mark.asyncio
async def test_policy_denial_blocks_and_supersession_cancels(tmp_path):
    db = ProcessingStore(tmp_path / "p.db")
    denied = EffectGateway(
        db,
        authorizer=_Authorizer("permission_denied"),
        executor=_Executor(),
        clock=_Clock(0),
    )
    denied.submit(_envelope(effect_id="fx1", operation_key="k1"))
    assert (await denied.execute_ready("fx1")).state == "blocked"

    superseded = EffectGateway(
        db,
        authorizer=_Authorizer("superseded"),
        executor=_Executor(),
        clock=_Clock(0),
    )
    superseded.submit(_envelope(effect_id="fx2", operation_key="k2"))
    assert (await superseded.execute_ready("fx2")).state == "cancelled"
    assert db.effect_state("fx2") == "cancelled"
    db.close()


@pytest.mark.asyncio
async def test_expired_effects_are_never_executed(tmp_path):
    db = ProcessingStore(tmp_path / "p.db")
    executor = _Executor("sent")
    gateway = EffectGateway(
        db, authorizer=_Authorizer(), executor=executor, clock=_Clock(1_000)
    )

    receipt = gateway.submit(_envelope(effect_id="fx1", expires_at_ms=500))
    assert receipt.state == "expired"

    gateway.submit(_envelope(effect_id="fx2", operation_key="k2", expires_at_ms=2_000))
    later = EffectGateway(
        db, authorizer=_Authorizer(), executor=executor, clock=_Clock(2_500)
    )
    assert (await later.execute_ready("fx2")).state == "expired"
    assert executor.calls == []
    db.close()


@pytest.mark.asyncio
async def test_unknown_effect_is_never_silently_executed(tmp_path):
    db = ProcessingStore(tmp_path / "p.db")
    executor = _Executor("sent")
    gateway = EffectGateway(db, authorizer=_Authorizer(), executor=executor, clock=_Clock(0))
    gateway.submit(_envelope())
    db.claim_effect("fx1", "dead-worker", 0, 30_000)
    assert gateway.recover_expired_claims() == ()
    assert db.recover_executing(30_001) == ("fx1",)

    receipt = await gateway.execute_ready("fx1")

    assert receipt.state == "unknown"
    assert "reconciliation" in (receipt.detail or "")
    assert executor.calls == []
    db.close()
