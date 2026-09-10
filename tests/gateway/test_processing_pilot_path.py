"""Plan 03 integration: the deployed path, from the fast gate to the dispatched effect.

The unit tests cover each piece; this test drives the *real* factories the gateway builds
(`build_processing_store`, `build_thread_registry`, `build_processing_gate`,
`build_effect_router`) so a wiring mistake between them fails here instead of in the pilot.
"""

from __future__ import annotations

import os
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
from yeoman_gateway.bus.queue import MessageBus
from yeoman_gateway.core.models import InboundEvent
from yeoman_gateway.policy.engine import PolicyEngine
from yeoman_gateway.policy.loader import save_policy
from yeoman_gateway.policy.schema import PolicyConfig
from yeoman_gateway.processing.dispatch import ManagedOutboundDispatcher
from yeoman_gateway.processing.policy import IngestRequest
from yeoman_shared.config.schema import Config

CHAT = "pilot@g.us"


class _Clock:
    def __init__(self, value: int = 1_700_000_000_000) -> None:
        self.value = value

    def __call__(self) -> int:
        return self.value


class _Transport:
    """Stands in for ChannelManager.send_now()."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_now(self, message) -> None:
        self.sent.append(message.content)

    async def send_reaction_now(self, message) -> None:  # pragma: no cover - unused here
        self.sent.append(message.emoji)


def _config() -> Config:
    return Config.model_validate(
        {
            "processing": {"enabled": True, "chats": [f"whatsapp:{CHAT}"]},
            "security": {"enabled": False},
        }
    )


@pytest.fixture()
def runtime(tmp_path: Path):
    policy_path = tmp_path / "policy.json"
    policy = PolicyConfig.model_validate(
        {
            "owners": {"whatsapp": ["owner@s.whatsapp.net"]},
            "channels": {
                "whatsapp": {
                    "default": {"whoCanTalk": {"mode": "everyone"}, "whenToReply": {"mode": "all"}}
                }
            },
        }
    )
    save_policy(policy, policy_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    adapter = EnginePolicyAdapter(
        engine=PolicyEngine(policy, workspace=workspace, apply_channels={"whatsapp"}),
        known_tools={"message"},
        policy_path=policy_path,
        workspace=workspace,
    )
    config = _config()
    with patch.dict(os.environ, {"YEOMAN_HOME": str(tmp_path)}):
        store = build_processing_store(config)
        assert store is not None
        registry = build_thread_registry(config, store)
        gate = build_processing_gate(config, adapter, store, registry)
        router = build_effect_router(config, adapter, store, MessageBus(), threads=registry)
    assert registry is not None and gate is not None and router is not None
    transport = _Transport()
    router.set_direct_transport(transport.send_now, transport.send_reaction_now)
    yield store, registry, gate, router, transport, adapter
    store.close()


def _event(*, message_id: str, content: str = "hi") -> InboundEvent:
    return InboundEvent(
        channel="whatsapp",
        chat_id=CHAT,
        sender_id="orderer@s.whatsapp.net",
        content=content,
        message_id=message_id,
        is_group=True,
        mentioned_bot=True,
        timestamp=datetime(2023, 11, 14, tzinfo=UTC),
    )


def test_gate_assigns_a_thread_and_turn_then_the_effect_carries_it(runtime) -> None:
    store, registry, gate, router, transport, _adapter = runtime
    request = IngestRequest(
        event_key=f"whatsapp:{CHAT}:m1", event_id="m1", trace_id="tr-m1", event=_event(message_id="m1")
    )

    result = gate.admit(request)

    assert result is not None and result.proceed is True
    assert result.assignment is not None
    thread_id, turn_id = result.assignment.thread_id, result.assignment.turn_id
    assert thread_id and turn_id
    assert store.get_thread(thread_id) is not None
    turn = store.get_turn(turn_id)
    assert turn is not None and turn.revision == 1
    # The journal carries the assignment the responder wrapper resolves later.
    assert store.event_assignment("m1") == (thread_id, turn_id)
    assert registry.turn_lookup(turn_id) is not None


@pytest.mark.asyncio
async def test_managed_reply_dispatches_with_a_real_turn(runtime) -> None:
    store, registry, gate, router, transport, _adapter = runtime
    gate.admit(
        IngestRequest(
            event_key=f"whatsapp:{CHAT}:m1",
            event_id="m1",
            trace_id="tr-m1",
            event=_event(message_id="m1"),
        )
    )
    dispatcher = ManagedOutboundDispatcher(router=router, bus=MessageBus())
    from yeoman_gateway.bus.events import OutboundMessage

    await dispatcher(
        OutboundMessage(
            channel="whatsapp", chat_id=CHAT, content="answer", metadata={"message_id": "m1"}
        )
    )

    effects = store.list_effects()
    assert len(effects) == 1
    assert effects[0].state == "sent"
    assert effects[0].turn_id and effects[0].turn_revision == 1  # real turn identity
    assert transport.sent == ["answer"]


@pytest.mark.asyncio
async def test_correction_cancels_the_queued_effect_before_dispatch(runtime) -> None:
    store, registry, gate, router, transport, _adapter = runtime
    result = gate.admit(
        IngestRequest(
            event_key=f"whatsapp:{CHAT}:m1",
            event_id="m1",
            trace_id="tr-m1",
            event=_event(message_id="m1"),
        )
    )
    turn_id = str(result.assignment.turn_id)
    turn = store.get_turn(turn_id)

    # A queued effect of revision 1 is invalidated by an authorised correction.
    store.enqueue_effect(
        effect_id="fx-stale",
        operation_key=f"send_text:{CHAT}:{turn_id}:1:m1",
        payload={"text": "old"},
        target={"channel": "whatsapp", "chat_id": CHAT},
        turn_id=turn_id,
        turn_revision=1,
        now_ms=1_700_000_000_000,
    )
    store.bump_turn_revision(
        turn_id, expected_revision=turn.revision, now_ms=1_700_000_001_000, reason="correction"
    )
    cancelled = store.cancel_stale_effects(
        turn_id, current_revision=2, now_ms=1_700_000_001_000, reason="correction"
    )

    assert cancelled == ("fx-stale",)
    assert store.effect_state("fx-stale") == "cancelled"
    assert transport.sent == []
    assert registry.turn_lookup(turn_id).revision == 2


@pytest.mark.asyncio
async def test_no_turn_means_no_effect_for_turn_bound_producers(runtime) -> None:
    """Fail-closed: without a turn the gateway refuses instead of sending unversioned."""
    from yeoman_gateway.bus.events import OutboundMessage
    from yeoman_gateway.processing.dispatch import SERVICE_PRINCIPALS, EffectNotDeliveredError

    store, registry, gate, router, transport, _adapter = runtime
    # An event the gate never assigned: no thread, no turn.
    dispatcher = ManagedOutboundDispatcher(router=router, bus=MessageBus())
    with pytest.raises(EffectNotDeliveredError):
        await dispatcher(
            OutboundMessage(
                channel="whatsapp",
                chat_id=CHAT,
                content="answer",
                metadata={"message_id": "never-assigned"},
            )
        )
    assert transport.sent == []
    assert SERVICE_PRINCIPALS  # service producers keep their turn-free path
    assert store.count_effects() == 0
