"""Plan 03 / R04: turn revisions, authority and invalidation of superseded work."""

from __future__ import annotations

from pathlib import Path

import pytest
from yeoman_gateway.processing.effects import EffectGateway
from yeoman_gateway.processing.models import (
    EffectEnvelope,
    EffectReceipt,
    EffectTarget,
    PolicySnapshot,
    StoredTurn,
    TextPayload,
    TurnStateError,
)
from yeoman_gateway.processing.policy import SnapshotEffectAuthorizer
from yeoman_gateway.processing.store import ProcessingStore
from yeoman_gateway.processing.threads import (
    TurnAuthority,
    ThreadRegistry,
    UpdateEffect,
    classify_update,
)

T0 = 1_700_000_000_000
CHAT = "chat@g.us"


class _Clock:
    def __init__(self, value: int = T0) -> None:
        self.value = value

    def __call__(self) -> int:
        return self.value


class _Snapshots:
    def snapshot(self) -> PolicySnapshot:
        return PolicySnapshot(version="policy-v1", policy_hash="hash-v1", healthy=True)


class _AllowAll:
    def resolve(self, *, principal: str, target: EffectTarget, capability: str):
        return True, "allow"


class _Executor:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def execute(self, envelope: EffectEnvelope) -> EffectReceipt:
        self.calls.append(envelope.effect_id)
        return EffectReceipt(effect_id=envelope.effect_id, state="sent")


class _Config:
    class threads:
        followup_window_seconds = 15
        idle_seconds = 1800
        reopen_window_seconds = 604800
        pending_inputs_per_thread = 32


def _store(tmp_path: Path) -> ProcessingStore:
    return ProcessingStore(tmp_path / "p.db")


def _turn(store: ProcessingStore, registry: ThreadRegistry, *, event_id: str = "m1"):
    from yeoman_gateway.processing.models import CanonicalEvent

    event = CanonicalEvent(
        event_id=event_id,
        event_key=f"wa:{event_id}",
        trace_id="tr1",
        kind="message",
        origin="whatsapp",
        principal="orderer@s.whatsapp.net",
        channel="whatsapp",
        chat_id=CHAT,
        occurred_ms=T0,
        source_message_id=event_id,
        payload={"kind": "message", "text": "hi", "is_group": True, "mentioned_bot": True},
    )
    decision = registry.assign(event, now_ms=T0)
    assert decision.turn_id is not None
    return decision


# --------------------------------------------------------------------------------------
# classification (pure)
# --------------------------------------------------------------------------------------


def test_other_participant_cannot_cancel_job():
    assert classify_update(kind="message", explicit_correction=True,
                           authorized=False) is UpdateEffect.OBSERVE


def test_additional_information_does_not_cancel():
    assert classify_update(kind="message", authorized=True) is UpdateEffect.APPEND


def test_material_change_and_reaction_classification():
    assert classify_update(kind="delete", authorized=True) is UpdateEffect.SUPERSEDE
    assert classify_update(kind="edit", authorized=True) is UpdateEffect.SUPERSEDE
    assert classify_update(kind="reaction", authorized=True) is UpdateEffect.OBSERVE


# --------------------------------------------------------------------------------------
# authority
# --------------------------------------------------------------------------------------


def test_turn_authority_allows_orderer_and_operator_only() -> None:
    turn = StoredTurn(turn_id="tu1", thread_id="th1", principal="orderer@s.whatsapp.net")
    authority = TurnAuthority(
        is_operator=lambda *, principal, channel, chat_id: principal == "owner@s.whatsapp.net"
    )

    assert authority.may_modify(turn, "orderer@s.whatsapp.net") == (True, "orderer")
    assert authority.may_modify(turn, "owner@s.whatsapp.net") == (True, "authorized_operator")
    assert authority.may_modify(turn, "stranger@s.whatsapp.net") == (
        False,
        "foreign_principal_observe",
    )


