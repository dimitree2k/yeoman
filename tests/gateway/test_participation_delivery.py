"""Phase 01 — acceptance, recipient delivery and crash recovery for participation.

These tests use real temporary SQLite stores and synthetic identities. Nothing here
contacts a transport, a provider or a live chat. ``transport_accepted`` consumes the
send allowance; ``delivered`` requires exact recipient evidence; an unresolved hold
never regains capacity by crossing a window boundary (spec section 9).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from yeoman_gateway.consciousness.log import SpeakupLog, deterministic_effect_id
from yeoman_gateway.processing.effects import EffectGateway
from yeoman_gateway.processing.models import (
    EffectEnvelope,
    EffectReceipt,
    ReactionPayload,
    TextPayload,
    TransportReceipt,
    TurnRef,
)
from yeoman_gateway.processing.store import ProcessingStore

CHAT = "synthetic@g.us"
CHANNEL = "whatsapp"
DAY_MS = 86_400_000
HOUR_MS = 3_600_000


async def _proposal(log: SpeakupLog, proposal_id: str) -> None:
    await log.record_proposed(
        proposal_id=proposal_id,
        channel=CHANNEL,
        chat_id=CHAT,
        action_type="observation",
        profile="balanced",
        message="Synthetic contribution.",
        trigger="burst",
        context_snapshot={},
        now=1.0,
    )


def _effect_id(proposal_id: str, operation: str = "comment") -> str:
    return deterministic_effect_id(
        channel=CHANNEL, chat_id=CHAT, operation=operation, proposal_id=proposal_id
    )


# -- reservations ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_simultaneous_reservations_under_limit_one_yield_one_success(
    tmp_path: Path,
) -> None:
    """Two genuinely concurrent writers cannot both win the last slot.

    The ledger's reservation path is synchronous on purpose (a check-then-send race
    must not exist), so real contention is exercised from worker threads.
    """
    log = SpeakupLog(tmp_path / "speakups.db")
    await _proposal(log, "p1")
    await _proposal(log, "p2")

    def reserve(proposal_id: str, effect_id: str) -> bool:
        return log.reserve_delivery_sync(
            proposal_id=proposal_id,
            effect_id=effect_id,
            channel=CHANNEL,
            chat_id=CHAT,
            now_ms=1000,
            limits=(("initiation", 1, DAY_MS, "calendar_day"),),
        )

    results = await asyncio.gather(
        asyncio.to_thread(reserve, "p1", "e1"),
        asyncio.to_thread(reserve, "p2", "e2"),
    )
    assert sorted(results) == [False, True]
    assert await log.consumed_slots(
        channel=CHANNEL,
        chat_id=CHAT,
        category="initiation",
        now_ms=1000,
        window_ms=DAY_MS,
        window_kind="calendar_day",
    ) == 1
    log.close()


@pytest.mark.asyncio
async def test_duplicate_reservation_for_same_effect_is_idempotent(tmp_path: Path) -> None:
    log = SpeakupLog(tmp_path / "speakups.db")
    await _proposal(log, "p1")
    limits = (("comment", 1, HOUR_MS),)
    assert await log.reserve_delivery(
        proposal_id="p1", effect_id="e1", channel=CHANNEL, chat_id=CHAT, now_ms=1000, limits=limits
    )
    assert await log.reserve_delivery(
        proposal_id="p1", effect_id="e1", channel=CHANNEL, chat_id=CHAT, now_ms=2000, limits=limits
    )
    assert await log.consumed_slots(
        channel=CHANNEL, chat_id=CHAT, category="comment", now_ms=2000, window_ms=HOUR_MS
    ) == 1
    log.close()


@pytest.mark.asyncio
async def test_zero_limit_denies_and_unknown_hold_survives_midnight(tmp_path: Path) -> None:
    log = SpeakupLog(tmp_path / "speakups.db")
    await _proposal(log, "p1")
    await _proposal(log, "p2")
    assert not await log.reserve_delivery(
        proposal_id="p1",
        effect_id="e1",
        channel=CHANNEL,
        chat_id=CHAT,
        now_ms=1000,
        limits=(("comment", 0, HOUR_MS),),
    )
    # Accepted just before midnight: an *unknown* outcome keeps the hold afterwards.
    assert await log.reserve_delivery(
        proposal_id="p2",
        effect_id="e2",
        channel=CHANNEL,
        chat_id=CHAT,
        now_ms=DAY_MS - 1000,
        limits=(("initiation", 1, DAY_MS, "calendar_day"),),
    )
    await log.note_delivery_unknown(
        "p2",
        effect_id="e2",
        evidence_kind="dispatch_unknown",
        evidence_ref="attempt-1",
        now_ms=DAY_MS - 500,
    )
    assert not await log.reserve_delivery(
        proposal_id="p3",
        effect_id="e3",
        channel=CHANNEL,
        chat_id=CHAT,
        now_ms=DAY_MS + 1000,
        limits=(("initiation", 1, DAY_MS, "calendar_day"),),
    )
    log.close()


@pytest.mark.asyncio
async def test_definite_failure_releases_and_acceptance_never_refunds(tmp_path: Path) -> None:
    log = SpeakupLog(tmp_path / "speakups.db")
    await _proposal(log, "p1")
    await _proposal(log, "p2")
    limits = (("comment", 1, HOUR_MS),)
    assert await log.reserve_delivery(
        proposal_id="p1", effect_id="e1", channel=CHANNEL, chat_id=CHAT, now_ms=1000, limits=limits
    )
    assert await log.release_delivery(
        "p1", effect_id="e1", state="failed", reason="transport_refused", now_ms=1100
    )
    assert await log.reserve_delivery(
        proposal_id="p2", effect_id="e2", channel=CHANNEL, chat_id=CHAT, now_ms=1200, limits=limits
    )
    await log.project_transport_accepted(
        "p2",
        effect_id="e2",
        provider_message_id="prov-1",
        evidence_kind="transport_receipt",
        evidence_ref="receipt-1",
        now_ms=1300,
    )
    # An accepted send cannot be refunded, even by a later failure claim.
    assert not await log.release_delivery(
        "p2", effect_id="e2", state="failed", reason="too_late", now_ms=1400
    )
    assert await log.delivery_state(proposal_id="p2", effect_id="e2") == "transport_accepted"
    log.close()


@pytest.mark.asyncio
async def test_exhausting_either_dimension_prevents_the_whole_reservation(
    tmp_path: Path,
) -> None:
    log = SpeakupLog(tmp_path / "speakups.db")
    await _proposal(log, "p1")
    await _proposal(log, "p2")
    await _proposal(log, "p3")
    both = (("initiation", 2, DAY_MS, "calendar_day"), ("comment", 1, HOUR_MS))
    assert await log.reserve_delivery(
        proposal_id="p1", effect_id="e1", channel=CHANNEL, chat_id=CHAT, now_ms=1000, limits=both
    )
    # Initiation still has room, comment does not: nothing may be reserved.
    assert not await log.reserve_delivery(
        proposal_id="p2", effect_id="e2", channel=CHANNEL, chat_id=CHAT, now_ms=1100, limits=both
    )
    assert await log.delivery_state(proposal_id="p2", effect_id="e2") is None
    assert await log.consumed_slots(
        channel=CHANNEL,
        chat_id=CHAT,
        category="initiation",
        now_ms=1100,
        window_ms=DAY_MS,
        window_kind="calendar_day",
    ) == 1
    log.close()


@pytest.mark.asyncio
async def test_duplicate_acceptance_and_delivery_receipts_consume_once(tmp_path: Path) -> None:
    log = SpeakupLog(tmp_path / "speakups.db")
    await _proposal(log, "p1")
    limits = (("comment", 2, HOUR_MS),)
    assert await log.reserve_delivery(
        proposal_id="p1", effect_id="e1", channel=CHANNEL, chat_id=CHAT, now_ms=1000, limits=limits
    )
    for _ in range(2):
        state = await log.project_transport_accepted(
            "p1",
            effect_id="e1",
            provider_message_id="prov-1",
            evidence_kind="transport_receipt",
            evidence_ref="receipt-1",
            now_ms=1100,
        )
        assert state == "transport_accepted"
    assert await log.consumed_slots(
        channel=CHANNEL, chat_id=CHAT, category="comment", now_ms=1200, window_ms=HOUR_MS
    ) == 1
    assert await log.project_recipient_delivery(
        "p1",
        effect_id="e1",
        provider_message_id="prov-1",
        evidence_kind="recipient_delivery",
        evidence_ref="signal-1",
        now_ms=1300,
    )
    assert not await log.project_recipient_delivery(
        "p1",
        effect_id="e1",
        provider_message_id="prov-1",
        evidence_kind="recipient_delivery",
        evidence_ref="signal-1",
        now_ms=1400,
    )
    assert await log.consumed_slots(
        channel=CHANNEL, chat_id=CHAT, category="comment", now_ms=1500, window_ms=HOUR_MS
    ) == 1
    log.close()


@pytest.mark.asyncio
async def test_provider_id_is_accepted_not_delivered(tmp_path: Path) -> None:
    """A provider message id alone never becomes a delivered anchor (A33)."""
    log = SpeakupLog(tmp_path / "speakups.db")
    await _proposal(log, "p1")
    await log.reserve_delivery(
        proposal_id="p1",
        effect_id=_effect_id("p1"),
        channel=CHANNEL,
        chat_id=CHAT,
        now_ms=1000,
        limits=(("comment", 2, HOUR_MS),),
    )
    await log.project_transport_accepted(
        "p1",
        effect_id=_effect_id("p1"),
        provider_message_id="prov-1",
        evidence_kind="transport_receipt",
        evidence_ref="receipt-1",
        now_ms=1100,
    )
    record = await log.delivery_record(proposal_id="p1", effect_id=_effect_id("p1"))
    assert record is not None
    assert record["delivery_state"] == "transport_accepted"
    assert record["delivered_at_ms"] is None
    assert await log.delivered_reservation_rows(
        channel=CHANNEL, chat_id=CHAT, since_ms=0, limit=10
    ) == []
    log.close()


@pytest.mark.asyncio
async def test_recipient_signal_advances_after_restart(tmp_path: Path) -> None:
    """The reservation survives close/reopen and only then becomes delivered (A18)."""
    db_path = tmp_path / "speakups.db"
    log = SpeakupLog(db_path)
    await _proposal(log, "p1")
    await log.reserve_delivery(
        proposal_id="p1",
        effect_id="e1",
        channel=CHANNEL,
        chat_id=CHAT,
        now_ms=1000,
        limits=(("comment", 2, HOUR_MS),),
    )
    await log.project_transport_accepted(
        "p1",
        effect_id="e1",
        provider_message_id="prov-1",
        evidence_kind="transport_receipt",
        evidence_ref="receipt-1",
        now_ms=1100,
    )
    log.close()

    reopened = SpeakupLog(db_path)
    assert await reopened.delivery_state(proposal_id="p1", effect_id="e1") == "transport_accepted"
    assert await reopened.project_recipient_delivery(
        "p1",
        effect_id="e1",
        provider_message_id="prov-1",
        evidence_kind="recipient_read",
        evidence_ref="signal-9",
        now_ms=5000,
    )
    rows = await reopened.delivered_reservation_rows(
        channel=CHANNEL, chat_id=CHAT, since_ms=0, limit=10
    )
    assert [row["effect_id"] for row in rows] == ["e1"]
    reopened.close()


@pytest.mark.asyncio
async def test_rejects_model_assertions_as_evidence(tmp_path: Path) -> None:
    log = SpeakupLog(tmp_path / "speakups.db")
    await _proposal(log, "p1")
    await log.reserve_delivery(
        proposal_id="p1",
        effect_id="e1",
        channel=CHANNEL,
        chat_id=CHAT,
        now_ms=1000,
        limits=(("comment", 2, HOUR_MS),),
    )
    with pytest.raises(ValueError):
        await log.project_recipient_delivery(
            "p1",
            effect_id="e1",
            provider_message_id="prov-1",
            evidence_kind="model_assertion",
            evidence_ref="judge",
            now_ms=1100,
        )
    with pytest.raises(ValueError):
        await log.project_transport_accepted(
            "p1",
            effect_id="e1",
            provider_message_id="prov-1",
            evidence_kind="recipient_delivery",
            evidence_ref="signal",
            now_ms=1100,
        )
    log.close()


# -- crash recovery --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_crash_after_reservation_before_effect_recovers_same_identity(
    tmp_path: Path,
) -> None:
    """Reservation saved, effect absent: the same fixed effect id creates it once."""
    log_path = tmp_path / "speakups.db"
    log = SpeakupLog(log_path)
    await _proposal(log, "p1")
    effect_id = _effect_id("p1")
    await log.reserve_delivery(
        proposal_id="p1",
        effect_id=effect_id,
        channel=CHANNEL,
        chat_id=CHAT,
        now_ms=1000,
        limits=(("comment", 1, HOUR_MS),),
    )
    log.close()

    reopened = SpeakupLog(log_path)
    rows = await reopened.pending_delivery_reservations()
    assert [row["effect_id"] for row in rows] == [effect_id]
    assert rows[0]["attempt_state"] == "unsubmitted"

    store = ProcessingStore(tmp_path / "processing.db")
    gateway = EffectGateway(store, executor=_NeverCalledExecutor())
    envelope = _envelope(effect_id)
    first = gateway.submit(envelope)
    second = gateway.submit(envelope)
    assert first.effect_id == effect_id
    assert second.effect_id == effect_id
    assert store.count_effects() == 1
    reopened.close()
    store.close()


@pytest.mark.asyncio
async def test_accepted_receipt_without_ledger_projection_is_repaired_once(
    tmp_path: Path,
) -> None:
    """Effect state is truth; a missing ledger projection is repaired, not resent."""
    log = SpeakupLog(tmp_path / "speakups.db")
    await _proposal(log, "p1")
    effect_id = _effect_id("p1")
    await log.reserve_delivery(
        proposal_id="p1",
        effect_id=effect_id,
        channel=CHANNEL,
        chat_id=CHAT,
        now_ms=1000,
        limits=(("comment", 1, HOUR_MS),),
    )
    await log.mark_status("p1", status="submitted")

    store = ProcessingStore(tmp_path / "processing.db")
    executor = _RecordingExecutor()
    gateway = EffectGateway(store, authorizer=_AllowAll(), executor=executor)
    envelope = _envelope(effect_id)
    gateway.submit(envelope)
    receipt = await gateway.execute_ready(effect_id)
    assert receipt.state == "sent"
    assert executor.calls == 1

    # Crash before the ledger projection: the transport receipt is the repair source.
    transport = store.effect_transport_receipt(effect_id)
    assert transport is not None and transport.provider_message_id == "prov-1"
    state = await log.project_transport_accepted(
        "p1",
        effect_id=effect_id,
        provider_message_id=transport.provider_message_id,
        evidence_kind="transport_receipt",
        evidence_ref=transport.receipt_id or effect_id,
        now_ms=2000,
    )
    assert state == "transport_accepted"
    # A second repair pass is a no-op and never resends.
    again = await gateway.execute_ready(effect_id)
    assert again.state == "sent"
    assert executor.calls == 1
    assert await log.consumed_slots(
        channel=CHANNEL, chat_id=CHAT, category="comment", now_ms=3000, window_ms=HOUR_MS
    ) == 1
    log.close()
    store.close()


@pytest.mark.asyncio
async def test_reaction_effect_receipt_is_not_a_handled_boolean(tmp_path: Path) -> None:
    """A reaction returns an effect id plus receipt state, never a bare handled flag (A33)."""
    log = SpeakupLog(tmp_path / "speakups.db")
    await _proposal(log, "p1")
    effect_id = _effect_id("p1", "reaction")
    assert await log.reserve_delivery(
        proposal_id="p1",
        effect_id=effect_id,
        channel=CHANNEL,
        chat_id=CHAT,
        now_ms=1000,
        limits=(("reaction", 1, HOUR_MS),),
    )
    store = ProcessingStore(tmp_path / "processing.db")
    executor = _RecordingExecutor(provider_message_id=None)
    gateway = EffectGateway(store, authorizer=_AllowAll(), executor=executor)
    gateway.submit(_envelope(effect_id, reaction=True))
    receipt = await gateway.execute_ready(effect_id)
    assert receipt.effect_id == effect_id
    assert receipt.state == "sent"
    # Routed and accepted, but there is no recipient evidence for a reaction.
    assert await log.project_transport_accepted(
        "p1",
        effect_id=effect_id,
        provider_message_id=None,
        evidence_kind="effect_sent",
        evidence_ref=receipt.attempt_id or effect_id,
        now_ms=1500,
    ) == "transport_accepted"
    assert await log.delivered_reservation_rows(
        channel=CHANNEL, chat_id=CHAT, since_ms=0, limit=10
    ) == []
    log.close()
    store.close()


@pytest.mark.asyncio
async def test_reaction_reservation_routed_but_failed_creates_no_success_metric(
    tmp_path: Path,
) -> None:
    log = SpeakupLog(tmp_path / "speakups.db")
    await _proposal(log, "p1")
    effect_id = _effect_id("p1", "reaction")
    await log.reserve_delivery(
        proposal_id="p1",
        effect_id=effect_id,
        channel=CHANNEL,
        chat_id=CHAT,
        now_ms=1000,
        limits=(("reaction", 1, HOUR_MS),),
    )
    # The transport proved it never dispatched: capacity is released, nothing succeeded.
    assert await log.release_delivery(
        "p1", effect_id=effect_id, state="failed", reason="not_executed", now_ms=1100
    )
    record = await log.delivery_record(proposal_id="p1", effect_id=effect_id)
    assert record is not None and record["delivery_state"] == "failed"
    assert await log.delivered_reservation_rows(
        channel=CHANNEL, chat_id=CHAT, since_ms=0, limit=10
    ) == []
    assert await log.consumed_slots(
        channel=CHANNEL, chat_id=CHAT, category="reaction", now_ms=1200, window_ms=HOUR_MS
    ) == 0
    log.close()


# -- helpers ---------------------------------------------------------------------------


def _envelope(effect_id: str, *, reaction: bool = False) -> EffectEnvelope:
    payload = (
        ReactionPayload(message_id="inbound-1", emoji="\N{THUMBS UP SIGN}")
        if reaction
        else TextPayload(text="Synthetic contribution.")
    )
    return EffectEnvelope(
        effect_id=effect_id,
        operation_key=f"test:{effect_id}",
        payload=payload,
        target={"channel": CHANNEL, "chat_id": CHAT},
        trace_id=effect_id,
        turn_id="turn-1",
        turn_revision=1,
        principal="service:speakup",
        capability="send_reaction" if reaction else "send_text",
    )


class _AllowAll:
    def check(self, envelope: EffectEnvelope, current_turn: TurnRef | None):
        del envelope, current_turn
        from yeoman_gateway.processing.models import DecisionRecord

        return DecisionRecord(
            decision_id="d1",
            trace_id="t1",
            stage="final",
            policy_version="v1",
            policy_hash="h1",
            principal="service:speakup",
            target="whatsapp",
            capability="send_text",
            turn_revision=1,
            outcome="allow",
            reason="ok",
            created_ms=1,
        )


class _RecordingExecutor:
    def __init__(self, *, provider_message_id: str | None = "prov-1") -> None:
        self.calls = 0
        self._provider_message_id = provider_message_id

    async def execute(self, envelope: EffectEnvelope) -> EffectReceipt:
        self.calls += 1
        return EffectReceipt(
            effect_id=envelope.effect_id,
            state="sent",
            operation_key=envelope.operation_key,
            accepted=True,
            transport_receipt=TransportReceipt(
                channel=CHANNEL,
                chat_id=CHAT,
                provider_message_id=self._provider_message_id,
                confirmed_ms=2,
                detail="synthetic",
            ),
        )


class _NeverCalledExecutor:
    async def execute(self, envelope: EffectEnvelope) -> EffectReceipt:
        raise AssertionError(f"must not execute {envelope.effect_id}")
