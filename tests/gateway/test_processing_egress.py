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
from yeoman_shared.config.schema import Config, WhatsAppConfig

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


@pytest.mark.asyncio
async def test_effect_delivery_gets_exactly_one_transport_attempt(tmp_path: Path) -> None:
    """A timeout after a possible dispatch must not re-send below the gateway."""
    from yeoman_gateway.channels.whatsapp import SEND_MAX_ATTEMPTS, WhatsAppChannel
    from yeoman_gateway.processing.dispatch import EFFECT_PROVENANCE_KEY

    channel = WhatsAppChannel(WhatsAppConfig(debounce_ms=0, debounce_media_ms=0), _RecordingBus())
    channel._connected = True
    calls: list[str] = []

    async def _failing_send(command_type: str, payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        calls.append(command_type)
        raise TimeoutError("no bridge response")

    channel._send_command = _failing_send  # type: ignore[method-assign]

    # Effect-delivered message: one attempt, then the outcome stays unknown.
    with pytest.raises(TimeoutError):
        await channel.send(
            OutboundMessage(
                channel="whatsapp",
                chat_id=CHAT,
                content="managed",
                metadata={EFFECT_PROVENANCE_KEY: "fx1"},
            )
        )
    assert [c for c in calls if c == "send_text"] == ["send_text"]

    # Legacy message: the existing retry behaviour is untouched.
    calls.clear()
    with pytest.raises(TimeoutError):
        await channel.send(OutboundMessage(channel="whatsapp", chat_id=CHAT, content="legacy"))
    assert len([c for c in calls if c == "send_text"]) == SEND_MAX_ATTEMPTS


def test_reaction_provenance_reaches_the_transport() -> None:
    from yeoman_gateway.channels.whatsapp import WhatsAppChannel
    from yeoman_gateway.processing.dispatch import EFFECT_PROVENANCE_KEY

    channel = WhatsAppChannel(WhatsAppConfig(), _RecordingBus())
    assert channel._send_attempts({EFFECT_PROVENANCE_KEY: "fx1"}) == 1
    assert channel._send_attempts({}) > 1


@pytest.mark.asyncio
async def test_self_declared_approval_is_not_authorization(tmp_path: Path) -> None:
    """Authorization never comes from an argument; only policy and principal decide."""
    from yeoman_gateway.processing.models import ExternalActionPayload

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
            payload=ExternalActionPayload(
                action="remote_write",
                arguments={"approved": True, "is_owner": True, "policy_override": "allow"},
            ),
            target=EffectTarget(channel="whatsapp", chat_id=CHAT),
            principal="stranger@s.whatsapp.net",
            capability="external_action",
        )
    )
    result = await gateway.execute_ready(receipt.effect_id)

    assert result.state == "blocked"
    assert executor.calls == []
    store.close()


# --------------------------------------------------------------------------------------
# parametrized integration coverage (Plan 02, Aufgabe 2)
# --------------------------------------------------------------------------------------


async def _produce_final_reply(router: IntentEffectRouter, bus: _RecordingBus) -> None:
    await router.submit_outbound(_outbound("hi"), principal="owner@s.whatsapp.net")


async def _produce_message_tool(router: IntentEffectRouter, bus: _RecordingBus) -> None:
    from yeoman_gateway.agent.tools.message import MessageTool
    from yeoman_gateway.processing.dispatch import ManagedOutboundDispatcher

    tool = MessageTool(send_callback=ManagedOutboundDispatcher(router=router, bus=bus))
    await tool.execute(content="hi", channel="whatsapp", chat_id=CHAT)


async def _produce_voice(router: IntentEffectRouter, bus: _RecordingBus) -> None:
    from yeoman_gateway.processing.dispatch import ManagedOutboundDispatcher

    dispatcher = ManagedOutboundDispatcher(router=router, bus=bus)
    await dispatcher(
        OutboundMessage(channel="whatsapp", chat_id=CHAT, content="", media=["/tmp/v.ogg"])
    )


async def _produce_reaction(router: IntentEffectRouter, bus: _RecordingBus) -> None:
    await router.submit_reaction(
        SendReactionIntent(channel="whatsapp", chat_id=CHAT, message_id="m1", emoji="👍"),
        principal="owner@s.whatsapp.net",
    )


