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
    ThreadRegistry,
    TurnAuthority,
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
    class Threads:
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
    # The fast gate journals first and assigns afterwards; mirror that order.
    store.append_event(
        event_key=event.event_key,
        event_id=event.event_id,
        trace_id=event.trace_id,
        payload=dict(event.payload or {}),
        now_ms=T0,
    )
    decision = registry.assign(event, now_ms=T0)
    assert decision.turn_id is not None
    assert store.event_assignment(event.event_id) == (decision.thread_id, decision.turn_id)
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


# --------------------------------------------------------------------------------------
# Task 3: actor, bounded postbox, immutable generations
# --------------------------------------------------------------------------------------


def _actor(store: ProcessingStore, registry: ThreadRegistry, thread_id: str):
    from yeoman_gateway.processing.actor import ThreadActor

    return ThreadActor(store=store, thread_id=thread_id, cap=32, clock=_Clock())


def _followup(store: ProcessingStore, thread_id: str, *, event_id: str, kind: str = "message"):
    return store.enqueue_pending_input(
        input_id=f"in-{event_id}",
        thread_id=thread_id,
        event_id=event_id,
        principal="orderer@s.whatsapp.net",
        now_ms=T0 + 1,
        kind=kind,
        cap=32,
    )


@pytest.mark.asyncio
async def test_followup_is_accepted_while_the_provider_call_is_in_flight(tmp_path: Path) -> None:
    """Barrier test: no sleeps, the provider waits on an event we control."""
    import asyncio

    store = _store(tmp_path)
    registry = ThreadRegistry(store=store, config=_Config())
    decision = _turn(store, registry)
    actor = _actor(store, registry, str(decision.thread_id))
    snapshot = actor.freeze_snapshot()
    assert snapshot is not None

    started = asyncio.Event()
    release = asyncio.Event()

    async def _provider(_snapshot):
        started.set()
        await release.wait()
        return "first answer"

    task = asyncio.create_task(actor.run_generation(snapshot, _provider))
    await started.wait()

    admission = actor.accept(event_id="m2", principal="orderer@s.whatsapp.net", authorized=True)

    assert admission.accepted is True
    assert store.count_pending(thread_id=str(decision.thread_id), states=("waiting",)) == 1
    assert actor.state_lock_busy is False

    release.set()
    outcome = await task

    assert outcome.state == "restart"  # new context, so the stale text is not sent
    assert outcome.text is None or outcome.text == "first answer"
    store.close()


def test_postbox_is_bounded_and_deferred_inputs_survive(tmp_path: Path) -> None:
    store = _store(tmp_path)
    registry = ThreadRegistry(store=store, config=_Config())
    decision = _turn(store, registry)
    thread_id = str(decision.thread_id)

    for index in range(32):
        assert _followup(store, thread_id, event_id=f"f{index}") == "waiting"
    assert _followup(store, thread_id, event_id="overflow") == "deferred"

    assert store.count_pending(thread_id=thread_id, states=("waiting",)) == 32
    assert store.count_pending(thread_id=thread_id, states=("deferred",)) == 1

    # Durable across a restart.
    store.close()
    store = ProcessingStore(tmp_path / "p.db")
    assert store.count_pending(thread_id=thread_id, states=("deferred",)) == 1

    drained = store.drain_pending_inputs(thread_id, now_ms=T0 + 2)
    assert len(drained) == 32
    promoted = store.promote_deferred(thread_id, now_ms=T0 + 3, capacity=32)
    assert promoted == ("overflow",)
    store.close()


def test_snapshot_is_immutable_and_carries_no_prompt_text(tmp_path: Path) -> None:
    store = _store(tmp_path)
    registry = ThreadRegistry(store=store, config=_Config())
    decision = _turn(store, registry)
    actor = _actor(store, registry, str(decision.thread_id))

    first = actor.freeze_snapshot()
    assert first is not None
    assert first.revision == 1 and first.context_version == 1
    assert first.source_refs and first.source_refs[0].event_id == "m1"
    assert "hi" not in first.snapshot_hash  # hashes only, no prompt text

    store.bump_context_version(first.turn_id, now_ms=T0 + 1)
    second = actor.freeze_snapshot()
    assert second is not None
    assert second.generation_id != first.generation_id
    assert second.context_version == 2
    assert first.context_version == 1  # frozen: the running request cannot change
    assert first.snapshot_hash != second.snapshot_hash

    generations = store.generations_for_turn(first.turn_id)
    assert len(generations) == 2
    store.close()