def test_broken_operator_lookup_never_grants_rights() -> None:
    def _boom(**kwargs):
        raise RuntimeError("policy unavailable")

    turn = StoredTurn(turn_id="tu1", thread_id="th1", principal="orderer@s.whatsapp.net")
    authority = TurnAuthority(is_operator=_boom)

    assert authority.may_modify(turn, "someone@s.whatsapp.net") == (
        False,
        "foreign_principal_observe",
    )


def test_service_principal_is_only_orderer_of_its_own_turn() -> None:
    turn = StoredTurn(turn_id="tu1", thread_id="th1", principal="service:cron")
    authority = TurnAuthority(is_operator=lambda **kwargs: False)

    assert authority.may_modify(turn, "service:cron") == (True, "orderer")
    assert authority.may_modify(turn, "service:speakup") == (False, "foreign_principal_observe")


# --------------------------------------------------------------------------------------
# revision semantics
# --------------------------------------------------------------------------------------


def test_additional_context_bumps_context_not_revision(tmp_path: Path) -> None:
    store = _store(tmp_path)
    registry = ThreadRegistry(store=store, config=_Config())
    decision = _turn(store, registry)
    turn_id = str(decision.turn_id)

    context_version = store.bump_context_version(turn_id, now_ms=T0 + 1_000)

    turn = store.get_turn(turn_id)
    assert context_version == 2
    assert turn is not None and turn.context_version == 2
    assert turn.revision == 1
    store.close()


def test_correction_bumps_revision_monotonically(tmp_path: Path) -> None:
    store = _store(tmp_path)
    registry = ThreadRegistry(store=store, config=_Config())
    turn_id = str(_turn(store, registry).turn_id)

    ref = store.bump_turn_revision(turn_id, expected_revision=1, now_ms=T0 + 1_000, reason="correction")

    assert ref.revision == 2
    assert ref.turn_id == turn_id
    with pytest.raises(TurnStateError):
        store.bump_turn_revision(turn_id, expected_revision=1, now_ms=T0 + 2_000)
    store.close()


def test_closed_turn_cannot_be_revised(tmp_path: Path) -> None:
    store = _store(tmp_path)
    registry = ThreadRegistry(store=store, config=_Config())
    turn_id = str(_turn(store, registry).turn_id)
    store.close_turn(turn_id, now_ms=T0 + 1_000)

    with pytest.raises(TurnStateError):
        store.bump_turn_revision(turn_id, expected_revision=1, now_ms=T0 + 2_000)
    store.close()


def test_deletion_of_the_triggering_message_marks_the_source(tmp_path: Path) -> None:
    store = _store(tmp_path)
    registry = ThreadRegistry(store=store, config=_Config())
    turn_id = str(_turn(store, registry).turn_id)

    store.mark_turn_source_removed(turn_id=turn_id, event_id="m1", now_ms=T0 + 5_000)
    store.bump_turn_revision(turn_id, expected_revision=1, now_ms=T0 + 5_000, reason="delete")

    sources = store.turn_sources(turn_id)
    assert [source.event_id for source in sources] == ["m1"]
    assert sources[0].removed_ms == T0 + 5_000  # the old snapshot stays provable
    assert store.get_turn(turn_id).revision == 2
    store.close()


# --------------------------------------------------------------------------------------
# invalidation of queued work
# --------------------------------------------------------------------------------------


def _gateway(store: ProcessingStore, executor: _Executor, registry: ThreadRegistry):
    return EffectGateway(
        store,
        authorizer=SnapshotEffectAuthorizer(
            snapshots=_Snapshots(),
            capabilities=_AllowAll(),
            turn_lookup=registry.turn_lookup,
            clock=_Clock(),
        ),
        executor=executor,
        clock=_Clock(),
    )


def _envelope(turn_id: str, revision: int, *, key: str = "k1") -> EffectEnvelope:
    return EffectEnvelope(
        effect_id="fx1",
        operation_key=key,
        payload=TextPayload(text="hi"),
        target=EffectTarget(channel="whatsapp", chat_id=CHAT),
        turn_id=turn_id,
        turn_revision=revision,
        principal="orderer@s.whatsapp.net",
        capability="send_text",
        expires_at_ms=T0 + 120_000,
    )