async def _produce_cron(router: IntentEffectRouter, bus: _RecordingBus) -> None:
    from yeoman_gateway.processing.dispatch import ServiceEffectProducer

    await ServiceEffectProducer(router=router, bus=bus).send(
        source="cron",
        operation_ref="cron:job-1:run-1",
        channel="whatsapp",
        chat_id=CHAT,
        content="reminder",
    )


async def _produce_speakup(router: IntentEffectRouter, bus: _RecordingBus) -> None:
    from yeoman_gateway.processing.dispatch import ServiceEffectProducer

    await ServiceEffectProducer(router=router, bus=bus).send(
        source="speakup",
        operation_ref="speakup:1",
        channel="whatsapp",
        chat_id=CHAT,
        content="spontaneous thought",
    )


async def _produce_ipc(router: IntentEffectRouter, bus: _RecordingBus) -> None:
    from yeoman_gateway.processing.dispatch import ServiceEffectProducer

    await ServiceEffectProducer(router=router, bus=bus).send(
        source="ipc",
        operation_ref="ipc:1",
        channel="whatsapp",
        chat_id=CHAT,
        content="from overseer",
    )


async def _run_producer(produce, router: IntentEffectRouter, bus: _RecordingBus) -> None:
    """Producers that report to a model surface the refusal; the ledger still decides."""
    from yeoman_gateway.processing.dispatch import EffectNotDeliveredError

    try:
        await produce(router, bus)
    except EffectNotDeliveredError:
        pass


PRODUCERS = {
    "final_reply": _produce_final_reply,
    "message_tool": _produce_message_tool,
    "voice": _produce_voice,
    "reaction": _produce_reaction,
    "cron": _produce_cron,
    "speakup": _produce_speakup,
    "ipc": _produce_ipc,
}


@pytest.mark.parametrize("producer_name", sorted(PRODUCERS))
@pytest.mark.asyncio
async def test_every_producer_is_authorized_once(
    tmp_path: Path, producer_name: str
) -> None:
    """Allowed -> exactly one executor call; denied -> none; duplicate -> no second call."""
    produce = PRODUCERS[producer_name]

    # allowed
    store = ProcessingStore(tmp_path / "allowed.db")
    executor = _Executor("sent")
    router, _ = _router(store, executor)
    await _run_producer(produce, router, _RecordingBus())
    assert len(executor.calls) == 1, producer_name
    store.close()

    # denied
    store = ProcessingStore(tmp_path / "denied.db")
    executor = _Executor("sent")
    router, _ = _router(store, executor, capabilities=_DenyAll())
    await _run_producer(produce, router, _RecordingBus())
    assert executor.calls == [], producer_name
    assert store.count_effects() == 1, producer_name
    store.close()

    # duplicate operation
    store = ProcessingStore(tmp_path / "duplicate.db")
    executor = _Executor("sent")
    router, _ = _router(store, executor)
    await _run_producer(produce, router, _RecordingBus())
    await _run_producer(produce, router, _RecordingBus())
    assert len(executor.calls) == 1, producer_name
    store.close()


@pytest.mark.parametrize("producer_name", sorted(PRODUCERS))
@pytest.mark.asyncio
async def test_generic_transport_error_never_becomes_a_chat_error(
    tmp_path: Path, producer_name: str
) -> None:
    produce = PRODUCERS[producer_name]
    store = ProcessingStore(tmp_path / "p.db")
    executor = _Executor(error=RuntimeError("bridge exploded"))
    router, gateway = _router(store, executor)
    bus = _RecordingBus()

    await _run_producer(produce, router, bus)

    assert executor.calls != [], producer_name
    assert bus.sent == [], producer_name
    effects = store.list_effects()
    assert effects and effects[0].state == "unknown", producer_name
    store.close()