@pytest.mark.asyncio
async def test_at_most_two_additional_generations_then_followup_turn(tmp_path: Path) -> None:
    store = _store(tmp_path)
    registry = ThreadRegistry(store=store, config=_Config())
    decision = _turn(store, registry)
    thread_id = str(decision.thread_id)
    actor = _actor(store, registry, thread_id)

    async def _provider(_snapshot):
        return "answer"

    for expected in (1, 2):
        snapshot = actor.freeze_snapshot()
        actor.accept(event_id=f"extra{expected}", principal="orderer@s.whatsapp.net", authorized=True)
        outcome = await actor.run_generation(snapshot, _provider)
        assert outcome.state == "restart"
        assert actor.additional_generations == expected

    snapshot = actor.freeze_snapshot()
    actor.accept(event_id="extra3", principal="orderer@s.whatsapp.net", authorized=True)
    outcome = await actor.run_generation(snapshot, _provider)

    assert outcome.state == "send"
    assert outcome.followup_turn_id is not None
    assert outcome.followup_turn_id != decision.turn_id
    assert store.get_turn(outcome.followup_turn_id) is not None
    store.close()


@pytest.mark.asyncio
async def test_correction_during_a_generation_supersedes_and_cancels(tmp_path: Path) -> None:
    store = _store(tmp_path)
    registry = ThreadRegistry(store=store, config=_Config())
    decision = _turn(store, registry)
    turn_id = str(decision.turn_id)
    actor = _actor(store, registry, str(decision.thread_id))
    snapshot = actor.freeze_snapshot()

    store.enqueue_effect(
        effect_id="fx-stale",
        operation_key=f"send_text:{CHAT}:{turn_id}:1:src",
        payload={"text": "old"},
        target={"channel": "whatsapp", "chat_id": CHAT},
        turn_id=turn_id,
        turn_revision=1,
        now_ms=T0,
    )

    async def _provider(_snapshot):
        return "stale answer"

    actor.accept(
        event_id="del1", kind="delete", principal="orderer@s.whatsapp.net", authorized=True
    )
    outcome = await actor.run_generation(snapshot, _provider)

    assert outcome.state == "superseded"
    assert outcome.revision == 2
    assert store.effect_state("fx-stale") == "cancelled"
    store.close()


@pytest.mark.asyncio
async def test_provider_error_sends_nothing_and_keeps_the_turn_open(tmp_path: Path) -> None:
    store = _store(tmp_path)
    registry = ThreadRegistry(store=store, config=_Config())
    decision = _turn(store, registry)
    actor = _actor(store, registry, str(decision.thread_id))
    snapshot = actor.freeze_snapshot()

    async def _provider(_snapshot):
        raise RuntimeError("provider down")

    outcome = await actor.run_generation(snapshot, _provider)

    assert outcome.state == "error"
    assert outcome.text is None
    assert store.count_effects() == 0
    assert store.get_turn(str(decision.turn_id)).state == "open"
    assert store.generations_for_turn(str(decision.turn_id))[0]["outcome"] == "error"
    store.close()


def test_actor_registry_finds_the_active_turn_and_closes_idle_threads(tmp_path: Path) -> None:
    from yeoman_gateway.processing.actor import ThreadActorRegistry

    store = _store(tmp_path)
    registry = ThreadRegistry(store=store, config=_Config())
    decision = _turn(store, registry)
    actors = ThreadActorRegistry(store=store, config=_Config(), clock=_Clock())

    turn_ref = actors.active_turn("whatsapp", CHAT)
    assert turn_ref is not None and turn_ref.turn_id == decision.turn_id

    closed = actors.tick(T0 + 31 * 60_000)
    assert closed == (decision.thread_id,)
    assert actors.active_turn("whatsapp", CHAT) is None
    store.close()


# --------------------------------------------------------------------------------------
# Task 3 wiring: the responder wrapper drives the generation loop
# --------------------------------------------------------------------------------------


def _assign_event(store: ProcessingStore, *, event_id: str, thread_id: str, turn_id: str) -> None:
    """The fast gate journals and assigns every inbound event before the responder runs."""
    store.append_event(
        event_key=f"wa:{event_id}",
        event_id=event_id,
        trace_id=f"tr-{event_id}",
        payload={"kind": "message", "text": "hi", "is_group": True, "mentioned_bot": True},
        now_ms=T0,
    )
    store.attach_event_assignment(
        event_id=event_id, thread_id=thread_id, turn_id=turn_id, now_ms=T0
    )


class _InnerResponder:
    """Records calls; the first call can signal that it started and then block."""

    def __init__(self, texts: list[str] | None = None, barrier=None, started=None) -> None:
        self.texts = list(texts or ["answer"])
        self.calls = 0
        self.snapshots: list[object] = []
        self._barrier = barrier
        self._started = started

    async def generate_reply(self, event, decision) -> str | None:
        self.calls += 1
        if self.calls == 1:
            if self._started is not None:
                self._started.set()
            if self._barrier is not None:
                await self._barrier.wait()
        return self.texts[min(self.calls - 1, len(self.texts) - 1)]


