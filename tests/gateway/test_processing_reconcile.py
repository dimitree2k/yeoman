"""Plan 04 / R07: unknown effects are classified from durable evidence, never guessed."""

from __future__ import annotations

from pathlib import Path

import pytest
from yeoman_gateway.processing.effects import EffectGateway
from yeoman_gateway.processing.models import (
    EffectEnvelope,
    EffectReceipt,
    EffectTarget,
    PolicySnapshot,
    TextPayload,
)
from yeoman_gateway.processing.policy import SnapshotEffectAuthorizer
from yeoman_gateway.processing.reconcile import (
    LocalEvidenceProbe,
    ProbeOutcome,
    ProbeResult,
    probe_due_ms,
    reconcile_effect,
)
from yeoman_gateway.processing.store import ProcessingStore

CHAT = "chat@g.us"
T0 = 1_700_000_000_000


class _Clock:
    def __init__(self, value: int = T0) -> None:
        self.value = value

    def __call__(self) -> int:
        return self.value


class _Snapshots:
    def snapshot(self) -> PolicySnapshot:
        return PolicySnapshot(version="v1", policy_hash="h1", healthy=True)


class _AllowAll:
    def resolve(self, *, principal: str, target: EffectTarget, capability: str):
        return True, "allow"


class _FailingExecutor:
    """Transport that always times out: the outcome stays unproven."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def execute(self, envelope: EffectEnvelope) -> EffectReceipt:
        self.calls.append(envelope.effect_id)
        raise TimeoutError("bridge timeout")


def _unknown_effect(store: ProcessingStore, executor: _FailingExecutor | None = None) -> str:
    gateway = EffectGateway(
        store,
        authorizer=SnapshotEffectAuthorizer(snapshots=_Snapshots(), capabilities=_AllowAll()),
        executor=executor or _FailingExecutor(),
        clock=_Clock(),
    )
    gateway.submit(
        EffectEnvelope(
            effect_id="fx1",
            operation_key="k1",
            payload=TextPayload(text="hi"),
            target=EffectTarget(channel="whatsapp", chat_id=CHAT),
            capability="send_text",
        )
    )
    return "fx1"


@pytest.mark.asyncio
async def _make_unknown(store: ProcessingStore) -> str:
    effect_id = _unknown_effect(store)
    gateway = EffectGateway(
        store,
        authorizer=SnapshotEffectAuthorizer(snapshots=_Snapshots(), capabilities=_AllowAll()),
        executor=_FailingExecutor(),
        clock=_Clock(),
    )
    await gateway.execute_ready(effect_id)
    return effect_id


@pytest.mark.asyncio
async def test_transport_receipt_confirms_an_unknown_effect(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "p.db")
    effect_id = await _make_unknown(store)
    assert store.effect_state(effect_id) == "unknown"
    store.record_transport_receipt(
        effect_id, channel="whatsapp", chat_id=CHAT, provider_message_id="3EB0", now_ms=T0
    )

    result = await reconcile_effect(store, effect_id, probe=LocalEvidenceProbe(store), now_ms=T0 + 1)

    assert result.outcome is ProbeOutcome.CONFIRMED
    assert store.effect_state(effect_id) == "sent"
    store.close()


@pytest.mark.asyncio
async def test_late_confirmation_corrects_a_nonrepeatable_effect(tmp_path: Path) -> None:
    """An escalated effect is never shortened to failed, and late proof still wins."""
    from yeoman_gateway.processing.models import InvalidTransitionError
    from yeoman_gateway.processing.signals import attach_receipt_evidence

    store = ProcessingStore(tmp_path / "p.db")
    effect_id = await _make_unknown(store)
    assert store.transition(
        effect_id,
        expected="unknown",
        target="unknown_nonrepeatable",
        now_ms=T0 + 1,
        evidence={"reason": "probe_deadline"},
    )
    assert store.effect_state(effect_id) == "unknown_nonrepeatable"

    # The escalation is not a failure verdict: the state machine refuses `failed`.
    with pytest.raises(InvalidTransitionError):
        store.transition(
            effect_id, expected="unknown_nonrepeatable", target="failed", now_ms=T0 + 2
        )
    assert store.effect_state(effect_id) == "unknown_nonrepeatable"

    # A late transport proof still corrects it, and the evidence stays visible.
    store.record_transport_receipt(
        effect_id, channel="whatsapp", chat_id=CHAT, provider_message_id="3EB0", now_ms=T0 + 3
    )
    attach_receipt_evidence(store, effect_id, now_ms=T0 + 4)
    result = await reconcile_effect(
        store, effect_id, probe=LocalEvidenceProbe(store), now_ms=T0 + 5
    )

    assert result.outcome is ProbeOutcome.CONFIRMED
    assert store.effect_state(effect_id) == "sent"
    lineage = store.get_lineage("")
    assert lineage.effects[0].state == "sent"
    store.close()


@pytest.mark.asyncio
async def test_delivery_signal_confirms_without_a_receipt(tmp_path: Path) -> None:
    from yeoman_gateway.processing.signals import WhatsAppSignalMapper

    store = ProcessingStore(tmp_path / "p.db")
    effect_id = await _make_unknown(store)
    store.record_transport_receipt(
        effect_id, channel="whatsapp", chat_id=CHAT, provider_message_id="3EB0", now_ms=T0
    )
    signal = WhatsAppSignalMapper().map(
        {"chatJid": CHAT, "messageId": "3EB0", "recipientJid": "4915@s.whatsapp.net",
         "status": "read"},
        kind="receipt",
    )
    store.append_event(
        event_key=signal.event_key, event_id=signal.event_id, trace_id=signal.trace_id,
        payload=signal.to_event_payload(), now_ms=T0,
    )
    # Use a probe that has no transport receipt to prove stage 2 works on its own.
    store.record_transport_receipt(
        effect_id, channel="whatsapp", chat_id=CHAT, provider_message_id="3EB0", now_ms=T0 + 1
    )

    result = await reconcile_effect(store, effect_id, probe=LocalEvidenceProbe(store), now_ms=T0 + 2)

    assert result.outcome is ProbeOutcome.CONFIRMED
    assert store.effect_state(effect_id) == "sent"
    store.close()


@pytest.mark.asyncio
async def test_absence_of_evidence_is_inconclusive_not_not_executed(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "p.db")
    effect_id = await _make_unknown(store)

    result = await reconcile_effect(store, effect_id, probe=LocalEvidenceProbe(store), now_ms=T0 + 1)

    assert result.outcome is ProbeOutcome.INCONCLUSIVE
    assert store.effect_state(effect_id) == "unknown"  # never shortened to failed
    store.close()


@pytest.mark.asyncio
async def test_deadline_escalates_without_claiming_failure(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "p.db")
    effect_id = await _make_unknown(store)

    result = await reconcile_effect(
        store,
        effect_id,
        probe=LocalEvidenceProbe(store),
        now_ms=T0 + 700_000,
        deadline_ms=T0 + 600_000,
    )

    assert result.state == "unknown_nonrepeatable"
    assert store.effect_state(effect_id) == "unknown_nonrepeatable"
    evidence = [item.kind for item in store.list_effects()[0].evidence]
    assert "operator" in evidence  # the escalation is visible and needs a decision
    store.close()


@pytest.mark.asyncio
async def test_not_executed_requeues_for_the_normal_dispatch_path(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "p.db")
    effect_id = await _make_unknown(store)

    class _ProvenAbsent:
        async def probe(self, request):
            return ProbeResult(ProbeOutcome.NOT_EXECUTED, "pre-dispatch refusal")

    result = await reconcile_effect(store, effect_id, probe=_ProvenAbsent(), now_ms=T0 + 1)

    assert result.state == "queued"
    assert store.effect_state(effect_id) == "queued"  # re-execution needs a fresh dispatch
    store.close()


@pytest.mark.asyncio
async def test_reconciliation_never_executes_the_effect(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "p.db")
    executor = _FailingExecutor()
    effect_id = _unknown_effect(store, executor)
    gateway = EffectGateway(
        store,
        authorizer=SnapshotEffectAuthorizer(snapshots=_Snapshots(), capabilities=_AllowAll()),
        executor=executor,
        clock=_Clock(),
    )
    await gateway.execute_ready(effect_id)
    assert executor.calls == ["fx1"]

    # Reconciling twice changes nothing and never touches the transport again.
    await reconcile_effect(store, effect_id, probe=LocalEvidenceProbe(store), now_ms=T0 + 1)
    await reconcile_effect(store, effect_id, probe=LocalEvidenceProbe(store), now_ms=T0 + 2)
    assert executor.calls == ["fx1"]

    # An unknown effect stays refused by the gateway until evidence says otherwise.
    receipt = await gateway.execute_ready(effect_id)
    assert receipt.state == "unknown"
    assert "reconciliation required" in (receipt.detail or "")
    assert executor.calls == ["fx1"]
    store.close()


@pytest.mark.asyncio
async def test_operator_decision_closes_an_escalated_effect(tmp_path: Path) -> None:
    from yeoman_gateway.processing.reconcile import operator_decision

    store = ProcessingStore(tmp_path / "p.db")
    effect_id = await _make_unknown(store)
    await reconcile_effect(
        store, effect_id, probe=LocalEvidenceProbe(store), now_ms=T0 + 700_000,
        deadline_ms=T0 + 600_000,
    )

    result = operator_decision(
        store,
        effect_id,
        decision="confirmed_sent",
        operator_id="owner",
        policy_version="policy.json@1",
        policy_hash="h1",
        now_ms=T0 + 800_000,
    )

    assert result.state == "sent"
    assert store.effect_state(effect_id) == "sent"
    decisions = [d for d in store.get_lineage("").decisions if d.stage == "admin"]
    assert len(decisions) == 1
    store.close()


def test_probe_schedule_follows_the_backoff() -> None:
    backoff = (5, 15, 45, 120, 300, 600)
    assert probe_due_ms(T0, attempt_number=1, backoff_seconds=backoff) == T0 + 5_000
    assert probe_due_ms(T0, attempt_number=2, backoff_seconds=backoff) == T0 + 20_000
    assert probe_due_ms(T0, attempt_number=3, backoff_seconds=backoff) == T0 + 65_000
    assert probe_due_ms(T0, attempt_number=4, backoff_seconds=backoff) == T0 + 185_000


# --------------------------------------------------------------------------------------
# Plan 04 task 8: late delivery/read evidence, never a state downgrade
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_evidence_never_moves_an_effect_to_sent(tmp_path: Path) -> None:
    from yeoman_gateway.processing.signals import (
        WhatsAppSignalMapper,
        attach_receipt_evidence,
    )

    store = ProcessingStore(tmp_path / "p.db")
    effect_id = await _make_unknown(store)
    store.record_transport_receipt(
        effect_id, channel="whatsapp", chat_id=CHAT, provider_message_id="3EB0", now_ms=T0
    )
    # The receipt signal arrived before the transport receipt was correlated.
    signal = WhatsAppSignalMapper().map(
        {"chatJid": CHAT, "messageId": "3EB0", "recipientJid": "4915@s.whatsapp.net",
         "status": "read"},
        kind="receipt",
    )
    store.append_event(
        event_key=signal.event_key, event_id=signal.event_id, trace_id=signal.trace_id,
        payload=signal.to_event_payload(), now_ms=T0,
    )

    attached = attach_receipt_evidence(store, effect_id, now_ms=T0 + 1)

    assert [item.kind for item in attached] == ["read"]
    assert store.effect_state(effect_id) == "unknown"  # a read is not transport acceptance
    details = [item.detail for item in store.list_effects()[0].evidence]
    assert any("recipient=" in (detail or "") for detail in details)
    # Idempotent: attaching twice does not grow the evidence.
    assert attach_receipt_evidence(store, effect_id, now_ms=T0 + 2) == ()
    store.close()


@pytest.mark.asyncio
async def test_late_evidence_never_downgrades_a_sent_effect(tmp_path: Path) -> None:
    from yeoman_gateway.processing.signals import (
        WhatsAppSignalMapper,
        attach_receipt_evidence,
    )

    store = ProcessingStore(tmp_path / "p.db")
    gateway = EffectGateway(
        store,
        authorizer=SnapshotEffectAuthorizer(snapshots=_Snapshots(), capabilities=_AllowAll()),
        executor=_SentExecutor(),
        clock=_Clock(),
    )
    _unknown_effect(store)
    await gateway.execute_ready("fx1")
    assert store.effect_state("fx1") == "sent"

    # A late delete signal and a late read must change nothing.
    delete_signal = WhatsAppSignalMapper().map(
        {"chatJid": CHAT, "messageId": "3EB0"}, kind="delete"
    )
    store.append_event(
        event_key=delete_signal.event_key, event_id=delete_signal.event_id,
        trace_id=delete_signal.trace_id, payload=delete_signal.to_event_payload(), now_ms=T0 + 5,
    )
    store.record_transport_receipt(
        "fx1", channel="whatsapp", chat_id=CHAT, provider_message_id="3EB0", now_ms=T0 + 6
    )
    attach_receipt_evidence(store, "fx1", now_ms=T0 + 7)

    assert store.effect_state("fx1") == "sent"
    lineage = store.get_lineage("")
    assert lineage.effects[0].state == "sent"
    store.close()


@pytest.mark.asyncio
async def test_lineage_exposes_evidence_without_raw_content(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "p.db")
    effect_id = await _make_unknown(store)
    store.record_transport_receipt(
        effect_id, channel="whatsapp", chat_id=CHAT, provider_message_id="3EB0", now_ms=T0
    )

    view = store.get_lineage("")
    rendered = repr(view)

    assert "hi" not in rendered  # no payload text
    assert "4915" not in rendered  # no raw JID
    assert view.effects[0].payload_available is True
    store.close()


class _SentExecutor:
    async def execute(self, envelope: EffectEnvelope) -> EffectReceipt:
        return EffectReceipt(effect_id=envelope.effect_id, state="sent")