@pytest.mark.asyncio
async def test_typing_stays_ephemeral_presence_without_an_effect(tmp_path: Path) -> None:
    """Typing must never become a durable, replayable outbox effect (spec R08)."""
    from yeoman_gateway.app.bootstrap import OrchestratorService
    from yeoman_gateway.core.intents import SetTypingIntent
    from yeoman_gateway.providers.base import LLMProvider, LLMResponse

    class _Provider(LLMProvider):
        async def chat(self, messages, tools=None, model=None, max_tokens=4096,
                       temperature=0.7, reasoning=None) -> LLMResponse:
            return LLMResponse(content="")

        def get_default_model(self) -> str:
            return "test/model"

    store = ProcessingStore(tmp_path / "p.db")
    executor = _Executor("sent")
    router, _ = _router(store, executor)
    typing_calls: list[tuple[str, str, bool]] = []

    async def _typing(channel: str, chat_id: str, enabled: bool) -> None:
        typing_calls.append((channel, chat_id, enabled))


    service = OrchestratorService(
        bus=_RecordingBus(),
        orchestrator=None,  # type: ignore[arg-type]
        typing_adapter=_typing,
        telemetry=None,  # type: ignore[arg-type]
        memory=None,  # type: ignore[arg-type]
        effect_router=router,
    )
    await service._dispatch_intents(
        [SetTypingIntent(channel="whatsapp", chat_id=CHAT, enabled=True)], principal="owner"
    )

    assert typing_calls == [("whatsapp", CHAT, True)]
    assert executor.calls == []
    assert store.count_effects() == 0
    store.close()


# --------------------------------------------------------------------------------------
# confirming transport (activation enabler)
# --------------------------------------------------------------------------------------