def _event_model(*, message_id: str = "m1", sender: str = "orderer@s.whatsapp.net"):
    from datetime import UTC, datetime

    from yeoman_gateway.core.models import InboundEvent

    return InboundEvent(
        channel="whatsapp",
        chat_id=CHAT,
        sender_id=sender,
        content="hi",
        message_id=message_id,
        is_group=True,
        mentioned_bot=True,
        timestamp=datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC),
    )


def _wrapper(store: ProcessingStore, inner, registry: ThreadRegistry):
    from yeoman_gateway.processing.actor import ThreadActorRegistry
    from yeoman_gateway.processing.responder import ThreadActorResponder

    actors = ThreadActorRegistry(store=store, config=_Config(), clock=_Clock())
    return ThreadActorResponder(inner=inner, actors=actors, store=store, clock=_Clock())


@pytest.mark.asyncio
async def test_unmanaged_event_passes_through_unchanged(tmp_path: Path) -> None:
    store = _store(tmp_path)
    inner = _InnerResponder(["legacy answer"])
    wrapper = _wrapper(store, inner, ThreadRegistry(store=store, config=_Config()))

    reply = await wrapper.generate_reply(_event_model(message_id="unknown"), object())

    assert reply == "legacy answer"
    assert inner.calls == 1
    store.close()


@pytest.mark.asyncio
async def test_followup_during_generation_produces_no_second_answer(tmp_path: Path) -> None:
    import asyncio

    store = _store(tmp_path)
    registry = ThreadRegistry(store=store, config=_Config())
    decision = _turn(store, registry)
    barrier = asyncio.Event()
    started = asyncio.Event()
    inner = _InnerResponder(["combined answer"], barrier=barrier, started=started)
    wrapper = _wrapper(store, inner, registry)

    _assign_event(
        store, event_id="m2", thread_id=str(decision.thread_id), turn_id=str(decision.turn_id)
    )
    task = asyncio.create_task(wrapper.generate_reply(_event_model(message_id="m1"), object()))
    await started.wait()
    followup = await wrapper.generate_reply(_event_model(message_id="m2"), object())
    assert followup is None  # accepted into the postbox, no second answer path
    barrier.set()
    reply = await task

    # The running generation restarted once with the wider snapshot instead of the
    # follow-up opening a second answer path.
    assert inner.calls == 2
    assert reply == "combined answer"
    assert store.count_pending(thread_id=str(decision.thread_id), states=("waiting",)) == 0
    store.close()


@pytest.mark.asyncio
async def test_new_context_restarts_the_generation_once(tmp_path: Path) -> None:
    import asyncio

    store = _store(tmp_path)
    registry = ThreadRegistry(store=store, config=_Config())
    decision = _turn(store, registry)
    barrier = asyncio.Event()
    started = asyncio.Event()
    inner = _InnerResponder(["stale answer", "fresh answer"], barrier=barrier, started=started)
    wrapper = _wrapper(store, inner, registry)

    _assign_event(
        store, event_id="m2", thread_id=str(decision.thread_id), turn_id=str(decision.turn_id)
    )
    task = asyncio.create_task(wrapper.generate_reply(_event_model(message_id="m1"), object()))
    await started.wait()
    await wrapper.generate_reply(_event_model(message_id="m2"), object())
    barrier.set()
    reply = await task

    assert inner.calls == 2  # one restart with the wider snapshot
    assert reply == "fresh answer"
    store.close()


@pytest.mark.asyncio
async def test_superseded_generation_returns_no_reply(tmp_path: Path) -> None:
    import asyncio

    store = _store(tmp_path)
    registry = ThreadRegistry(store=store, config=_Config())
    decision = _turn(store, registry)
    store.enqueue_effect(
        effect_id="fx1",
        operation_key=f"send_text:{CHAT}:{decision.turn_id}:1:src",
        payload={"text": "old"},
        target={"channel": "whatsapp", "chat_id": CHAT},
        turn_id=str(decision.turn_id),
        turn_revision=1,
        now_ms=T0,
    )
    barrier = asyncio.Event()
    started = asyncio.Event()
    inner = _InnerResponder(["stale answer"], barrier=barrier, started=started)
    wrapper = _wrapper(store, inner, registry)
    _assign_event(
        store, event_id="m2", thread_id=str(decision.thread_id), turn_id=str(decision.turn_id)
    )
    task = asyncio.create_task(wrapper.generate_reply(_event_model(message_id="m1"), object()))
    await started.wait()
    correction = _event_model(message_id="m2")
    correction.raw_metadata["processing_kind"] = "delete"
    correction.raw_metadata["explicit_correction"] = True
    await wrapper.generate_reply(correction, object())
    barrier.set()
    reply = await task

    assert reply is None
    assert store.effect_state("fx1") == "cancelled"
    store.close()
