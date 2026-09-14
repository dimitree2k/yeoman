"""Phase 01 — acceptance, recipient delivery and crash recovery for participation.

These tests use real temporary SQLite stores and synthetic identities. Nothing here
contacts a transport, a provider or a live chat. ``transport_accepted`` consumes the
send allowance; ``delivered`` requires exact recipient evidence; an unresolved hold
never regains capacity by crossing a window boundary (spec section 9).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
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


# -- approval binding and recovery (A02, A34, A35) -------------------------------------

OWNER = "owner@s.whatsapp.net"
GROUP = "group@g.us"
_FIXED_NOW = datetime(2026, 4, 25, 12, 0, tzinfo=UTC)


class _AllowSecurity:
    def check_output(self, text: str, context: dict[str, object] | None = None):
        del text, context
        from yeoman_gateway.core.models import SecurityDecision, SecurityResult

        return SecurityResult(
            stage="output", decision=SecurityDecision(action="allow", reason="ok")
        )


class _SanitizingSecurity(_AllowSecurity):
    def check_output(self, text: str, context: dict[str, object] | None = None):
        del context
        from yeoman_gateway.core.models import SecurityDecision, SecurityResult

        return SecurityResult(
            stage="output",
            decision=SecurityDecision(action="sanitize", reason="redacted"),
            sanitized_text=f"{text} [redacted]",
        )


class _FakeMemory:
    def search(self, **kwargs: object) -> list[object]:
        del kwargs
        return []


class _RecordingEffects:
    """A managed-only service effect producer: ``None`` is never success here."""

    def __init__(self, *, state: str = "sent", raises: bool = False) -> None:
        self.calls: list[dict[str, object]] = []
        self._state = state
        self._raises = raises

    def target_calls(self, chat_id: str) -> list[dict[str, object]]:
        return [call for call in self.calls if call.get("chat_id") == chat_id]

    async def send(self, **kwargs: object):
        require_managed = bool(kwargs.pop("require_managed", False))
        self.calls.append({**kwargs, "require_managed": require_managed})
        if self._raises:
            raise RuntimeError("transport unavailable")
        if require_managed and not kwargs.get("effect_id"):
            raise AssertionError("managed delivery requires a stable effect id")
        return EffectReceipt(
            effect_id=str(kwargs.get("effect_id") or ""),
            state=self._state,
            operation_key=str(kwargs.get("operation_ref") or ""),
            accepted=True,
            attempt_id="attempt-1",
            transport_receipt=(
                TransportReceipt(
                    channel=str(kwargs.get("channel")),
                    chat_id=str(kwargs.get("chat_id")),
                    provider_message_id="prov-1",
                    confirmed_ms=1,
                )
                if self._state == "sent"
                else None
            ),
        )


def _group_policy(*, group: bool = True) -> object:
    from yeoman_gateway.policy.schema import PolicyConfig

    return PolicyConfig.model_validate(
        {
            "owners": {"whatsapp": [OWNER]},
            "channels": {
                "whatsapp": {
                    "chats": {
                        OWNER: {"spontaneity": {"enabled": True, "profile": "helpful"}},
                        GROUP: {
                            "whoCanTalk": {"mode": "everyone"},
                            "whenToReply": {"mode": "all"},
                            **(
                                {"spontaneity": {"enabled": True, "profile": "balanced",
                                                  "preview": "owner_dm"}}
                                if group
                                else {}
                            ),
                        },
                    }
                }
            },
        }
    )


def _build_tools(tmp_path: Path, *, security: object | None = None, policy: object | None = None):
    from yeoman_gateway.bus.queue import MessageBus
    from yeoman_gateway.consciousness.approval import SpeakupApprovalStore
    from yeoman_gateway.consciousness.tools import ConsciousnessTools
    from yeoman_gateway.policy.engine import PolicyEngine
    from yeoman_gateway.storage.inbound_archive import InboundArchive
    from yeoman_shared.config.schema import Config, ConsciousnessConfig

    config = Config(
        consciousness=ConsciousnessConfig.model_validate(
            {
                "enabled": True,
                "ownerDmDefaultEnabled": False,
                "defaultDailyCap": 3,
                "approvalTimeoutSeconds": 3600,
                "maxSpeakupLengthChars": 200,
            }
        )
    )
    log = SpeakupLog(tmp_path / "speakups.db")
    store = SpeakupApprovalStore(
        tmp_path / "approvals.json", now=lambda: _FIXED_NOW.timestamp()
    )
    tools = ConsciousnessTools(
        config=config,
        policy_engine=PolicyEngine(policy or _group_policy(), workspace=tmp_path),
        bus=MessageBus(),
        log=log,
        inbound_archive=InboundArchive(tmp_path / "inbound.db"),
        memory=_FakeMemory(),
        security=security or _AllowSecurity(),
        approval_store=store,
        now=lambda: _FIXED_NOW,
    )
    tools.begin_run(trigger="cron")
    return tools, store, log


async def _previewed_group_proposal(tools, log) -> str:
    proposal = await tools.propose_speakup(
        chat_id=GROUP,
        message="hello group",
        action_type="observation",
        confidence=0.9,
    )
    proposal_id = str(proposal["proposal_id"])
    result = await tools.commit_speakup(proposal_id)
    assert result["status"] == "queued_for_approval"
    return proposal_id


@pytest.mark.asyncio
async def test_approval_binds_payload_revision(tmp_path: Path) -> None:
    """An approval for one payload cannot submit a later, different payload (A35)."""
    tools, store, log = _build_tools(tmp_path)
    tools._service_effects = _RecordingEffects()
    proposal_id = await _previewed_group_proposal(tools, log)
    approval = await store.get(proposal_id)
    assert approval is not None
    assert approval.payload_hash

    await log.record_approval_claim(
        proposal_id,
        owner_channel="whatsapp",
        owner_chat_id=OWNER,
        owner_id=OWNER,
        payload_hash=approval.payload_hash,
        proposal_revision=approval.proposal_revision,
        target_effect_id="",
        now_ms=1_000,
    )
    # A different payload now occupies the same proposal id.
    changed = await log.proposal_row(proposal_id)
    assert changed is not None
    tools._proposals[proposal_id] = tools._proposals[proposal_id].__class__(
        proposal_id=proposal_id,
        channel="whatsapp",
        chat_id=GROUP,
        message="a completely different message",
        action_type="observation",
        profile="balanced",
        confidence=0.9,
        trigger="cron",
        context_snapshot={},
    )
    result = await tools.submit_proposal(proposal_id)
    assert result["status"] == "rejected"
    assert result["reason"] == "approval_payload_changed"
    assert tools._service_effects.target_calls(GROUP) == []


@pytest.mark.asyncio
async def test_sanitizer_change_invalidates_approval(tmp_path: Path) -> None:
    tools, store, log = _build_tools(tmp_path, security=_SanitizingSecurity())
    effects = _RecordingEffects()
    tools._service_effects = effects
    proposal_id = await _previewed_group_proposal(tools, log)
    approval = await store.get(proposal_id)
    assert approval is not None
    await log.record_approval_claim(
        proposal_id,
        owner_channel="whatsapp",
        owner_chat_id=OWNER,
        owner_id=OWNER,
        payload_hash=approval.payload_hash,
        proposal_revision=1,
        target_effect_id="",
        now_ms=1_000,
    )
    result = await tools.submit_proposal(proposal_id)
    assert result == {"status": "rejected", "reason": "sanitized_payload_changed"}
    assert effects.target_calls(GROUP) == []


@pytest.mark.asyncio
async def test_approval_invalidated_by_off_after_preview(tmp_path: Path) -> None:
    """An approval cannot survive the owner switching the target chat off (A03)."""
    tools, store, log = _build_tools(tmp_path)
    effects = _RecordingEffects()
    tools._service_effects = effects
    proposal_id = await _previewed_group_proposal(tools, log)
    approval = await store.get(proposal_id)
    assert approval is not None
    await log.record_approval_claim(
        proposal_id,
        owner_channel="whatsapp",
        owner_chat_id=OWNER,
        owner_id=OWNER,
        payload_hash=approval.payload_hash,
        proposal_revision=1,
        target_effect_id="",
        now_ms=1_000,
    )
    # The owner switches the chat off before submitting the approval.
    tools.policy_engine = type(tools.policy_engine)(
        type(tools.policy_engine.policy).model_validate(
            {
                "owners": {"whatsapp": [OWNER]},
                "channels": {
                    "whatsapp": {
                        "chats": {
                            OWNER: {"spontaneity": {"enabled": True, "profile": "helpful"}},
                            GROUP: {"spontaneity": {"enabled": False}},
                        }
                    }
                },
            }
        ),
        workspace=tmp_path,
    )
    result = await tools.submit_proposal(proposal_id)
    assert result["status"] == "rejected"
    assert result["reason"] == "chat_not_eligible"
    assert effects.target_calls(GROUP) == []


@pytest.mark.asyncio
async def test_stale_quote_is_refused_before_submission(tmp_path: Path) -> None:
    """A quote that no longer exists is never silently dropped: the send is refused."""
    tools, store, log = _build_tools(tmp_path)
    effects = _RecordingEffects()
    proposal = await tools.propose_speakup(
        chat_id=OWNER,
        message="quoted answer",
        action_type="observation",
        confidence=0.9,
    )
    proposal_id = str(proposal["proposal_id"])
    cached = tools._proposals[proposal_id]
    from dataclasses import replace

    tools._proposals[proposal_id] = replace(cached, reply_to_message_id="vanished-msg")
    result = await tools.commit_speakup(proposal_id)
    assert result == {"status": "rejected", "reason": "stale_quote"}
    assert effects.target_calls(OWNER) == []
    assert effects.calls == []


@pytest.mark.asyncio
async def test_transport_exception_keeps_authorization_recoverable(tmp_path: Path) -> None:
    tools, store, log = _build_tools(tmp_path)
    tools._service_effects = _RecordingEffects()
    proposal_id = await _previewed_group_proposal(tools, log)
    failing = _RecordingEffects(raises=True)
    tools._service_effects = failing
    approval = await store.get(proposal_id)
    assert approval is not None
    await log.record_approval_claim(
        proposal_id,
        owner_channel="whatsapp",
        owner_chat_id=OWNER,
        owner_id=OWNER,
        payload_hash=approval.payload_hash,
        proposal_revision=1,
        target_effect_id="",
        now_ms=1_000,
    )
    with pytest.raises(RuntimeError):
        await tools.submit_proposal(proposal_id)
    claim = await log.approval_claim(proposal_id)
    assert claim is not None and claim["state"] == "claimed"
    row = await log.proposal_row(proposal_id)
    assert row is not None and row["status"] == "submitted"

    # The retry succeeds and converges on one target effect.
    retried = _RecordingEffects()
    tools._service_effects = retried
    again = await tools.submit_proposal(proposal_id)
    assert again["status"] == "transport_accepted"
    target_calls = retried.target_calls(GROUP)
    assert len(target_calls) == 1
    assert target_calls[0]["require_managed"] is True
    # The allowance is consumed exactly once, by the evidenced acceptance.
    assert await log.consumed_slots(
        channel="whatsapp",
        chat_id=GROUP,
        category="comment",
        now_ms=5_000,
        window_ms=1_800_000,
    ) == 1
    record = await log.delivery_record(
        proposal_id=proposal_id, effect_id=str(target_calls[0]["effect_id"])
    )
    assert record is not None and record["delivery_state"] == "transport_accepted"


@pytest.mark.asyncio
async def test_duplicate_approval_code_converges_on_one_effect(tmp_path: Path) -> None:
    tools, store, log = _build_tools(tmp_path)
    effects = _RecordingEffects()
    tools._service_effects = effects
    proposal_id = await _previewed_group_proposal(tools, log)
    approval = await store.get(proposal_id)
    assert approval is not None
    args = {
        "owner_channel": "whatsapp",
        "owner_chat_id": OWNER,
        "owner_id": OWNER,
        "payload_hash": approval.payload_hash,
        "proposal_revision": 1,
        "target_effect_id": "",
        "now_ms": 1_000,
    }
    assert await log.record_approval_claim(proposal_id, **args)
    assert await log.record_approval_claim(proposal_id, **args)
    first = await tools.submit_proposal(proposal_id)
    assert first["status"] == "transport_accepted"
    await log.resolve_approval_claim(proposal_id, resolution="submitted", now_ms=2_000)
    second = await tools.submit_proposal(proposal_id)
    assert second.get("duplicate") is True
    assert len(effects.target_calls(GROUP)) == 1
    assert await log.consumed_slots(
        channel="whatsapp",
        chat_id=GROUP,
        category="comment",
        now_ms=5_000,
        window_ms=1_800_000,
    ) == 1
    log.close()


@pytest.mark.asyncio
async def test_wrong_owner_claim_is_refused(tmp_path: Path) -> None:
    tools, store, log = _build_tools(tmp_path)
    tools._service_effects = _RecordingEffects()
    proposal_id = await _previewed_group_proposal(tools, log)
    approval = await store.get(proposal_id)
    assert approval is not None
    assert await log.record_approval_claim(
        proposal_id,
        owner_channel="whatsapp",
        owner_chat_id=OWNER,
        owner_id=OWNER,
        payload_hash=approval.payload_hash,
        proposal_revision=1,
        target_effect_id="",
        now_ms=1_000,
    )
    # A different owner chat may never adopt the same claim.
    assert not await log.record_approval_claim(
        proposal_id,
        owner_channel="whatsapp",
        owner_chat_id="intruder@s.whatsapp.net",
        owner_id="intruder@s.whatsapp.net",
        payload_hash=approval.payload_hash,
        proposal_revision=1,
        target_effect_id="",
        now_ms=1_100,
    )
    log.close()