@pytest.mark.asyncio
async def test_cancelled_revision_never_reaches_the_transport(tmp_path: Path) -> None:
    store = _store(tmp_path)
    registry = ThreadRegistry(store=store, config=_Config())
    turn_id = str(_turn(store, registry).turn_id)
    executor = _Executor()
    gateway = _gateway(store, executor, registry)

    gateway.submit(_envelope(turn_id, 1))
    store.bump_turn_revision(turn_id, expected_revision=1, now_ms=T0 + 1_000, reason="correction")
    cancelled = store.cancel_stale_effects(turn_id, current_revision=2, now_ms=T0 + 1_000, reason="correction")

    assert cancelled == ("fx1",)
    result = await gateway.execute_ready("fx1")
    assert result.state == "cancelled"
    assert executor.calls == []
    store.close()


@pytest.mark.asyncio
async def test_unknown_turn_is_refused_and_never_sent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    registry = ThreadRegistry(store=store, config=_Config())
    executor = _Executor()
    gateway = _gateway(store, executor, registry)

    gateway.submit(_envelope("tu-does-not-exist", 1))
    result = await gateway.execute_ready("fx1")

    assert result.state == "cancelled"
    assert "superseded" in (result.detail or "")
    assert executor.calls == []
    store.close()


@pytest.mark.asyncio
async def test_late_revision_never_downgrades_started_or_sent_work(tmp_path: Path) -> None:
    store = _store(tmp_path)
    registry = ThreadRegistry(store=store, config=_Config())
    turn_id = str(_turn(store, registry).turn_id)
    executor = _Executor()
    gateway = _gateway(store, executor, registry)

    gateway.submit(_envelope(turn_id, 1))
    assert (await gateway.execute_ready("fx1")).state == "sent"

    store.bump_turn_revision(turn_id, expected_revision=1, now_ms=T0 + 1_000, reason="correction")
    cancelled = store.cancel_stale_effects(turn_id, current_revision=2, now_ms=T0 + 1_000)

    assert cancelled == ()
    assert store.effect_state("fx1") == "sent"
    assert executor.calls == ["fx1"]
    store.close()


@pytest.mark.asyncio
async def test_effect_of_a_reopened_thread_cannot_send(tmp_path: Path) -> None:
    store = _store(tmp_path)
    registry = ThreadRegistry(store=store, config=_Config())
    decision = _turn(store, registry)
    turn_id = str(decision.turn_id)
    executor = _Executor()
    gateway = _gateway(store, executor, registry)

    gateway.submit(_envelope(turn_id, 1))
    store.close_turn(turn_id, now_ms=T0 + 1_000)

    result = await gateway.execute_ready("fx1")

    assert result.state == "cancelled"
    assert executor.calls == []
    store.close()


def test_operation_key_carries_turn_identity(tmp_path: Path) -> None:
    store = _store(tmp_path)
    registry = ThreadRegistry(store=store, config=_Config())
    turn_id = str(_turn(store, registry).turn_id)

    store.enqueue_effect(
        effect_id="fx1",
        operation_key=f"send_text:{CHAT}:{turn_id}:1:src",
        payload={"text": "hi"},
        target={"channel": "whatsapp", "chat_id": CHAT},
        turn_id=turn_id,
        turn_revision=1,
        now_ms=T0,
    )
    # Same logical action, new revision: a different operation key is required.
    with pytest.raises(ValueError):
        store.enqueue_effect(
            effect_id="fx2",
            operation_key=f"send_text:{CHAT}:{turn_id}:1:src",
            payload={"text": "hi"},
            target={"channel": "whatsapp", "chat_id": CHAT},
            turn_id=turn_id,
            turn_revision=2,
            now_ms=T0,
        )
    store.enqueue_effect(
        effect_id="fx3",
        operation_key=f"send_text:{CHAT}:{turn_id}:2:src",
        payload={"text": "hi"},
        target={"channel": "whatsapp", "chat_id": CHAT},
        turn_id=turn_id,
        turn_revision=2,
        now_ms=T0,
    )
    assert store.count_effects() == 2
    store.close()
