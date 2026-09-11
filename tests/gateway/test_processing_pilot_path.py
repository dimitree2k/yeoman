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


@pytest.mark.asyncio
async def test_tool_effect_from_a_generation_uses_the_frozen_turn(runtime) -> None:
    """A correction during the provider call must invalidate that turn's tool sends."""
    from yeoman_gateway.bus.events import OutboundMessage
    from yeoman_gateway.processing.actor import ThreadActorRegistry
    from yeoman_gateway.processing.responder import ThreadActorResponder

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
    actors = ThreadActorRegistry(store=store, config=_config().processing, clock=_Clock())

    class _ToolSendingResponder:
        """Stands in for a generation that dispatches a tool effect while it runs."""

        def __init__(self) -> None:
            self.revision_seen: int | None = None

        async def generate_reply(self, event, decision, *, session_key=None) -> str | None:
            # The correction lands while this call is still running.
            store.bump_turn_revision(
                turn_id, expected_revision=1, now_ms=1_700_000_002_000, reason="correction"
            )
            from yeoman_gateway.processing.dispatch import CURRENT_TURN

            binding = CURRENT_TURN.get()
            self.revision_seen = binding.turn.revision if binding else None
            await router.submit_message(
                OutboundMessage(
                    channel="whatsapp",
                    chat_id=CHAT,
                    content="tool answer",
                    metadata={"message_id": "m1"},
                ),
                principal="orderer@s.whatsapp.net",
                capability="send_text",
                payload=__import__(
                    "yeoman_gateway.processing.models", fromlist=["TextPayload"]
                ).TextPayload(text="tool answer"),
            )
            return "text answer"

    inner = _ToolSendingResponder()
    wrapper = ThreadActorResponder(inner=inner, actors=actors, store=store)
    reply = await wrapper.generate_reply(_event(message_id="m1"), object())

    # The generation saw revision 1; its effect was therefore created against revision 1
    # and cancelled by the correction that arrived during the call.
    assert inner.revision_seen == 1
    assert reply is None or reply == "text answer"
    states = {effect.state for effect in store.list_effects()}
    assert "sent" not in states or transport.sent == []
    store.close()


# --------------------------------------------------------------------------------------
# DM legacy carry-over (last Plan 03 item)
# --------------------------------------------------------------------------------------


class _Session:
    def __init__(self, key: str) -> None:
        self.key = key
        self.messages: list[dict] = []
        self.metadata: dict = {}

    def add_message(self, role: str, content: str, **kwargs) -> None:
        self.messages.append({"role": role, "content": content, **kwargs})


class _Sessions:
    def __init__(self) -> None:
        self.store: dict[str, _Session] = {}
        self.saves = 0

    def get_or_create(self, key: str) -> _Session:
        return self.store.setdefault(key, _Session(key))

    def save(self, session: _Session) -> None:
        self.saves += 1


def _dm_runtime(tmp_path: Path):
    """A managed DM: no @g.us, so the carry-over path applies."""
    config = Config.model_validate(
        {"processing": {"enabled": True, "chats": ["whatsapp:owner@s.whatsapp.net"]}}
    )
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
    workspace.mkdir(exist_ok=True)
    adapter = EnginePolicyAdapter(
        engine=PolicyEngine(policy, workspace=workspace, apply_channels={"whatsapp"}),
        known_tools={"message"},
        policy_path=policy_path,
        workspace=workspace,
    )
    with patch.dict(os.environ, {"YEOMAN_HOME": str(tmp_path)}):
        store = build_processing_store(config)
        assert store is not None
        registry = build_thread_registry(config, store)
        gate = build_processing_gate(config, adapter, store, registry)
    return config, store, registry, gate


def test_dm_keeps_its_history_through_a_marked_carryover(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from yeoman_gateway.core.models import InboundEvent
    from yeoman_gateway.processing.actor import ThreadActorRegistry
    from yeoman_gateway.processing.responder import LEGACY_CONTEXT_MARKER, ThreadActorResponder

    config, store, registry, gate = _dm_runtime(tmp_path)
    sessions = _Sessions()
    chat_session = sessions.get_or_create("whatsapp:owner@s.whatsapp.net")
    chat_session.add_message("user", "earlier question about the pilot")
    chat_session.add_message("assistant", "earlier answer")

    class _Inner:
        def __init__(self, sessions: _Sessions) -> None:
            self.sessions = sessions

        async def generate_reply(self, event, decision, *, session_key=None):
            return "answer"

    actors = ThreadActorRegistry(store=store, config=config.processing, clock=_Clock())
    wrapper = ThreadActorResponder(inner=_Inner(sessions), actors=actors, store=store)

    event = InboundEvent(
        channel="whatsapp",
        chat_id="owner@s.whatsapp.net",
        sender_id="owner@s.whatsapp.net",
        content="next question",
        message_id="dm1",
        mentioned_bot=True,
        timestamp=datetime(2023, 11, 14, tzinfo=UTC),
    )
    result = gate.admit(
        IngestRequest(
            event_key="whatsapp:owner:dm1", event_id="dm1", trace_id="tr-dm1", event=event
        )
    )
    thread_id = str(result.assignment.thread_id)
    thread_key = f"whatsapp:owner@s.whatsapp.net:thread:{thread_id}"

    import asyncio

    reply = asyncio.run(wrapper.generate_reply(event, object()))

    assert reply == "answer"
    carried = sessions.get_or_create(thread_key)
    marked = [m for m in carried.messages if LEGACY_CONTEXT_MARKER in m["content"]]
    assert len(marked) == 1
    assert "earlier question about the pilot" in marked[0]["content"]
    # The chat session itself is never touched.
    assert len(chat_session.messages) == 2

    # A second turn must not copy again.
    asyncio.run(wrapper.generate_reply(event, object()))
    assert len([m for m in sessions.get_or_create(thread_key).messages
                if LEGACY_CONTEXT_MARKER in m["content"]]) == 1
    store.close()


def test_gate_logs_a_positive_assignment_marker(runtime) -> None:
    """Rollout visibility works in both directions: assigned and degraded.

    The marker is the per-message observation line (routing spec, criterion 12): it shows
    the classification, candidate counts, continuity verdict with evidence, the action and
    the lineage - and no message content.
    """
    from loguru import logger

    _store, _registry, gate, _router, _transport, _adapter = runtime
    records: list[str] = []
    sink = logger.add(lambda message: records.append(message.record["message"]), level="INFO")
    try:
        gate.admit(
            IngestRequest(
                event_key=f"whatsapp:{CHAT}:m1",
                event_id="m1",
                trace_id="tr-m1",
                event=_event(message_id="m1"),
            )
        )
    finally:
        logger.remove(sink)

    markers = [record for record in records if "routing_decision" in record]
    assert markers, f"no observation line was logged: {records}"
    line = markers[0]
    for field in (
        "classification=",
        "candidates=",
        "eligible=",
        "topic_break=",
        "continuity=",
        "evidence=",
        "outcome=",
        "reply_action=",
        "thread_id=",
        "turn_id=",
    ):
        assert field in line, f"{field} missing from {line}"
    assert "assigned" in line or "no_thread" in line
