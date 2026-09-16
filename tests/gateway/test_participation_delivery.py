"""Phase 01 — acceptance, recipient delivery and crash recovery for participation.

These tests use real temporary SQLite stores and synthetic identities. Nothing here
contacts a transport, a provider or a live chat. ``transport_accepted`` consumes the
send allowance; ``delivered`` requires exact recipient evidence; an unresolved hold
never regains capacity by crossing a window boundary (spec section 9).
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from yeoman_gateway.consciousness.log import SpeakupLog, deterministic_effect_id
from yeoman_gateway.processing.dispatch import (
    BusEffectExecutor,
    EffectNotDeliveredError,
    IntentEffectRouter,
    SendBudget,
    ServiceEffectProducer,
)
from yeoman_gateway.processing.effects import EffectGateway, is_pre_dispatch_error
from yeoman_gateway.processing.models import (
    EffectConflictError,
    EffectEnvelope,
    EffectReceipt,
    EffectTarget,
    ParticipationPreDispatchDenied,
    PolicySnapshot,
    ProcessingError,
    ReactionPayload,
    TextPayload,
    TransportReceipt,
    TurnRef,
    payload_hash,
)
from yeoman_gateway.processing.policy import SnapshotEffectAuthorizer
from yeoman_gateway.processing.store import ProcessingStore

CHAT = "synthetic@g.us"
CHANNEL = "whatsapp"
DAY_MS = 86_400_000
HOUR_MS = 3_600_000


def test_participation_effect_binds_admission_and_origin_atomically(tmp_path: Path) -> None:
    """The outbox must persist a trusted admission link with the queued effect."""
    store = ProcessingStore(tmp_path / "processing.db")
    envelope = EffectEnvelope(
        effect_id="effect-admission-1",
        operation_key="participation:admission-1",
        payload=TextPayload(text="prepared"),
        target=EffectTarget(channel=CHANNEL, chat_id=CHAT),
        principal="service:speakup",
        capability="send_text",
        origin="participation",
        admission_id="admission-1",
    )
    admission = SimpleNamespace(
        admission_id="admission-1",
        opportunity_id="opportunity-1",
        channel=CHANNEL,
        chat_id=CHAT,
        activation_epoch=3,
        lane="production",
        observed_revision=1,
        action="comment",
        intent="observation",
        purpose="synthetic",
        emoji=None,
        target_message_id=None,
        anchor_message_id=None,
        payload_hash=envelope.payload_hash,
    )

    effect_id = store.enqueue_participation_effect(envelope, admission)

    assert effect_id == "effect-admission-1"
    stored = store.get_effect(effect_id)
    assert stored is not None
    assert stored.origin == "participation"
    assert stored.admission_id == "admission-1"
    assert store.get_participation_admission("admission-1") is not None
    assert store.enqueue_participation_effect(envelope, admission) == effect_id
    duplicate_admission = EffectEnvelope(
        effect_id="effect-admission-duplicate",
        operation_key="participation:admission-duplicate",
        payload=TextPayload(text="prepared"),
        target=EffectTarget(channel=CHANNEL, chat_id=CHAT),
        principal="service:speakup",
        capability="send_text",
        origin="participation",
        admission_id="admission-1",
    )
    with pytest.raises(EffectConflictError, match="admission_id"):
        store.enqueue_participation_effect(duplicate_admission, admission)
    changed = EffectEnvelope(
        effect_id="effect-admission-1",
        operation_key="participation:admission-1",
        payload=TextPayload(text="changed"),
        target=EffectTarget(channel=CHANNEL, chat_id=CHAT),
        principal="service:speakup",
        capability="send_text",
        origin="participation",
        admission_id="admission-1",
    )
    with pytest.raises(EffectConflictError):
        store.enqueue_participation_effect(changed, admission)
    with pytest.raises(EffectConflictError, match="target"):
        store.enqueue_effect(
            effect_id="effect-foreign-target",
            operation_key="participation:foreign-target",
            payload=TextPayload(text="prepared"),
            target=EffectTarget(channel=CHANNEL, chat_id="other@g.us"),
            origin="participation",
            admission_id="admission-1",
            now_ms=1,
        )
    with pytest.raises(EffectConflictError, match="payload hash"):
        store.enqueue_effect(
            effect_id="effect-foreign-payload",
            operation_key="participation:foreign-payload",
            payload=TextPayload(text="foreign"),
            target=EffectTarget(channel=CHANNEL, chat_id=CHAT),
            origin="participation",
            admission_id="admission-1",
            now_ms=1,
        )
    assert store.get_effect("effect-foreign-target") is None
    assert store.get_effect("effect-foreign-payload") is None
    store.close()
    reopened = ProcessingStore(tmp_path / "processing.db")
    persisted = reopened.get_effect(effect_id)
    assert persisted is not None
    assert persisted.origin == "participation"
    assert persisted.admission_id == "admission-1"
    assert reopened.get_participation_admission("admission-1") is not None
    reopened.close()


def test_generic_enqueue_cannot_create_unbound_participation_effect(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "processing.db")
    with pytest.raises(ProcessingError, match="admission"):
        store.enqueue_effect(
            effect_id="effect-unbound",
            operation_key="participation:unbound",
            payload=TextPayload(text="prepared"),
            target=EffectTarget(channel=CHANNEL, chat_id=CHAT),
            origin="participation",
            admission_id="missing",
            now_ms=1,
        )
    assert store.get_effect("effect-unbound") is None
    envelope = EffectEnvelope(
        effect_id="effect-missing-hash",
        operation_key="participation:missing-hash",
        payload=TextPayload(text="prepared"),
        target=EffectTarget(channel=CHANNEL, chat_id=CHAT),
        origin="participation",
        admission_id="admission-missing-hash",
    )
    with pytest.raises(ProcessingError, match="payload hash"):
        store.enqueue_participation_effect(
            envelope,
            SimpleNamespace(
                admission_id="admission-missing-hash",
                channel=CHANNEL,
                chat_id=CHAT,
                payload_hash="",
            ),
        )
    assert store.get_effect("effect-missing-hash") is None
    store.close()


def test_snapshot_authorizer_rejects_missing_participation_hash_and_action() -> None:
    envelope = EffectEnvelope(
        effect_id="effect-incomplete-admission",
        operation_key="participation:incomplete-admission",
        payload=TextPayload(text="prepared"),
        target=EffectTarget(channel=CHANNEL, chat_id=CHAT),
        principal="service:speakup",
        capability="send_text",
        origin="participation",
        admission_id="admission-incomplete",
    )
    admission = SimpleNamespace(
        admission_id="admission-incomplete",
        channel=CHANNEL,
        chat_id=CHAT,
        payload_hash="",
        action="",
        intent="observation",
    )
    authorizer = SnapshotEffectAuthorizer(
        snapshots=_StaticPolicy(),
        capabilities=_TargetCapabilities(),
        admission_loader=lambda _admission_id: admission,
        participation_authorizer=lambda _envelope, _admission: (True, "allow"),
        clock=lambda: 2,
    )

    decision = authorizer.check(envelope, None)

    assert decision.outcome == "deny"
    assert decision.reason == "participation_payload_hash_missing"


@pytest.mark.parametrize(
    ("reservation_state", "source_principals_authorized", "expected"),
    [
        ("reserved", True, "participation_reservation_not_submitted"),
        ("submitted", False, "participation_source_principal_not_authorized"),
    ],
)
def test_snapshot_authorizer_requires_submitted_reservation_and_source_acl(
    reservation_state: str,
    source_principals_authorized: bool,
    expected: str,
) -> None:
    envelope = EffectEnvelope(
        effect_id="effect-request-evidence",
        operation_key="participation:request-evidence",
        payload=TextPayload(text="prepared"),
        target=EffectTarget(channel=CHANNEL, chat_id=CHAT),
        principal="service:speakup",
        capability="send_text",
        origin="participation",
        admission_id="admission-request-evidence",
    )
    admission = SimpleNamespace(
        admission_id="admission-request-evidence",
        channel=CHANNEL,
        chat_id=CHAT,
        payload_hash=envelope.payload_hash,
        action="comment",
        intent="observation",
        activation_epoch=3,
    )
    request = SimpleNamespace(
        admission=admission,
        lane="production",
        is_paused=None,
        is_shadow=False,
        feature_enabled=True,
        opted_in=True,
        current_epoch=3,
        source_authorized=True,
        effect_id=envelope.effect_id,
        reservation_state=reservation_state,
        payload_hash=envelope.payload_hash,
        expected_payload_hash=envelope.payload_hash,
        source_principals_authorized=source_principals_authorized,
    )

    class _Checker:
        def check(self, _request: object) -> tuple[bool, str]:
            return True, "allow"

    authorizer = SnapshotEffectAuthorizer(
        snapshots=_StaticPolicy(),
        capabilities=_TargetCapabilities(),
        admission_loader=lambda _admission_id: admission,
        participation_authorizer=_Checker(),
        participation_request_builder=lambda _envelope, _admission: request,
        clock=lambda: 2,
    )

    decision = authorizer.check(envelope, None)

    assert decision.outcome == "deny"
    assert decision.reason == expected


def test_legacy_effect_rows_get_legacy_provenance_on_store_open(tmp_path: Path) -> None:
    path = tmp_path / "legacy-processing.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO meta (key, value) VALUES ('schema_version', '7');
        CREATE TABLE effects (
          effect_id TEXT PRIMARY KEY,
          operation_key TEXT NOT NULL UNIQUE,
          trace_id TEXT NOT NULL DEFAULT '',
          turn_id TEXT NOT NULL DEFAULT '',
          turn_revision INTEGER NOT NULL DEFAULT 1,
          principal TEXT NOT NULL DEFAULT '',
          capability TEXT NOT NULL DEFAULT '',
          target_json TEXT NOT NULL DEFAULT '{}',
          target_hash TEXT NOT NULL DEFAULT '',
          payload_kind TEXT NOT NULL,
          payload_hash TEXT NOT NULL,
          payload_json TEXT,
          payload_purged_ms INTEGER,
          state TEXT NOT NULL,
          expires_at_ms INTEGER,
          policy_version TEXT,
          policy_hash TEXT,
          lease_owner TEXT,
          lease_until_ms INTEGER,
          created_ms INTEGER NOT NULL,
          updated_ms INTEGER NOT NULL
        );
        """
    )
    connection.execute(
        """
        INSERT INTO effects (
          effect_id, operation_key, payload_kind, payload_hash, payload_json,
          state, created_ms, updated_ms
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("legacy-effect", "legacy-operation", "text", "hash", '{"kind":"text","text":"x"}', "queued", 1, 1),
    )
    connection.commit()
    connection.close()

    store = ProcessingStore(path)
    stored = store.get_effect("legacy-effect")
    assert stored is not None
    assert stored.origin == "legacy"
    assert stored.admission_id is None
    store.close()


def test_participation_pre_dispatch_denial_is_classified_by_type() -> None:
    assert is_pre_dispatch_error(ParticipationPreDispatchDenied("paused_chat"))


class _TargetCapabilities:
    def resolve(self, *, principal: str, target: EffectTarget, capability: str):
        del capability
        return (principal == "service:speakup" and target.chat_id == CHAT, "allow")


class _StaticPolicy:
    def snapshot(self) -> PolicySnapshot:
        return PolicySnapshot(version="policy-v1", policy_hash="hash-v1", loaded_ms=1)


class _TransportSpy:
    def __init__(self, ledger: object | None = None) -> None:
        self.calls = 0
        self.ledger = ledger

    async def execute(self, envelope: EffectEnvelope) -> EffectReceipt:
        self.calls += 1
        if self.ledger is not None:
            assert getattr(self.ledger, "calls", []) == [
                ("opportunity-router", envelope.effect_id)
            ]
        return EffectReceipt(effect_id=envelope.effect_id, state="sent")


class _Bus:
    def __init__(self) -> None:
        self.outbound_calls = 0
        self.reaction_calls = 0

    async def publish_outbound(self, _message: object) -> None:
        self.outbound_calls += 1
        raise AssertionError("participation must not use the legacy bus")

    async def publish_reaction(self, _message: object) -> None:
        self.reaction_calls += 1
        raise AssertionError("participation must not use the legacy bus")


class _LedgerAttemptSpy:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.release_calls: list[tuple[str, str, str]] = []
        self.reservation_state = "reserved"

    async def record_send_attempt(self, opportunity_id: str, *, effect_id: str, now_ms: int) -> None:
        del now_ms
        self.calls.append((opportunity_id, effect_id))
        self.reservation_state = "submitted"

    async def release_delivery(
        self,
        proposal_id: str,
        *,
        effect_id: str,
        state: str,
        reason: str,
        now_ms: int,
    ) -> bool:
        del reason, now_ms
        self.release_calls.append((proposal_id, effect_id, state))
        self.reservation_state = state
        return True


def test_real_effect_gateway_rechecks_persisted_participation_before_transport(
    tmp_path: Path,
) -> None:
    store = ProcessingStore(tmp_path / "processing.db")
    envelope = EffectEnvelope(
        effect_id="effect-final-check",
        operation_key="participation:final-check",
        payload=TextPayload(text="prepared"),
        target=EffectTarget(channel=CHANNEL, chat_id=CHAT),
        principal="service:speakup",
        capability="send_text",
        origin="participation",
        admission_id="admission-final-check",
    )
    admission = SimpleNamespace(
        admission_id="admission-final-check",
        opportunity_id="opportunity-final-check",
        channel=CHANNEL,
        chat_id=CHAT,
        activation_epoch=3,
        lane="production",
        observed_revision=1,
        action="comment",
        intent="observation",
        purpose="synthetic",
        emoji=None,
        target_message_id=None,
        anchor_message_id=None,
        payload_hash=payload_hash(TextPayload(text="prepared")),
    )
    store.enqueue_participation_effect(envelope, admission)
    transport = _TransportSpy()
    allowed = {"value": True}
    authorizer = SnapshotEffectAuthorizer(
        snapshots=_StaticPolicy(),
        capabilities=_TargetCapabilities(),
        admission_loader=store.get_participation_admission,
        participation_authorizer=lambda _envelope, _admission: (
            allowed["value"],
            "paused_chat" if not allowed["value"] else "allow",
        ),
        clock=lambda: 2,
    )
    gateway = EffectGateway(
        store,
        authorizer=authorizer,
        executor=transport,
        clock=lambda: 2,
    )

    first = asyncio.run(gateway.execute_ready(envelope.effect_id))
    assert first.state == "sent"
    assert transport.calls == 1

    # A newly queued effect is denied by the fresh participation check after a pause.
    paused_envelope = EffectEnvelope(
        effect_id="effect-final-check-paused",
        operation_key="participation:final-check-paused",
        payload=TextPayload(text="prepared"),
        target=EffectTarget(channel=CHANNEL, chat_id=CHAT),
        principal="service:speakup",
        capability="send_text",
        origin="participation",
        admission_id="admission-final-check-paused",
    )
    paused_data = vars(admission).copy()
    paused_data["admission_id"] = "admission-final-check-paused"
    paused_admission = SimpleNamespace(**paused_data)
    store.enqueue_participation_effect(paused_envelope, paused_admission)
    allowed["value"] = False
    denied = asyncio.run(gateway.execute_ready(paused_envelope.effect_id))
    assert denied.state == "blocked"
    assert transport.calls == 1
    store.close()


def test_service_producer_carries_admission_through_real_effect_gateway(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "processing.db")
    admission = SimpleNamespace(
        admission_id="admission-router",
        opportunity_id="opportunity-router",
        channel=CHANNEL,
        chat_id=CHAT,
        activation_epoch=3,
        lane="production",
        observed_revision=1,
        action="comment",
        intent="observation",
        purpose="synthetic",
        emoji=None,
        target_message_id=None,
        anchor_message_id=None,
        payload_hash=payload_hash(TextPayload(text="prepared")),
    )
    ledger = _LedgerAttemptSpy()
    transport = _TransportSpy(ledger)
    authorizer = SnapshotEffectAuthorizer(
        snapshots=_StaticPolicy(),
        capabilities=_TargetCapabilities(),
        admission_loader=store.get_participation_admission,
        participation_authorizer=lambda _envelope, _admission: (True, "allow"),
        clock=lambda: 2,
    )
    gateway = EffectGateway(
        store,
        authorizer=authorizer,
        executor=transport,
        clock=lambda: 2,
    )
    config = SimpleNamespace(
        processing=SimpleNamespace(
            enabled=True,
            is_chat_enabled=lambda channel, chat_id: channel == CHANNEL and chat_id == CHAT,
            budgets=None,
            deadlines=SimpleNamespace(reactive_ms=60_000, proactive_ms=60_000),
        )
    )
    router = IntentEffectRouter(
        gateway=gateway,
        config=config,
        clock=lambda: 2,
        participation_ledger=ledger,
    )
    producer = ServiceEffectProducer(router=router, bus=_Bus())

    receipt = asyncio.run(
        producer.send(
            source="speakup",
            operation_ref="opportunity-router",
            channel=CHANNEL,
            chat_id=CHAT,
            content="prepared",
            effect_id="effect-router",
            require_managed=True,
            admission=admission,
        )
    )

    assert receipt is not None
    assert receipt.state == "sent"
    assert ledger.calls == [("opportunity-router", "effect-router")]
    stored = store.get_effect("effect-router")
    assert stored is not None
    assert stored.origin == "participation"
    assert stored.admission_id == "admission-router"
    assert transport.calls == 1
    store.close()


@pytest.mark.asyncio
async def test_participation_capacity_block_releases_submitted_reservation(
    tmp_path: Path,
) -> None:
    log = SpeakupLog(tmp_path / "speakups.db")
    await _proposal(log, "opportunity-capacity")
    effect_id = "effect-capacity"
    assert await log.reserve_delivery(
        proposal_id="opportunity-capacity",
        effect_id=effect_id,
        channel=CHANNEL,
        chat_id=CHAT,
        now_ms=1000,
        limits=(("comment", 1, HOUR_MS),),
    )
    store = ProcessingStore(tmp_path / "processing.db")
    transport = _TransportSpy()
    gateway = EffectGateway(store, authorizer=_AllowAll(), executor=transport, clock=lambda: 2)
    config = SimpleNamespace(
        processing=SimpleNamespace(
            enabled=True,
            is_chat_enabled=lambda channel, chat_id: channel == CHANNEL and chat_id == CHAT,
            budgets=None,
            deadlines=SimpleNamespace(reactive_ms=60_000, proactive_ms=60_000),
        )
    )
    router = IntentEffectRouter(
        gateway=gateway,
        config=config,
        clock=lambda: 2,
        budget=SendBudget(units=1, window_seconds=60, waiting_cap=0),
        participation_ledger=log,
    )
    admission = SimpleNamespace(
        admission_id="admission-capacity",
        opportunity_id="opportunity-capacity",
        channel=CHANNEL,
        chat_id=CHAT,
        activation_epoch=3,
        lane="production",
        observed_revision=1,
        action="comment",
        intent="observation",
        purpose="synthetic",
        emoji=None,
        target_message_id=None,
        anchor_message_id=None,
        payload_hash=payload_hash(TextPayload(text="prepared")),
    )
    producer = ServiceEffectProducer(router=router, bus=_Bus())

    receipt = await producer.send(
        source="speakup",
        operation_ref="opportunity-capacity",
        channel=CHANNEL,
        chat_id=CHAT,
        content="prepared",
        effect_id=effect_id,
        require_managed=True,
        admission=admission,
    )

    assert receipt is not None and receipt.state == "blocked"
    assert transport.calls == 0
    assert await log.delivery_state(
        proposal_id="opportunity-capacity", effect_id=effect_id
    ) == "failed"
    assert await log.consumed_slots(
        channel=CHANNEL,
        chat_id=CHAT,
        category="comment",
        now_ms=1000,
        window_ms=HOUR_MS,
    ) == 0
    log.close()
    store.close()


@pytest.mark.asyncio
async def test_managed_speakup_reaction_requires_admission() -> None:
    class _ManagedRouter:
        def manages(self, channel: str, chat_id: str) -> bool:
            del channel, chat_id
            return True

        async def submit_message(self, _message: object, **kwargs: object) -> object:
            del kwargs
            raise AssertionError("unbound managed reaction reached the router")

    producer = ServiceEffectProducer(router=_ManagedRouter(), bus=_Bus())

    with pytest.raises(EffectNotDeliveredError, match="admission"):
        await producer.send_reaction(
            source="speakup",
            operation_ref="reaction-without-admission",
            channel=CHANNEL,
            chat_id=CHAT,
            message_id="inbound-1",
            emoji="👍",
            effect_id="effect-unbound-reaction",
            require_managed=True,
        )


def test_dispatch_preflight_denial_is_before_bus_handoff(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "processing.db")
    envelope = EffectEnvelope(
        effect_id="effect-dispatch-check",
        operation_key="participation:dispatch-check",
        payload=TextPayload(text="prepared"),
        target=EffectTarget(channel=CHANNEL, chat_id=CHAT),
        principal="service:speakup",
        capability="send_text",
        origin="participation",
        admission_id="admission-dispatch-check",
    )
    admission = SimpleNamespace(
        admission_id="admission-dispatch-check",
        opportunity_id="opportunity-dispatch-check",
        channel=CHANNEL,
        chat_id=CHAT,
        activation_epoch=3,
        lane="production",
        observed_revision=1,
        action="comment",
        intent="observation",
        purpose="synthetic",
        payload_hash=envelope.payload_hash,
    )
    store.enqueue_participation_effect(envelope, admission)
    bus = _Bus()
    executor = BusEffectExecutor(
        bus=bus,
        participation_pre_dispatch=lambda _envelope: (False, "paused_chat"),
    )
    gateway = EffectGateway(
        store,
        authorizer=SnapshotEffectAuthorizer(
            snapshots=_StaticPolicy(),
            capabilities=_TargetCapabilities(),
            admission_loader=store.get_participation_admission,
            participation_authorizer=lambda _envelope, _admission: (True, "allow"),
            clock=lambda: 2,
        ),
        executor=executor,
        clock=lambda: 2,
    )

    result = asyncio.run(gateway.execute_ready(envelope.effect_id))

    assert result.state == "failed"
    assert bus.outbound_calls == 0
    assert bus.reaction_calls == 0
    store.close()


@pytest.mark.asyncio
async def test_participation_security_payload_change_is_denied_before_transport() -> None:
    envelope = EffectEnvelope(
        effect_id="effect-security-change",
        operation_key="participation:security-change",
        payload=TextPayload(text="secret"),
        target=EffectTarget(channel=CHANNEL, chat_id=CHAT),
        principal="service:speakup",
        capability="send_text",
        origin="participation",
        admission_id="admission-security-change",
    )
    executor = BusEffectExecutor(
        bus=_Bus(),
        security=_SanitizingSecurity(),
        participation_pre_dispatch=lambda _envelope: (True, "allow"),
    )

    with pytest.raises(
        ParticipationPreDispatchDenied,
        match="participation_payload_changed_by_security",
    ):
        await executor.execute(envelope)


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


class _ExplodingExecutor:
    """A transport that raised after dispatch: the effect outcome is unproven."""

    async def execute(self, envelope: EffectEnvelope) -> EffectReceipt:
        raise RuntimeError(f"connection reset while sending {envelope.effect_id}")


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


# -- delivered anchors (A18, A23, A32) -------------------------------------------------


@pytest.mark.asyncio
async def test_delivered_anchors_require_recipient_evidence(tmp_path: Path) -> None:
    """Only confirmed recipient deliveries become conversational anchors."""
    from yeoman_gateway.consciousness.delivery import DeliveryAnchorReader

    log = SpeakupLog(tmp_path / "speakups.db")
    store = ProcessingStore(tmp_path / "processing.db")
    reader = DeliveryAnchorReader(log=log, store=store)

    # A real text effect in the processing store, one per proposal state.
    for index, effect_id in enumerate([f"e{number}" for number in range(6)]):
        gateway = EffectGateway(store, authorizer=_AllowAll(), executor=_RecordingExecutor())
        gateway.submit(_envelope(effect_id))
        await gateway.execute_ready(effect_id)
        await _proposal(log, f"p{index}")
        await log.reserve_delivery(
            proposal_id=f"p{index}",
            effect_id=effect_id,
            channel=CHANNEL,
            chat_id=CHAT,
            now_ms=1000,
            limits=(("comment", 10, HOUR_MS),),
        )
        if index >= 1:  # every state except the first is given recipient evidence
            await log.project_transport_accepted(
                f"p{index}",
                effect_id=effect_id,
                provider_message_id=f"prov-{index}",
                evidence_kind="transport_receipt",
                evidence_ref=f"receipt-{index}",
                now_ms=1100,
            )
    # Only p1 has authenticated recipient evidence.
    await log.project_recipient_delivery(
        "p1",
        effect_id="e1",
        provider_message_id="prov-1",
        evidence_kind="recipient_delivery",
        evidence_ref="signal-1",
        now_ms=1200,
    )
    anchors = await reader.delivered_anchors(CHANNEL, CHAT, since_ms=0, limit=10)
    assert [anchor["effect_id"] for anchor in anchors] == ["e1"]
    anchor = anchors[0]
    assert anchor["provider_message_id"] == "prov-1"
    assert anchor["delivered_at_ms"] == 1200
    assert anchor["channel"] == CHANNEL and anchor["chat_id"] == CHAT
    assert anchor["evidence_kind"] == "recipient_delivery"
    assert anchor["evidence_ref"] == "signal-1"
    assert anchor["message"] == "Synthetic contribution."
    assert anchor["delivery_state"] == "delivered"
    store.close()
    log.close()


@pytest.mark.asyncio
async def test_delivered_anchors_survive_restart_and_do_not_leak_chats(tmp_path: Path) -> None:
    from yeoman_gateway.consciousness.delivery import DeliveryAnchorReader

    log = SpeakupLog(tmp_path / "speakups.db")
    store = ProcessingStore(tmp_path / "processing.db")
    gateway = EffectGateway(store, authorizer=_AllowAll(), executor=_RecordingExecutor())
    gateway.submit(_envelope("e1"))
    await gateway.execute_ready("e1")
    await _proposal(log, "p1")
    await log.reserve_delivery(
        proposal_id="p1",
        effect_id="e1",
        channel=CHANNEL,
        chat_id=CHAT,
        now_ms=1000,
        limits=(("comment", 10, HOUR_MS),),
    )
    await log.project_recipient_delivery(
        "p1",
        effect_id="e1",
        provider_message_id="prov-1",
        evidence_kind="recipient_delivery",
        evidence_ref="signal-1",
        now_ms=1200,
    )
    log.close()
    store.close()

    reopened_log = SpeakupLog(tmp_path / "speakups.db")
    reopened_store = ProcessingStore(tmp_path / "processing.db")
    reader = DeliveryAnchorReader(log=reopened_log, store=reopened_store)
    anchors = await reader.delivered_anchors(CHANNEL, CHAT, since_ms=0, limit=10)
    assert len(anchors) == 1
    assert await reader.delivered_anchors(
        CHANNEL, "other@g.us", since_ms=0, limit=10
    ) == []
    assert await reader.delivered_anchors(
        "telegram", CHAT, since_ms=0, limit=10
    ) == []
    reopened_store.close()
    reopened_log.close()


@pytest.mark.asyncio
async def test_delivered_anchors_deduplicate_repeated_callbacks(tmp_path: Path) -> None:
    from yeoman_gateway.consciousness.delivery import DeliveryAnchorReader

    log = SpeakupLog(tmp_path / "speakups.db")
    store = ProcessingStore(tmp_path / "processing.db")
    gateway = EffectGateway(store, authorizer=_AllowAll(), executor=_RecordingExecutor())
    gateway.submit(_envelope("e1"))
    await gateway.execute_ready("e1")
    await _proposal(log, "p1")
    await log.reserve_delivery(
        proposal_id="p1",
        effect_id="e1",
        channel=CHANNEL,
        chat_id=CHAT,
        now_ms=1000,
        limits=(("comment", 10, HOUR_MS),),
    )
    for _ in range(3):
        await log.project_recipient_delivery(
            "p1",
            effect_id="e1",
            provider_message_id="prov-1",
            evidence_kind="recipient_read",
            evidence_ref="signal-1",
            now_ms=1200,
        )
    reader = DeliveryAnchorReader(log=log, store=store)
    anchors = await reader.delivered_anchors(CHANNEL, CHAT, since_ms=0, limit=10)
    assert len(anchors) == 1
    store.close()
    log.close()


# -- receipt reconciliation (A17, A34) -------------------------------------------------


async def _reserved_effect(
    log: SpeakupLog,
    store: ProcessingStore,
    *,
    proposal_id: str,
    effect_id: str,
    execute: bool = False,
) -> "_RecordingExecutor":
    gateway = EffectGateway(store, authorizer=_AllowAll(), executor=_RecordingExecutor())
    executor = gateway._executor
    gateway.submit(_envelope(effect_id))
    await _proposal(log, proposal_id)
    await log.reserve_delivery(
        proposal_id=proposal_id,
        effect_id=effect_id,
        channel=CHANNEL,
        chat_id=CHAT,
        now_ms=1000,
        limits=(("comment", 10, HOUR_MS),),
    )
    await log.record_send_attempt(proposal_id, effect_id=effect_id, now_ms=1000)
    if execute:
        await gateway.execute_ready(effect_id)
    assert isinstance(executor, _RecordingExecutor)
    return executor


@pytest.mark.asyncio
async def test_reconciler_projects_acceptance_and_then_delivery(tmp_path: Path) -> None:
    from yeoman_gateway.consciousness.delivery import ParticipationReceiptReconciler

    log = SpeakupLog(tmp_path / "speakups.db")
    store = ProcessingStore(tmp_path / "processing.db")
    await _reserved_effect(
        log, store, proposal_id="p1", effect_id="e1", execute=True
    )
    reconciler = ParticipationReceiptReconciler(log=log, store=store)
    first = await reconciler.reconcile(now_ms=5000)
    assert first["accepted"] == 1
    assert await log.delivery_state(proposal_id="p1", effect_id="e1") == "transport_accepted"
    assert await log.consumed_slots(
        channel=CHANNEL, chat_id=CHAT, category="comment", now_ms=5100, window_ms=HOUR_MS
    ) == 1

    # A later delivery signal advances the same reservation once.
    _append_receipt(store, chat_id=CHAT, provider_message_id="prov-1")
    second = await reconciler.reconcile(now_ms=6000)
    assert second["delivered"] == 1
    assert await log.delivery_state(proposal_id="p1", effect_id="e1") == "delivered"
    third = await reconciler.reconcile(now_ms=7000)
    assert third["delivered"] == 0
    assert await log.consumed_slots(
        channel=CHANNEL, chat_id=CHAT, category="comment", now_ms=7100, window_ms=HOUR_MS
    ) == 1
    store.close()
    log.close()


@pytest.mark.asyncio
async def test_reconciler_releases_definite_effect_failure(tmp_path: Path) -> None:
    from yeoman_gateway.consciousness.delivery import ParticipationReceiptReconciler

    log = SpeakupLog(tmp_path / "speakups.db")
    store = ProcessingStore(tmp_path / "processing.db")
    gateway = EffectGateway(store, authorizer=_AllowAll(), executor=_RecordingExecutor())
    gateway.submit(_envelope("e1"))
    await _proposal(log, "p1")
    await log.reserve_delivery(
        proposal_id="p1",
        effect_id="e1",
        channel=CHANNEL,
        chat_id=CHAT,
        now_ms=1000,
        limits=(("comment", 1, HOUR_MS),),
    )
    await log.record_send_attempt("p1", effect_id="e1", now_ms=1000)
    store.transition(
        "e1", expected="queued", target="failed", now_ms=1100,
        evidence={"kind": "not_executed", "detail": "synthetic refusal"},
    )
    reconciler = ParticipationReceiptReconciler(log=log, store=store)
    counters = await reconciler.reconcile(now_ms=5000)
    assert counters["released"] == 1
    assert await log.delivery_state(proposal_id="p1", effect_id="e1") == "failed"
    assert await log.consumed_slots(
        channel=CHANNEL, chat_id=CHAT, category="comment", now_ms=5100, window_ms=HOUR_MS
    ) == 0
    store.close()
    log.close()


@pytest.mark.asyncio
async def test_reconciler_retains_unknown_and_missing_effects(tmp_path: Path) -> None:
    from yeoman_gateway.consciousness.delivery import ParticipationReceiptReconciler

    log = SpeakupLog(tmp_path / "speakups.db")
    store = ProcessingStore(tmp_path / "processing.db")
    # Reservations saved with no processing effect at all (crash between stores).
    await _proposal(log, "p1")
    await log.reserve_delivery(
        proposal_id="p1",
        effect_id="missing",
        channel=CHANNEL,
        chat_id=CHAT,
        now_ms=1000,
        limits=(("comment", 1, HOUR_MS),),
    )
    # Reservations whose effect outcome is unproven: the transport raised after the
    # frame may already have been written, so the effect store records ``unknown``.
    gateway = EffectGateway(store, authorizer=_AllowAll(), executor=_ExplodingExecutor())
    gateway.submit(_envelope("e2"))
    await _proposal(log, "p2")
    await log.reserve_delivery(
        proposal_id="p2",
        effect_id="e2",
        channel=CHANNEL,
        chat_id=CHAT,
        now_ms=1000,
        limits=(("comment", 2, HOUR_MS),),
    )
    await log.record_send_attempt("p2", effect_id="e2", now_ms=1000)
    receipt = await gateway.execute_ready("e2")
    assert receipt.state == "unknown"
    reconciler = ParticipationReceiptReconciler(log=log, store=store)
    counters = await reconciler.reconcile(now_ms=5000)
    assert counters["skipped"] == 1
    assert counters["retained"] == 1
    assert await log.delivery_state(proposal_id="p1", effect_id="missing") == "reserved"
    assert await log.delivery_state(proposal_id="p2", effect_id="e2") == "delivery_unknown"
    # Both holds survive a window boundary.
    assert await log.consumed_slots(
        channel=CHANNEL,
        chat_id=CHAT,
        category="comment",
        now_ms=1000 + 2 * HOUR_MS,
        window_ms=HOUR_MS,
    ) == 2
    store.close()
    log.close()


def _append_receipt(
    store: ProcessingStore,
    *,
    chat_id: str,
    provider_message_id: str,
    status: str = "delivered",
) -> str:
    payload = {"status": status, "recipient_token": "sha256:synthetic"}
    return store.append_event(
        event_key=f"whatsapp:{chat_id}:receipt:{provider_message_id}:synthetic:{status}",
        event_id=f"receipt-{provider_message_id}-{status}",
        trace_id=f"trace-{provider_message_id}",
        payload={
            "kind": "receipt",
            "channel": CHANNEL,
            "chat_id": chat_id,
            "principal": "participant@s.whatsapp.net",
            "source_message_id": provider_message_id,
            "target_message_id": provider_message_id,
            "occurred_ms": 1200,
            **payload,
        },
        now_ms=1200,
    )


# -- historical rows and restart truth (A23, A24) --------------------------------------


@pytest.mark.asyncio
async def test_historical_sent_row_is_preserved_but_not_verified(tmp_path: Path) -> None:
    """A legacy ``sent`` row stays as history and never becomes learning evidence (A23)."""
    log = SpeakupLog(tmp_path / "speakups.db")
    await _proposal(log, "legacy")
    await log.mark_sent("legacy", now=1.0)
    row = await log.proposal_row("legacy")
    assert row is not None and row["status"] == "sent"
    # No reservation, no provider receipt, no delivered anchor and no outcome sample.
    assert await log.delivered_reservation_rows(
        channel=CHANNEL, chat_id=CHAT, since_ms=0, limit=10
    ) == []
    assert await log.participation_outcome_samples(
        channel=CHANNEL, chat_id=CHAT, limit=10
    ) == []
    assert await log.pending_outcome_deliveries(before_ms=10**12) == []
    log.close()


@pytest.mark.asyncio
async def test_restart_drops_speculative_queue_but_keeps_durable_truth(
    tmp_path: Path,
) -> None:
    """Restart keeps attempts, approvals and effects; speculative batching is gone (A24)."""
    from yeoman_gateway.consciousness.opportunities import OpportunityScheduler
    from yeoman_gateway.consciousness.participation_runtime import SourceOwner

    db_path = tmp_path / "speakups.db"
    log = SpeakupLog(db_path)
    await _proposal(log, "p1")
    await log.reserve_delivery(
        proposal_id="p1",
        effect_id="e1",
        channel=CHANNEL,
        chat_id=CHAT,
        now_ms=1000,
        limits=(("comment", 3, HOUR_MS),),
    )
    assert await log.reserve_judge_attempt(
        "opp:0",
        opportunity_id="opp",
        channel=CHANNEL,
        chat_id=CHAT,
        now_ms=1000,
        hourly_limit=2,
        min_gap_ms=0,
        continuation_candidate=False,
        continuation_reserve=0,
    )
    owner = SourceOwner(store=log)
    assert owner.claim(
        channel=CHANNEL,
        chat_id=CHAT,
        source_event_id="m1",
        activation_epoch=1,
        owner="participation",
    ).granted
    log.close()

    reopened = SpeakupLog(db_path)
    # Durable truth survived: the hold, the attempt charge and the source claim.
    assert await reopened.delivery_state(proposal_id="p1", effect_id="e1") == "reserved"
    assert not await reopened.reserve_judge_attempt(
        "opp:0",
        opportunity_id="opp",
        channel=CHANNEL,
        chat_id=CHAT,
        now_ms=2000,
        hourly_limit=2,
        min_gap_ms=0,
        continuation_candidate=False,
        continuation_reserve=0,
    )
    assert await reopened.consumed_slots(
        channel=CHANNEL, chat_id=CHAT, category="comment", now_ms=2000, window_ms=HOUR_MS
    ) == 1
    restarted_owner = SourceOwner(store=reopened)

    handled: list[str] = []
    release = asyncio.Event()

    async def handle(opportunity) -> None:
        handled.append(opportunity.opportunity_id)
        await release.wait()

    scheduler = OpportunityScheduler(handle=handle, max_concurrent_decisions=1)
    # A fresh scheduler has no queue: the old speculative batch cannot reappear.
    assert scheduler.pending_count == 0
    await scheduler.start()
    try:
        assert (
            restarted_owner.claim(
                channel=CHANNEL,
                chat_id=CHAT,
                source_event_id="m1",
                activation_epoch=2,
                owner="participation",
            ).reason
            == "already_owned"
        )
        await asyncio.sleep(0.05)
        assert handled == []
    finally:
        release.set()
        await scheduler.stop()
    reopened.close()
