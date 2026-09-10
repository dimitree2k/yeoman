"""Plan 02 / R05, R08: one dispatch line for managed effects, plus bypass negatives."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from yeoman_gateway.bus.events import OutboundMessage, ReactionMessage
from yeoman_gateway.bus.queue import MessageBus
from yeoman_gateway.core.intents import SendOutboundIntent, SendReactionIntent
from yeoman_gateway.policy.engine import PolicyEngine
from yeoman_gateway.policy.schema import PolicyConfig
from yeoman_gateway.processing.dispatch import (
    BusEffectExecutor,
    EffectPayloadRejectedError,
    IntentEffectRouter,
    validate_payload,
)
from yeoman_gateway.processing.effects import EffectGateway
from yeoman_gateway.processing.models import (
    DecisionRecord,
    EffectEnvelope,
    EffectReceipt,
    EffectTarget,
    PolicySnapshot,
    TextPayload,
)
from yeoman_gateway.processing.policy import PolicyCapabilityResolver, SnapshotEffectAuthorizer
from yeoman_gateway.processing.store import ProcessingStore
from yeoman_shared.config.schema import Config

CHAT = "chat@g.us"
OTHER_CHAT = "other@g.us"


class _Clock:
    def __init__(self, value: int = 0) -> None:
        self.value = value

    def __call__(self) -> int:
        return self.value


class _StaticSnapshots:
    def __init__(self, healthy: bool = True) -> None:
        self._snapshot = PolicySnapshot(
            version="policy-v1", policy_hash="hash-v1", loaded_ms=0, healthy=healthy
        )

    def snapshot(self) -> PolicySnapshot:
        return self._snapshot


class _AllowAll:
    def resolve(self, *, principal: str, target: EffectTarget, capability: str):
        return True, "allow"


class _DenyAll:
    def resolve(self, *, principal: str, target: EffectTarget, capability: str):
        return False, "permission_denied"


class _Executor:
    def __init__(self, result: str = "sent", error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[EffectEnvelope] = []

    async def execute(self, envelope: EffectEnvelope) -> EffectReceipt:
        self.calls.append(envelope)
        if self.error is not None:
            raise self.error
        return EffectReceipt(effect_id=envelope.effect_id, state=self.result)


def _config(chats: tuple[str, ...] = (f"whatsapp:{CHAT}",)) -> Config:
    return Config.model_validate({"processing": {"enabled": True, "chats": list(chats)}})


def _router(store: ProcessingStore, executor: _Executor, capabilities: Any | None = None):
    gateway = EffectGateway(
        store,
        authorizer=SnapshotEffectAuthorizer(
            snapshots=_StaticSnapshots(),
            capabilities=capabilities or _AllowAll(),
            clock=_Clock(0),
        ),
        executor=executor,
        clock=_Clock(0),
    )
    return IntentEffectRouter(gateway=gateway, config=_config(), clock=_Clock(0)), gateway


def _outbound(text: str = "hello", **metadata: Any) -> SendOutboundIntent:
    return SendOutboundIntent(
        event=OutboundMessage(
            channel="whatsapp",
            chat_id=CHAT,
            content=text,
            metadata={"message_id": "m1", **metadata},
        )
    )


def _reaction() -> SendReactionIntent:
    return SendReactionIntent(
        channel="whatsapp", chat_id=CHAT, message_id="m1", emoji="👍"
    )


class _RecordingBus(MessageBus):
    def __init__(self) -> None:
        super().__init__()
        self.sent: list[OutboundMessage] = []
        self.reacted: list[ReactionMessage] = []

    async def publish_outbound(self, message: OutboundMessage) -> None:
        self.sent.append(message)

    async def publish_reaction(self, message: ReactionMessage) -> None:
        self.reacted.append(message)


# --------------------------------------------------------------------------------------
# dispatch line
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_managed_reply_uses_the_effect_path_only(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "p.db")
    executor = _Executor("sent")
    router, _ = _router(store, executor)
    bus = _RecordingBus()

    handled = await router.submit_outbound(_outbound("hi"), principal="owner@s.whatsapp.net")

    assert handled is True
    assert len(executor.calls) == 1
    assert isinstance(executor.calls[0].payload, TextPayload)
    assert executor.calls[0].target.chat_id == CHAT
    assert executor.calls[0].principal == "owner@s.whatsapp.net"
    assert bus.sent == []  # the router itself never publishes
    assert store.count_effects() == 1
    store.close()


@pytest.mark.asyncio
async def test_unmanaged_chat_keeps_the_legacy_path(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "p.db")
    executor = _Executor("sent")
    gateway = EffectGateway(
        store,
        authorizer=SnapshotEffectAuthorizer(
            snapshots=_StaticSnapshots(), capabilities=_AllowAll(), clock=_Clock(0)
        ),
        executor=executor,
        clock=_Clock(0),
    )
    router = IntentEffectRouter(
        gateway=gateway, config=_config(chats=("whatsapp:somewhere-else@g.us",)), clock=_Clock(0)
    )

    handled = await router.submit_outbound(_outbound("hi"), principal="owner@s.whatsapp.net")

    assert handled is False
    assert executor.calls == []
    assert store.count_effects() == 0
    store.close()


@pytest.mark.asyncio
async def test_denied_effect_is_never_executed(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "p.db")
    executor = _Executor("sent")
    router, _ = _router(store, executor, capabilities=_DenyAll())
    bus = _RecordingBus()

    handled = await router.submit_outbound(_outbound("hi"), principal="stranger@s.whatsapp.net")

    assert handled is True  # the managed path owns the chat
    assert executor.calls == []
    assert bus.sent == []
    effects = store.get_lineage("m1").effects
    assert len(effects) == 1
    assert effects[0].state == "blocked"
    store.close()


@pytest.mark.asyncio
async def test_duplicate_delivery_is_not_sent_twice(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "p.db")
    executor = _Executor("sent")
    router, _ = _router(store, executor)

    await router.submit_outbound(_outbound("hi"), principal="owner@s.whatsapp.net")
    await router.submit_outbound(_outbound("hi"), principal="owner@s.whatsapp.net")

    assert len(executor.calls) == 1
    assert store.count_effects() == 1
    store.close()


@pytest.mark.asyncio
async def test_transport_failure_never_becomes_a_chat_error(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "p.db")
    executor = _Executor(error=TimeoutError("bridge timeout"))
    router, gateway = _router(store, executor)
    bus = _RecordingBus()

    await router.submit_outbound(_outbound("hi"), principal="owner@s.whatsapp.net")

    assert bus.sent == []  # no fallback legacy send, no error text
    stored = store.effect_by_operation_key(
        next(iter([row.operation_key for row in store.get_lineage("m1").effects]))
    )
    assert stored is not None and stored.state == "unknown"
    assert executor.calls and len(executor.calls) == 1
    assert gateway.wired is True
    store.close()


@pytest.mark.asyncio
async def test_reaction_runs_through_the_same_path(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "p.db")
    executor = _Executor("sent")
    router, _ = _router(store, executor)

    await router.submit_reaction(_reaction(), principal="owner@s.whatsapp.net")

    assert len(executor.calls) == 1
    assert executor.calls[0].capability == "send_reaction"
    store.close()


@pytest.mark.asyncio
async def test_payload_validation_rejects_empty_text(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "p.db")
    bus = _RecordingBus()
    executor = BusEffectExecutor(bus=bus)
    envelope = EffectEnvelope(
        effect_id="fx1",
        operation_key="k1",
        payload=TextPayload(text="   "),
        target=EffectTarget(channel="whatsapp", chat_id=CHAT),
    )
    with pytest.raises(EffectPayloadRejectedError):
        validate_payload(envelope)

    gateway = EffectGateway(
        store,
        authorizer=SnapshotEffectAuthorizer(
            snapshots=_StaticSnapshots(), capabilities=_AllowAll(), clock=_Clock(0)
        ),
        executor=executor,
        clock=_Clock(0),
    )
    receipt = gateway.submit(envelope)
    result = await gateway.execute_ready(receipt.effect_id)

    assert result.state == "unknown"  # invalid payload never counts as executed
    assert bus.sent == []
    store.close()


@pytest.mark.asyncio
async def test_queue_acceptance_is_not_reported_as_sent(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "p.db")
    bus = _RecordingBus()
    gateway = EffectGateway(
        store,
        authorizer=SnapshotEffectAuthorizer(
            snapshots=_StaticSnapshots(), capabilities=_AllowAll(), clock=_Clock(0)
        ),
        executor=BusEffectExecutor(bus=bus),
        clock=_Clock(0),
    )
    receipt = gateway.submit(
        EffectEnvelope(
            effect_id="fx1",
            operation_key="k1",
            payload=TextPayload(text="hi"),
            target=EffectTarget(channel="whatsapp", chat_id=CHAT),
        )
    )
    result = await gateway.execute_ready(receipt.effect_id)

    assert [message.content for message in bus.sent] == ["hi"]
    assert result.state == "unknown"
    assert "unproven" in (result.detail or "")
    store.close()


# --------------------------------------------------------------------------------------
# bypass negatives (Aufgabe 3)
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_model_supplied_principal_is_ignored(tmp_path: Path) -> None:
    """A producer must pass the triggering principal; model metadata is not authority."""
    store = ProcessingStore(tmp_path / "p.db")
    executor = _Executor("sent")
    gateway = EffectGateway(
        store,
        authorizer=SnapshotEffectAuthorizer(
            snapshots=_StaticSnapshots(), capabilities=_DenyAll(), clock=_Clock(0)
        ),
        executor=executor,
        clock=_Clock(0),
    )
    router = IntentEffectRouter(gateway=gateway, config=_config(), clock=_Clock(0))

    intent = SendOutboundIntent(
        event=OutboundMessage(
            channel="whatsapp",
            chat_id=CHAT,
            content="hi",
            metadata={"message_id": "m1", "principal": "owner@s.whatsapp.net"},
        )
    )
    await router.submit_outbound(intent, principal="stranger@s.whatsapp.net")

    assert executor.calls == []
    store.close()


@pytest.mark.asyncio
async def test_cross_chat_target_cannot_reuse_a_permission(tmp_path: Path) -> None:
    policy = PolicyConfig.model_validate(
        {
            "owners": {"whatsapp": ["owner@s.whatsapp.net"]},
            "channels": {
                "whatsapp": {
                    "default": {"whoCanTalk": {"mode": "everyone"}},
                    "chats": {OTHER_CHAT: {"whoCanTalk": {"mode": "owner_only"}}},
                }
            },
        }
    )
    engine = PolicyEngine(policy, workspace=tmp_path, apply_channels={"whatsapp"})
    resolver = PolicyCapabilityResolver(
        engine_provider=lambda: engine, known_tools=lambda: {"message"}
    )

    allowed, _ = resolver.resolve(
        principal="stranger@s.whatsapp.net",
        target=EffectTarget(channel="whatsapp", chat_id=CHAT),
        capability="send_text",
    )
    assert allowed is True

    allowed, reason = resolver.resolve(
        principal="stranger@s.whatsapp.net",
        target=EffectTarget(channel="whatsapp", chat_id=OTHER_CHAT),
        capability="send_text",
    )
    assert allowed is False
    assert reason == "permission_denied"


@pytest.mark.asyncio
async def test_unmapped_capability_cannot_be_declared_by_a_producer(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "p.db")
    executor = _Executor("sent")
    gateway = EffectGateway(
        store,
        authorizer=SnapshotEffectAuthorizer(
            snapshots=_StaticSnapshots(), capabilities=_DenyAll(), clock=_Clock(0)
        ),
        executor=executor,
        clock=_Clock(0),
    )
    receipt = gateway.submit(
        EffectEnvelope(
            effect_id="fx1",
            operation_key="k1",
            payload=TextPayload(text="hi"),
            target=EffectTarget(channel="whatsapp", chat_id=CHAT),
            principal="owner@s.whatsapp.net",
            capability="delegate_write_to_remote_agent",
        )
    )
    result = await gateway.execute_ready(receipt.effect_id)

    assert result.state == "blocked"
    assert executor.calls == []

    decisions = store.get_lineage("").decisions
    assert decisions and decisions[0].outcome == "deny"
    store.close()


def test_decision_records_keep_policy_identity(tmp_path: Path) -> None:
    authorizer = SnapshotEffectAuthorizer(
        snapshots=_StaticSnapshots(), capabilities=_AllowAll(), clock=_Clock(5)
    )
    record: DecisionRecord = authorizer.check(
        EffectEnvelope(
            effect_id="fx1",
            operation_key="k1",
            payload=TextPayload(text="hi"),
            target=EffectTarget(channel="whatsapp", chat_id=CHAT),
            principal="owner@s.whatsapp.net",
            capability="send_text",
        ),
        None,
    )
    assert record.outcome == "allow"
    assert record.policy_version == "policy-v1"
    assert record.policy_hash == "hash-v1"
    assert record.created_ms == 5


# --------------------------------------------------------------------------------------
# tool producers (Plan 02, Aufgabe 2)
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_message_tool_goes_through_the_gateway_for_managed_chats(tmp_path: Path) -> None:
    from yeoman_gateway.agent.tools.message import MessageTool
    from yeoman_gateway.processing.dispatch import ManagedOutboundDispatcher

    store = ProcessingStore(tmp_path / "p.db")
    executor = _Executor("sent")
    router, _ = _router(store, executor)
    bus = _RecordingBus()
    tool = MessageTool(send_callback=ManagedOutboundDispatcher(router=router, bus=bus))

    result = await tool.execute(content="hi", channel="whatsapp", chat_id=CHAT)

    assert len(executor.calls) == 1
    assert executor.calls[0].capability == "send_text"
    assert bus.sent == []
    assert "delivered" in result.lower()
    store.close()


@pytest.mark.asyncio
async def test_message_tool_keeps_legacy_publish_for_unmanaged_chats(tmp_path: Path) -> None:
    from yeoman_gateway.agent.tools.message import MessageTool
    from yeoman_gateway.processing.dispatch import ManagedOutboundDispatcher

    store = ProcessingStore(tmp_path / "p.db")
    executor = _Executor("sent")
    gateway = EffectGateway(
        store,
        authorizer=SnapshotEffectAuthorizer(
            snapshots=_StaticSnapshots(), capabilities=_AllowAll(), clock=_Clock(0)
        ),
        executor=executor,
        clock=_Clock(0),
    )
    router = IntentEffectRouter(
        gateway=gateway, config=_config(chats=("whatsapp:elsewhere@g.us",)), clock=_Clock(0)
    )
    bus = _RecordingBus()
    tool = MessageTool(send_callback=ManagedOutboundDispatcher(router=router, bus=bus))

    result = await tool.execute(content="hi", channel="whatsapp", chat_id=CHAT)

    assert [message.content for message in bus.sent] == ["hi"]
    assert executor.calls == []
    assert "delivered" in result.lower()
    store.close()


@pytest.mark.asyncio
async def test_unproven_effect_is_never_reported_as_delivered(tmp_path: Path) -> None:
    from yeoman_gateway.agent.tools.message import MessageTool
    from yeoman_gateway.processing.dispatch import ManagedOutboundDispatcher

    store = ProcessingStore(tmp_path / "p.db")
    bus = _RecordingBus()
    gateway = EffectGateway(
        store,
        authorizer=SnapshotEffectAuthorizer(
            snapshots=_StaticSnapshots(), capabilities=_AllowAll(), clock=_Clock(0)
        ),
        executor=BusEffectExecutor(bus=bus),
        clock=_Clock(0),
    )
    router = IntentEffectRouter(gateway=gateway, config=_config(), clock=_Clock(0))
    tool = MessageTool(send_callback=ManagedOutboundDispatcher(router=router, bus=bus))

    result = await tool.execute(content="hi", channel="whatsapp", chat_id=CHAT)

    assert [message.content for message in bus.sent] == ["hi"]
    assert result.startswith("Error")
    assert "delivery complete" not in result.lower()
    store.close()


@pytest.mark.asyncio
async def test_managed_delete_is_disabled_until_a_contract_exists(tmp_path: Path) -> None:
    from yeoman_gateway.agent.tools.delete_message import DeleteMessageTool
    from yeoman_gateway.processing.dispatch import ManagedOutboundDispatcher

    store = ProcessingStore(tmp_path / "p.db")
    bus = _RecordingBus()
    gateway = EffectGateway(
        store,
        authorizer=SnapshotEffectAuthorizer(
            snapshots=_StaticSnapshots(), capabilities=_AllowAll(), clock=_Clock(0)
        ),
        executor=BusEffectExecutor(bus=bus),
        clock=_Clock(0),
    )
    router = IntentEffectRouter(gateway=gateway, config=_config(), clock=_Clock(0))
    tool = DeleteMessageTool(send_callback=ManagedOutboundDispatcher(router=router, bus=bus))
    tool.set_context("whatsapp", CHAT, is_owner=True)

    result = await tool.execute(message_id="m1")

    assert bus.sent == []
    assert result.startswith("Error")
    effects = store.get_lineage("turn").effects
    assert effects and effects[0].state == "unknown"
    assert effects[0].capability == "delete_message"
    store.close()


def test_outbound_classification_is_explicit() -> None:
    from yeoman_gateway.processing.dispatch import classify_outbound

    capability, payload = classify_outbound(
        OutboundMessage(channel="whatsapp", chat_id=CHAT, content="hi")
    )
    assert capability == "send_text"
    assert isinstance(payload, TextPayload)

    capability, _ = classify_outbound(
        OutboundMessage(channel="whatsapp", chat_id=CHAT, content="", media=["/tmp/a.ogg"])
    )
    assert capability == "send_media"

    capability, payload = classify_outbound(
        OutboundMessage(
            channel="whatsapp",
            chat_id=CHAT,
            content="",
            metadata={"delete_message": {"message_id": "m9"}},
        )
    )
    assert capability == "delete_message"
    assert payload.message_id == "m9"


@pytest.mark.asyncio
async def test_legacy_publish_is_refused_for_managed_chats(tmp_path: Path) -> None:
    """Runtime half of "one producer per chat and turn"."""
    from yeoman_gateway.processing.dispatch import (
        EFFECT_PROVENANCE_KEY,
        BusEffectExecutor,
        managed_outbound_guard,
    )

    store = ProcessingStore(tmp_path / "p.db")
    bus = MessageBus()
    gateway = EffectGateway(
        store,
        authorizer=SnapshotEffectAuthorizer(
            snapshots=_StaticSnapshots(), capabilities=_AllowAll(), clock=_Clock(0)
        ),
        executor=BusEffectExecutor(bus=bus, mark_provenance=True),
        clock=_Clock(0),
    )
    router = IntentEffectRouter(gateway=gateway, config=_config(), clock=_Clock(0))
    bus.set_managed_outbound_guard(managed_outbound_guard(router))

    # A legacy producer without provenance is dropped, not delivered.
    await bus.publish_outbound(OutboundMessage(channel="whatsapp", chat_id=CHAT, content="legacy"))
    assert bus.outbound.empty()

    # An unmanaged chat keeps the legacy path.
    await bus.publish_outbound(
        OutboundMessage(channel="whatsapp", chat_id="free@g.us", content="legacy")
    )
    assert (await bus.consume_outbound()).content == "legacy"

    # The effect transport carries provenance and passes.
    await router.submit_outbound(_outbound("managed"), principal="owner@s.whatsapp.net")
    delivered = await bus.consume_outbound()
    assert delivered.content == "managed"
    assert delivered.metadata.get(EFFECT_PROVENANCE_KEY)
    store.close()


@pytest.mark.asyncio
async def test_provenance_cannot_be_forged_by_a_producer(tmp_path: Path) -> None:
    from yeoman_gateway.processing.dispatch import (
        EFFECT_PROVENANCE_KEY,
        managed_outbound_guard,
    )

    store = ProcessingStore(tmp_path / "p.db")
    executor = _Executor("sent")
    router, _ = _router(store, executor)
    guard = managed_outbound_guard(router)

    allowed, reason = guard(
        OutboundMessage(
            channel="whatsapp",
            chat_id=CHAT,
            content="forged",
            metadata={EFFECT_PROVENANCE_KEY: "fx-not-real"},
        )
    )
    # The marker alone proves nothing: it must name a persisted effect for this chat.
    assert allowed is False
    assert reason == "forged_effect_provenance"

    # A real effect for a different chat does not authorize this target either.
    receipt_gateway = EffectGateway(
        store,
        authorizer=SnapshotEffectAuthorizer(
            snapshots=_StaticSnapshots(), capabilities=_AllowAll(), clock=_Clock(0)
        ),
        executor=_Executor("sent"),
        clock=_Clock(0),
    )
    envelope = EffectEnvelope(
        effect_id="fx-other",
        operation_key="k-other",
        payload=TextPayload(text="x"),
        target=EffectTarget(channel="whatsapp", chat_id="other-chat@g.us"),
    )
    receipt_gateway.submit(envelope)
    allowed, reason = guard(
        OutboundMessage(
            channel="whatsapp",
            chat_id=CHAT,
            content="forged",
            metadata={EFFECT_PROVENANCE_KEY: "fx-other"},
        )
    )
    assert allowed is False and reason == "forged_effect_provenance"
    store.close()


# --------------------------------------------------------------------------------------
# system producers and shared output control (Plan 02, Aufgabe 2)
# --------------------------------------------------------------------------------------


class _SanitizingSecurity:
    def __init__(self, action: str = "sanitize") -> None:
        self.action = action
        self.calls: list[dict[str, Any]] = []

    def check_output(self, text: str, context: dict[str, Any] | None = None):
        from yeoman_gateway.core.models import SecurityDecision, SecurityResult

        self.calls.append({"text": text, "context": dict(context or {})})
        return SecurityResult(
            stage="output",
            decision=SecurityDecision(action=self.action, reason="test"),
            sanitized_text="[redacted]",
        )


@pytest.mark.asyncio
async def test_text_effects_pass_the_shared_output_control(tmp_path: Path) -> None:
    from yeoman_gateway.processing.dispatch import BusEffectExecutor

    store = ProcessingStore(tmp_path / "p.db")
    bus = _RecordingBus()
    security = _SanitizingSecurity("sanitize")
    gateway = EffectGateway(
        store,
        authorizer=SnapshotEffectAuthorizer(
            snapshots=_StaticSnapshots(), capabilities=_AllowAll(), clock=_Clock(0)
        ),
        executor=BusEffectExecutor(bus=bus, security=security),
        clock=_Clock(0),
    )
    gateway.submit(
        EffectEnvelope(
            effect_id="fx1",
            operation_key="k1",
            payload=TextPayload(text="secret token"),
            target=EffectTarget(channel="whatsapp", chat_id=CHAT),
            capability="send_text",
        )
    )
    await gateway.execute_ready("fx1")

    assert [message.content for message in bus.sent] == ["[redacted]"]
    assert security.calls and security.calls[0]["context"]["capability"] == "send_text"
    store.close()


@pytest.mark.asyncio
async def test_blocked_text_effect_falls_back_to_the_block_message(tmp_path: Path) -> None:
    from yeoman_gateway.processing.dispatch import BusEffectExecutor

    store = ProcessingStore(tmp_path / "p.db")
    bus = _RecordingBus()
    gateway = EffectGateway(
        store,
        authorizer=SnapshotEffectAuthorizer(
            snapshots=_StaticSnapshots(), capabilities=_AllowAll(), clock=_Clock(0)
        ),
        executor=BusEffectExecutor(
            bus=bus, security=_SanitizingSecurity("block"), security_block_message="[blocked]"
        ),
        clock=_Clock(0),
    )
    gateway.submit(
        EffectEnvelope(
            effect_id="fx1",
            operation_key="k1",
            payload=TextPayload(text="danger"),
            target=EffectTarget(channel="whatsapp", chat_id=CHAT),
        )
    )
    await gateway.execute_ready("fx1")

    assert [message.content for message in bus.sent] == ["[redacted]"]
    store.close()


@pytest.mark.asyncio
async def test_service_producer_uses_a_service_principal(tmp_path: Path) -> None:
    from yeoman_gateway.processing.dispatch import ServiceEffectProducer

    store = ProcessingStore(tmp_path / "p.db")
    executor = _Executor("sent")
    router, _ = _router(store, executor)
    bus = _RecordingBus()
    producer = ServiceEffectProducer(router=router, bus=bus)

    receipt = await producer.send(
        source="cron",
        operation_ref="cron:job-1:run-1",
        channel="whatsapp",
        chat_id=CHAT,
        content="reminder",
    )

    assert receipt is not None
    assert len(executor.calls) == 1
    assert executor.calls[0].principal == "service:cron"
    assert bus.sent == []
    store.close()


@pytest.mark.asyncio
async def test_service_producer_keeps_legacy_for_unmanaged_chats(tmp_path: Path) -> None:
    from yeoman_gateway.processing.dispatch import ServiceEffectProducer

    store = ProcessingStore(tmp_path / "p.db")
    executor = _Executor("sent")
    gateway = EffectGateway(
        store,
        authorizer=SnapshotEffectAuthorizer(
            snapshots=_StaticSnapshots(), capabilities=_AllowAll(), clock=_Clock(0)
        ),
        executor=executor,
        clock=_Clock(0),
    )
    router = IntentEffectRouter(
        gateway=gateway, config=_config(chats=("whatsapp:elsewhere@g.us",)), clock=_Clock(0)
    )
    bus = _RecordingBus()
    producer = ServiceEffectProducer(router=router, bus=bus)

    assert (
        await producer.send(
            source="cron",
            operation_ref="cron:job-1:run-1",
            channel="whatsapp",
            chat_id=CHAT,
            content="reminder",
        )
        is None
    )
    assert [message.content for message in bus.sent] == ["reminder"]
    assert executor.calls == []
    store.close()


@pytest.mark.asyncio
async def test_unknown_system_source_is_refused(tmp_path: Path) -> None:
    from yeoman_gateway.processing.dispatch import (
        EffectNotDeliveredError,
        ServiceEffectProducer,
    )

    store = ProcessingStore(tmp_path / "p.db")
    executor = _Executor("sent")
    router, _ = _router(store, executor)
    bus = _RecordingBus()
    producer = ServiceEffectProducer(router=router, bus=bus)

    with pytest.raises(EffectNotDeliveredError):
        await producer.send(
            source="mystery-box",
            operation_ref="x",
            channel="whatsapp",
            chat_id=CHAT,
            content="hello",
        )
    assert executor.calls == []
    assert bus.sent == []
    store.close()


@pytest.mark.asyncio
async def test_service_principal_without_policy_rights_is_blocked(tmp_path: Path) -> None:
    from yeoman_gateway.processing.dispatch import ServiceEffectProducer

    store = ProcessingStore(tmp_path / "p.db")
    executor = _Executor("sent")
    router, _ = _router(store, executor, capabilities=_DenyAll())
    producer = ServiceEffectProducer(router=router, bus=_RecordingBus())

    receipt = await producer.send(
        source="speakup",
        operation_ref="speakup:1",
        channel="whatsapp",
        chat_id=CHAT,
        content="hi",
    )

    assert receipt is not None and receipt.state == "blocked"
    assert executor.calls == []
    store.close()


# --------------------------------------------------------------------------------------
# non-migrated capabilities (Plan 02, Aufgabe 3)
# --------------------------------------------------------------------------------------


def test_new_mode_disables_non_migrated_write_capabilities() -> None:
    from yeoman_gateway.agent.tools.registry import ToolRegistry
    from yeoman_gateway.processing.dispatch import (
        NON_MIGRATED_CAPABILITIES,
        disable_non_migrated_tools,
    )

    class _Tool:
        def __init__(self, name: str) -> None:
            self.name = name

        def to_schema(self) -> dict[str, Any]:
            return {"type": "function", "function": {"name": self.name}}

        def validate_params(self, params: dict[str, Any]) -> list[str]:
            return []

        async def execute(self, **kwargs: Any) -> str:
            return "should not run"

    registry = ToolRegistry()
    for name in ("message", "send_voice", *NON_MIGRATED_CAPABILITIES):
        registry.register(_Tool(name))

    disabled = disable_non_migrated_tools(registry)

    assert set(disabled) == set(NON_MIGRATED_CAPABILITIES)
    visible = {entry["function"]["name"] for entry in registry.get_definitions()}
    assert visible == {"message", "send_voice"}
    assert registry.disabled_tools()["exec"]


@pytest.mark.asyncio
async def test_disabled_tool_refuses_execution_even_if_called_directly() -> None:
    from yeoman_gateway.agent.tools.registry import ToolRegistry

    class _Tool:
        name = "browse"

        def to_schema(self) -> dict[str, Any]:
            return {"type": "function", "function": {"name": self.name}}

        def validate_params(self, params: dict[str, Any]) -> list[str]:
            return []

        async def execute(self, **kwargs: Any) -> str:
            raise AssertionError("disabled tool must not execute")

    registry = ToolRegistry()
    registry.register(_Tool())
    registry.disable("browse", "no capability check")

    result = await registry.execute("browse", {})

    assert result.startswith("Error")
    assert "disabled" in result
