"""Native WhatsApp forward effects stay typed and never fall through to media sends."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from yeoman_gateway.bus.events import OutboundMessage
from yeoman_gateway.bus.queue import MessageBus
from yeoman_gateway.channels.whatsapp import BridgeProtocolError, WhatsAppChannel
from yeoman_gateway.policy.engine import PolicyEngine
from yeoman_gateway.policy.schema import PolicyConfig
from yeoman_gateway.processing.dispatch import (
    BusEffectExecutor,
    ForwardDispatchError,
    IntentEffectRouter,
    classify_outbound,
    validate_payload,
)
from yeoman_gateway.processing.effects import EffectGateway, is_pre_dispatch_error
from yeoman_gateway.processing.models import (
    EffectEnvelope,
    EffectReceipt,
    EffectTarget,
    ForwardPayload,
    PolicySnapshot,
    payload_from_mapping,
)
from yeoman_gateway.processing.policy import PolicyCapabilityResolver, SnapshotEffectAuthorizer
from yeoman_gateway.processing.store import ProcessingStore
from yeoman_shared.config.schema import Config, WhatsAppConfig

CHAT = "target@g.us"


class _Snapshots:
    def snapshot(self) -> PolicySnapshot:
        return PolicySnapshot(version="policy-v1", policy_hash="hash-v1", loaded_ms=0)


class _AllowAll:
    def resolve(self, *, principal: str, target: EffectTarget, capability: str):
        return True, "allow"


class _Executor:
    async def execute(self, envelope: EffectEnvelope) -> EffectReceipt:
        return EffectReceipt(effect_id=envelope.effect_id, state="sent")


class _RecordingBus(MessageBus):
    def __init__(self) -> None:
        super().__init__()
        self.sent: list[OutboundMessage] = []

    async def publish_outbound(self, message: OutboundMessage) -> None:
        self.sent.append(message)


def _envelope(payload: Any, *, capability: str = "forward_message") -> EffectEnvelope:
    return EffectEnvelope(
        effect_id="fx-forward",
        operation_key="forward-operation",
        payload=payload,
        target=EffectTarget(channel="whatsapp", chat_id=CHAT),
        capability=capability,
    )


def test_forward_payload_round_trips_and_rejects_empty_source() -> None:
    payload = ForwardPayload(source_chat_id="source@g.us", source_message_id="SRC-1")
    assert payload_from_mapping(payload.to_dict()) == payload

    with pytest.raises(Exception):
        validate_payload(_envelope(ForwardPayload(source_chat_id="", source_message_id="SRC-1")))


def test_classify_outbound_uses_forward_capability_before_media() -> None:
    capability, payload = classify_outbound(
        OutboundMessage(
            channel="whatsapp",
            chat_id=CHAT,
            content="",
            media=["should-not-be-loaded"],
            metadata={
                "forward_message": {
                    "source_chat_id": "source@g.us",
                    "source_message_id": "SRC-1",
                }
            },
        )
    )

    assert capability == "forward_message"
    assert payload == ForwardPayload(source_chat_id="source@g.us", source_message_id="SRC-1")


@pytest.mark.asyncio
async def test_effect_executor_preserves_only_forward_source_identities() -> None:
    bus = _RecordingBus()
    executor = BusEffectExecutor(bus=bus)
    receipt = await executor.execute(_envelope(ForwardPayload("source@g.us", "SRC-1")))

    assert receipt.state == "unknown"
    assert len(bus.sent) == 1
    message = bus.sent[0]
    assert message.content == ""
    assert message.media == []
    assert message.metadata["forward_message"] == {
        "source_chat_id": "source@g.us",
        "source_message_id": "SRC-1",
    }


@pytest.mark.asyncio
async def test_whatsapp_channel_sends_native_forward_before_media() -> None:
    channel = WhatsAppChannel(WhatsAppConfig(debounce_ms=0, debounce_media_ms=0), MessageBus())
    channel._connected = True
    calls: list[tuple[str, dict[str, Any]]] = []

    async def send_command(command: str, payload: dict[str, Any], **_: Any) -> dict[str, Any]:
        calls.append((command, payload))
        return {"forwarded": {"messageId": "OUT-1"}}

    channel._send_command_with_retry = send_command  # type: ignore[method-assign]

    receipt = await channel.send(
        OutboundMessage(
            channel="whatsapp",
            chat_id=CHAT,
            content="",
            media=["must-not-be-loaded"],
            metadata={
                "forward_message": {
                    "source_chat_id": "source@g.us",
                    "source_message_id": "SRC-1",
                }
            },
        )
    )

    assert calls == [
        (
            "forward_message",
            {
                "to": CHAT,
                "sourceChatJid": "source@g.us",
                "sourceMessageId": "SRC-1",
            },
        )
    ]
    assert receipt == {"provider_message_id": "OUT-1"}


@pytest.mark.asyncio
async def test_whatsapp_channel_lookup_uses_bridge_command() -> None:
    channel = WhatsAppChannel(WhatsAppConfig(), MessageBus())
    channel._connected = True
    calls: list[tuple[str, dict[str, Any]]] = []

    async def send_command(command: str, payload: dict[str, Any], **_: Any) -> dict[str, Any]:
        calls.append((command, payload))
        return {"status": "found", "messageId": "SRC-1"}

    channel._send_command_with_retry = send_command  # type: ignore[method-assign]

    assert await channel.lookup_message(CHAT, "SRC-1") == {
        "status": "found",
        "messageId": "SRC-1",
    }
    assert calls == [("lookup_message", {"chatJid": CHAT, "messageId": "SRC-1"})]


def test_forward_capability_is_denied_without_explicit_policy_tool(tmp_path: Path) -> None:
    policy = PolicyConfig.model_validate(
        {
            "owners": {"whatsapp": ["owner@s.whatsapp.net"]},
            "channels": {
                "whatsapp": {
                    "default": {
                        "whoCanTalk": {"mode": "everyone"},
                        "whenToReply": {"mode": "all"},
                        "allowedTools": {"mode": "allowlist", "tools": ["message"]},
                    }
                }
            },
        }
    )
    engine = PolicyEngine(policy, workspace=tmp_path, apply_channels={"whatsapp"})
    resolver = PolicyCapabilityResolver(
        engine_provider=lambda: engine,
        known_tools=lambda: {"message", "forward_message"},
    )

    assert resolver.resolve(
        principal="owner@s.whatsapp.net",
        target=EffectTarget(channel="whatsapp", chat_id=CHAT),
        capability="forward_message",
    ) == (False, "capability_denied:forward_message")


def test_forward_protocol_error_is_proven_pre_dispatch() -> None:
    assert is_pre_dispatch_error(BridgeProtocolError("ERR_FORWARD_UNAVAILABLE", "gone", False))


def test_forward_dispatch_error_keeps_exact_source_context() -> None:
    error = ForwardDispatchError("source@g.us", "SRC-1", "Original message unavailable")
    assert error.source_chat_id == "source@g.us"
    assert error.source_message_id == "SRC-1"
    assert error.user_message == "Original message unavailable"


@pytest.mark.asyncio
async def test_router_classifies_forward_before_media(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "processing.db")
    gateway = EffectGateway(
        store,
        authorizer=SnapshotEffectAuthorizer(snapshots=_Snapshots(), capabilities=_AllowAll()),
        executor=_Executor(),
    )
    config = Config.model_validate({"processing": {"enabled": True, "chats": [f"whatsapp:{CHAT}"]}})
    router = IntentEffectRouter(gateway=gateway, config=config)
    intent = type("Intent", (), {
        "event": OutboundMessage(
            channel="whatsapp",
            chat_id=CHAT,
            content="",
            media=["not-used"],
            metadata={
                "message_id": "m-forward",
                "forward_message": {
                    "source_chat_id": "source@g.us",
                    "source_message_id": "SRC-1",
                },
            },
        )
    })()

    assert await router.submit_outbound(intent, principal="owner@s.whatsapp.net") is True
    effect = store.get_lineage("m-forward").effects[0]
    stored = store.get_effect(effect.effect_id)
    assert stored is not None
    assert stored.capability == "forward_message"
    assert stored.payload == ForwardPayload("source@g.us", "SRC-1")
    store.close()
