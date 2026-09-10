"""Plan 06 / Aufgabe 2: end-to-end matrix over the real pipeline.

Only the provider and network boundaries are faked (transport, policy file, clock); the
gate, store, registry, effect router and dispatcher are the production objects.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from yeoman_gateway.adapters.policy_engine import EnginePolicyAdapter
from yeoman_gateway.app.bootstrap import (
    build_effect_router,
    build_processing_gate,
    build_processing_store,
    build_thread_registry,
)
from yeoman_gateway.bus.events import OutboundMessage
from yeoman_gateway.bus.queue import MessageBus
from yeoman_gateway.core.models import InboundEvent
from yeoman_gateway.policy.engine import PolicyEngine
from yeoman_gateway.policy.loader import save_policy
from yeoman_gateway.policy.schema import PolicyConfig
from yeoman_gateway.processing.dispatch import EffectNotDeliveredError, ManagedOutboundDispatcher
from yeoman_gateway.processing.policy import IngestRequest
from yeoman_shared.config.schema import Config

CHAT = "pilot@g.us"
OTHER = "second@g.us"
T0 = 1_700_000_000_000


class _Clock:
    def __init__(self, value: int = T0) -> None:
        self.value = value

    def __call__(self) -> int:
        return self.value


@dataclass
class _Transport:
    """Stands in for ChannelManager.send_now()."""

    sent: list[str] = field(default_factory=list)
    fail: bool = False

    async def send_now(self, message) -> None:
        if self.fail:
            raise TimeoutError("bridge timeout")
        self.sent.append(message.content)

    async def send_reaction_now(self, message) -> None:
        self.sent.append(message.emoji)


def _config(*, chats: tuple[str, ...] = (CHAT,), waiting_cap: int = 20) -> Config:
    return Config.model_validate(
        {
            "processing": {
                "enabled": True,
                "chats": [f"whatsapp:{chat}" for chat in chats],
                "budgets": {"outbox_waiting_per_chat": waiting_cap},
            },
            "security": {"enabled": False},
        }
    )


def _policy(chats: tuple[str, ...] = (CHAT,), *, mode: str = "everyone") -> PolicyConfig:
    default = {"whoCanTalk": {"mode": mode}, "whenToReply": {"mode": "all"}}
    return PolicyConfig.model_validate(
        {
            "owners": {"whatsapp": ["owner@s.whatsapp.net"]},
            "runtime": {"reloadOnChange": True},
            "channels": {"whatsapp": {"default": default}},
        }
    )


@dataclass
class _Runtime:
    store: object
    registry: object
    gate: object
    router: object
    transport: _Transport
    adapter: EnginePolicyAdapter
    policy_path: Path
    workspace: Path
    config: Config

    def reload_policy(self, policy: PolicyConfig) -> None:
        """Write and pick up a new policy, bypassing the production check interval."""
        save_policy(policy, self.policy_path)
        self.adapter._last_reload_check = 0.0  # test-only: skip the throttle window
        self.adapter._maybe_reload()


@pytest.fixture()
def runtime(tmp_path: Path):
    _rt = _make_runtime(tmp_path)
    yield _rt
    _rt.store.close()


def _make_runtime(
    tmp_path: Path, *, chats: tuple[str, ...] = (CHAT,), waiting_cap: int = 20, **kwargs
) -> _Runtime:
    policy_path = tmp_path / "policy.json"
    policy = _policy(chats, **kwargs)
    save_policy(policy, policy_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    adapter = EnginePolicyAdapter(
        engine=PolicyEngine(policy, workspace=workspace, apply_channels={"whatsapp"}),
        known_tools={"message"},
        policy_path=policy_path,
        workspace=workspace,
    )
    config = _config(chats=chats, waiting_cap=waiting_cap)
    # Pin the database: reopening must never fall back to the default (live) path.
    config.processing.db_path = str(tmp_path / "processing.db")
    with patch.dict(os.environ, {"YEOMAN_HOME": str(tmp_path)}):
        store = build_processing_store(config)
        assert store is not None
        registry = build_thread_registry(config, store)
        gate = build_processing_gate(config, adapter, store, registry)
        router = build_effect_router(config, adapter, store, MessageBus(), threads=registry)
    assert registry is not None and gate is not None and router is not None
    transport = _Transport()
    router.set_direct_transport(transport.send_now, transport.send_reaction_now)
    return _Runtime(
        store=store,
        registry=registry,
        gate=gate,
        router=router,
        transport=transport,
        adapter=adapter,
        policy_path=policy_path,
        workspace=workspace,
        config=config,
    )


def _event(*, message_id: str, content: str = "hi", chat: str = CHAT) -> InboundEvent:
    return InboundEvent(
        channel="whatsapp",
        chat_id=chat,
        sender_id="orderer@s.whatsapp.net",
        content=content,
        message_id=message_id,
        is_group=True,
        mentioned_bot=True,
        timestamp=datetime(2023, 11, 14, tzinfo=UTC),
    )


def _admit(runtime: _Runtime, *, message_id: str, chat: str = CHAT, content: str = "hi"):
    return runtime.gate.admit(
        IngestRequest(
            event_key=f"whatsapp:{chat}:{message_id}",
            event_id=message_id,
            trace_id=f"tr-{message_id}",
            event=_event(message_id=message_id, chat=chat, content=content),
        )
    )


async def _dispatch(
    runtime: _Runtime,
    *,
    chat: str = CHAT,
    content: str = "answer",
    message_id: str = "m1",
    principal: str | None = None,
):
    """Dispatch one managed reply. ``principal`` comes from the runtime, never metadata."""
    dispatcher = ManagedOutboundDispatcher(
        router=runtime.router,
        bus=MessageBus(),
        principal=(lambda: principal) if principal else None,
    )
    await dispatcher(
        OutboundMessage(
            channel="whatsapp", chat_id=chat, content=content, metadata={"message_id": message_id}
        )
    )


@pytest.mark.asyncio
async def test_a_follow_up_joins_the_thread_and_opens_a_second_turn(runtime) -> None:
    """Two messages in one chat share the thread but never share a turn."""
    first = _admit(runtime, message_id="m1")
    second = _admit(runtime, message_id="m2")

    assert first.assignment is not None and second.assignment is not None
    assert first.assignment.thread_id == second.assignment.thread_id
    # The follow-up joins the open turn as a second source (bundling, not a new turn).
    turn_id = str(first.assignment.turn_id)
    sources = [ref.event_id for ref in runtime.store.turn_sources(turn_id)]
    assert sources == ["m1", "m2"]
    await _dispatch(runtime, message_id="m1")
    await _dispatch(runtime, message_id="m2")

    effects = runtime.store.list_effects()
    assert len(effects) == 2
    assert {effect.state for effect in effects} == {"sent"}
    effects_by_turn = {effect.turn_id for effect in effects}
    assert len(effects_by_turn) == 1  # both answers belong to the bundled turn
    assert runtime.transport.sent == ["answer", "answer"]


@pytest.mark.asyncio
async def test_two_chats_keep_their_own_budget_and_effects(tmp_path: Path) -> None:
    runtime = _make_runtime(tmp_path, chats=(CHAT, OTHER))
    try:
        _admit(runtime, message_id="m1", chat=CHAT)
        _admit(runtime, message_id="m2", chat=OTHER)
        await _dispatch(runtime, chat=CHAT, message_id="m1")
        await _dispatch(runtime, chat=OTHER, message_id="m2")

        effects = runtime.store.list_effects()
        assert len(effects) == 2
        assert len({effect.target_hash for effect in effects}) == 2
        assert runtime.store.count_send_budget_reservations() == 2
    finally:
        runtime.store.close()


@pytest.mark.asyncio
async def test_duplicate_admission_does_not_send_twice(runtime) -> None:
    first = _admit(runtime, message_id="m1")
    duplicate = _admit(runtime, message_id="m1")

    assert first.assignment is not None
    # A replay lands in the same thread and turn: no second assignment is invented.
    assert duplicate.assignment is not None
    assert duplicate.assignment.thread_id == first.assignment.thread_id
    assert duplicate.assignment.turn_id == first.assignment.turn_id
    await _dispatch(runtime, message_id="m1")
    await _dispatch(runtime, message_id="m1")

    effects = runtime.store.list_effects()
    assert len(effects) == 1
    assert runtime.transport.sent == ["answer"]


@pytest.mark.asyncio
async def test_correction_during_the_provider_await_cancels_the_stale_effect(runtime) -> None:
    result = _admit(runtime, message_id="m1")
    turn_id = str(result.assignment.turn_id)
    turn = runtime.store.get_turn(turn_id)
    runtime.store.enqueue_effect(
        effect_id="fx-stale",
        operation_key=f"send_text:{CHAT}:{turn_id}:1",
        payload={"text": "old"},
        target={"channel": "whatsapp", "chat_id": CHAT},
        turn_id=turn_id,
        turn_revision=1,
        now_ms=T0,
    )

    runtime.store.bump_turn_revision(
        turn_id, expected_revision=turn.revision, now_ms=T0 + 1_000, reason="correction"
    )
    cancelled = runtime.store.cancel_stale_effects(
        turn_id, current_revision=2, now_ms=T0 + 1_000, reason="correction"
    )

    assert cancelled == ("fx-stale",)
    assert runtime.store.effect_state("fx-stale") == "cancelled"
    assert runtime.transport.sent == []
    assert runtime.registry.turn_lookup(turn_id).revision == 2


@pytest.mark.asyncio
async def test_policy_change_before_the_effect_blocks_the_send(runtime) -> None:
    _admit(runtime, message_id="m1")
    runtime.reload_policy(_policy(mode="owner_only"))

    # The reply is produced for the sender, so the new policy decides its egress.
    with pytest.raises(EffectNotDeliveredError) as denied:
        await _dispatch(runtime, message_id="m1", principal="orderer@s.whatsapp.net")

    assert "blocked" in str(denied.value)
    effects = runtime.store.list_effects()
    assert len(effects) == 1
    assert effects[0].state == "blocked"
    assert runtime.transport.sent == []


@pytest.mark.asyncio
async def test_queue_overflow_blocks_with_a_reason(tmp_path: Path) -> None:
    runtime = _make_runtime(tmp_path, waiting_cap=0)
    try:
        result = _admit(runtime, message_id="m1")
        turn_id = str(result.assignment.turn_id)
        runtime.store.enqueue_effect(
            effect_id="fx-waiting",
            operation_key="waiting:1",
            payload={"text": "queued"},
            target={"channel": "whatsapp", "chat_id": CHAT},
            turn_id=turn_id,
            turn_revision=1,
            now_ms=T0,
        )

        with pytest.raises(EffectNotDeliveredError) as blocked:
            await _dispatch(runtime, message_id="m1")
        assert "blocked" in str(blocked.value)
        assert "queue_capacity" in str(blocked.value)

        states = sorted(effect.state for effect in runtime.store.list_effects())
        assert states == ["blocked", "queued"]
        assert runtime.transport.sent == []
    finally:
        runtime.store.close()


@pytest.mark.asyncio
async def test_unknown_send_status_is_resolved_by_a_late_receipt(runtime) -> None:
    from yeoman_gateway.processing.reconcile import (
        LocalEvidenceProbe,
        ProbeOutcome,
        reconcile_effect,
    )

    _admit(runtime, message_id="m1")
    runtime.transport.fail = True
    with pytest.raises(EffectNotDeliveredError) as unproven:
        await _dispatch(runtime, message_id="m1")
    assert "unknown" in str(unproven.value)

    effect = runtime.store.list_effects()[0]
    assert effect.state == "unknown"

    runtime.store.record_transport_receipt(
        effect.effect_id,
        channel="whatsapp",
        chat_id=CHAT,
        provider_message_id="3EB0",
        now_ms=T0 + 5_000,
    )
    result = await reconcile_effect(
        runtime.store, effect.effect_id, probe=LocalEvidenceProbe(runtime.store), now_ms=T0 + 6_000
    )

    assert result.outcome is ProbeOutcome.CONFIRMED
    assert runtime.store.effect_state(effect.effect_id) == "sent"


@pytest.mark.asyncio
async def test_restart_never_resends_a_proven_send(tmp_path: Path) -> None:
    """Crash matrix: what was proven sent before the restart is not sent a second time."""
    runtime = _make_runtime(tmp_path)
    _admit(runtime, message_id="m1")
    await _dispatch(runtime, message_id="m1")
    effect = runtime.store.list_effects()[0]
    assert effect.state == "sent"
    runtime.store.close()

    reopened = build_processing_store(runtime.config)
    assert reopened is not None
    try:
        assert reopened.recover_executing(now_ms=T0 + 10_000) == ()
        assert reopened.effect_state(effect.effect_id) == "sent"
        assert len(reopened.list_effects()) == 1
    finally:
        reopened.close()


def test_every_effect_has_a_parent_turn(runtime) -> None:
    """No orphan effects: each one links to the turn and revision that produced it."""
    _admit(runtime, message_id="m1")

    effects = runtime.store.list_effects()
    for effect in effects:
        assert effect.turn_id, "effect without a parent turn"
        assert effect.turn_revision >= 1
        assert runtime.store.get_turn(effect.turn_id) is not None