class _ConfirmingTransport:
    """Stands in for ChannelManager.send_now()."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.sent: list[OutboundMessage] = []
        self.reacted: list[Any] = []

    async def send_now(self, message: OutboundMessage) -> None:
        if self.error is not None:
            raise self.error
        self.sent.append(message)

    async def send_reaction_now(self, message: Any) -> None:
        if self.error is not None:
            raise self.error
        self.reacted.append(message)


@pytest.mark.asyncio
async def test_confirming_transport_reports_sent(tmp_path: Path) -> None:
    """With a real transport adapter a successful send is proven, not guessed."""
    from yeoman_gateway.processing.dispatch import BusEffectExecutor

    store = ProcessingStore(tmp_path / "p.db")
    transport = _ConfirmingTransport()
    executor = BusEffectExecutor(
        bus=_RecordingBus(),
        direct_sender=transport.send_now,
        direct_reaction_sender=transport.send_reaction_now,
    )
    gateway = EffectGateway(
        store,
        authorizer=SnapshotEffectAuthorizer(
            snapshots=_StaticSnapshots(), capabilities=_AllowAll(), clock=_Clock(0)
        ),
        executor=executor,
        clock=_Clock(0),
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
    result = await gateway.execute_ready("fx1")

    assert result.state == "sent"
    assert [message.content for message in transport.sent] == ["hi"]
    assert store.effect_state("fx1") == "sent"
    store.close()


@pytest.mark.asyncio
async def test_message_tool_reports_delivery_with_a_confirming_transport(tmp_path: Path) -> None:
    from yeoman_gateway.agent.tools.message import MessageTool
    from yeoman_gateway.processing.dispatch import (
        BusEffectExecutor,
        ManagedOutboundDispatcher,
    )

    store = ProcessingStore(tmp_path / "p.db")
    transport = _ConfirmingTransport()
    gateway = EffectGateway(
        store,
        authorizer=SnapshotEffectAuthorizer(
            snapshots=_StaticSnapshots(), capabilities=_AllowAll(), clock=_Clock(0)
        ),
        executor=BusEffectExecutor(
            bus=_RecordingBus(),
            direct_sender=transport.send_now,
            direct_reaction_sender=transport.send_reaction_now,
        ),
        clock=_Clock(0),
    )
    router = IntentEffectRouter(gateway=gateway, config=_config(), clock=_Clock(0))
    tool = MessageTool(send_callback=ManagedOutboundDispatcher(router=router, bus=_RecordingBus()))

    result = await tool.execute(content="hi", channel="whatsapp", chat_id=CHAT)

    assert "delivery complete" in result.lower()
    assert len(transport.sent) == 1
    store.close()


@pytest.mark.asyncio
async def test_pre_dispatch_refusal_is_proven_not_executed(tmp_path: Path) -> None:
    from yeoman_gateway.processing.dispatch import BusEffectExecutor

    store = ProcessingStore(tmp_path / "p.db")
    transport = _ConfirmingTransport(error=RuntimeError("WhatsApp bridge not connected"))
    gateway = EffectGateway(
        store,
        authorizer=SnapshotEffectAuthorizer(
            snapshots=_StaticSnapshots(), capabilities=_AllowAll(), clock=_Clock(0)
        ),
        executor=BusEffectExecutor(
            bus=_RecordingBus(),
            direct_sender=transport.send_now,
            direct_reaction_sender=transport.send_reaction_now,
        ),
        clock=_Clock(0),
    )
    _ = gateway.submit(
        EffectEnvelope(
            effect_id="fx1",
            operation_key="k1",
            payload=TextPayload(text="hi"),
            target=EffectTarget(channel="whatsapp", chat_id=CHAT),
            capability="send_text",
        )
    )
    result = await gateway.execute_ready("fx1")

    assert result.state == "failed"  # proven not executed, requeueable with evidence
    assert store.effect_state("fx1") == "failed"
    store.close()


@pytest.mark.asyncio
async def test_failure_after_possible_dispatch_stays_unknown(tmp_path: Path) -> None:
    from yeoman_gateway.processing.dispatch import BusEffectExecutor

    store = ProcessingStore(tmp_path / "p.db")
    transport = _ConfirmingTransport(error=TimeoutError("no bridge response"))
    gateway = EffectGateway(
        store,
        authorizer=SnapshotEffectAuthorizer(
            snapshots=_StaticSnapshots(), capabilities=_AllowAll(), clock=_Clock(0)
        ),
        executor=BusEffectExecutor(
            bus=_RecordingBus(),
            direct_sender=transport.send_now,
            direct_reaction_sender=transport.send_reaction_now,
        ),
        clock=_Clock(0),
    )
    _ = gateway.submit(
        EffectEnvelope(
            effect_id="fx1",
            operation_key="k1",
            payload=TextPayload(text="hi"),
            target=EffectTarget(channel="whatsapp", chat_id=CHAT),
            capability="send_text",
        )
    )
    result = await gateway.execute_ready("fx1")

    assert result.state == "unknown"
    store.close()


# --------------------------------------------------------------------------------------
# send budgets (spec R08 start values)
# --------------------------------------------------------------------------------------


def test_send_budget_counts_units_and_slides() -> None:
    from yeoman_gateway.processing.dispatch import SendBudget

    now = [0.0]
    budget = SendBudget(units=2, window_seconds=10, waiting_cap=5, clock=lambda: now[0])

    assert budget.reserve("whatsapp:chat") is True
    assert budget.reserve("whatsapp:chat") is True
    assert budget.reserve("whatsapp:chat") is False  # hard cap within the window
    assert budget.spent("whatsapp:chat") == 2

    now[0] = 11.0  # window slides
    assert budget.reserve("whatsapp:chat") is True


@pytest.mark.asyncio
async def test_chat_budget_blocks_with_a_reason_instead_of_dropping(tmp_path: Path) -> None:
    from yeoman_gateway.processing.dispatch import SendBudget

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
    budget = SendBudget(units=1, window_seconds=60, waiting_cap=50, clock=lambda: 0.0)
    router = IntentEffectRouter(
        gateway=gateway, config=_config(), clock=_Clock(0), budget=budget
    )

    await router.submit_outbound(_outbound("one"), principal="owner")
    await router.submit_outbound(_outbound("two"), principal="owner")

    effects = store.list_effects()
    assert sorted(effect.state for effect in effects) == ["blocked", "sent"]
    blocked = next(effect for effect in effects if effect.state == "blocked")
    assert "budget_exhausted" in [item.detail for item in blocked.evidence]
    assert len(executor.calls) == 1
    store.close()


@pytest.mark.asyncio
async def test_outbox_cap_blocks_visibly(tmp_path: Path) -> None:
    from yeoman_gateway.processing.dispatch import SendBudget

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
    budget = SendBudget(units=99, window_seconds=60, waiting_cap=0, clock=lambda: 0.0)
    router = IntentEffectRouter(
        gateway=gateway, config=_config(), clock=_Clock(0), budget=budget
    )

    await router.submit_outbound(_outbound("one"), principal="owner")
    await router.submit_outbound(_outbound("two"), principal="owner")

    effects = store.list_effects()
    assert {effect.state for effect in effects} == {"blocked"}
    details = {item.detail for effect in effects for item in effect.evidence}
    assert "queue_capacity" in details
    assert executor.calls == []
    store.close()


def test_media_counts_as_several_transport_units() -> None:
    from yeoman_gateway.processing.dispatch import payload_units
    from yeoman_gateway.processing.models import MediaPayload

    assert payload_units(TextPayload(text="hi")) == 1
    assert payload_units(MediaPayload(media=("a", "b", "c"))) == 3
