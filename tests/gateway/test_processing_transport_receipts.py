"""Plan 04 / R07: the provider receipt the bridge reports is captured, not discarded."""

from __future__ import annotations

from pathlib import Path

import pytest
from yeoman_gateway.processing.effects import EffectGateway
from yeoman_gateway.processing.models import (
    EffectEnvelope,
    EffectTarget,
    PolicySnapshot,
    ProcessingError,
    TextPayload,
)
from yeoman_gateway.processing.policy import SnapshotEffectAuthorizer
from yeoman_gateway.processing.store import ProcessingStore

CHAT = "chat@g.us"
T0 = 1_700_000_000_000


class _Clock:
    def __init__(self, value: int = T0) -> None:
        self.value = value

    def __call__(self) -> int:
        return self.value


class _Snapshots:
    def snapshot(self) -> PolicySnapshot:
        return PolicySnapshot(version="v1", policy_hash="h1", healthy=True)


class _AllowAll:
    def resolve(self, *, principal: str, target: EffectTarget, capability: str):
        return True, "allow"


class _Transport:
    """Stands in for a channel that now returns what the bridge replied."""

    def __init__(self, report: dict | None) -> None:
        self.report = report
        self.sent: list[str] = []

    async def send_now(self, message):
        self.sent.append(message.content)
        return self.report

    async def send_reaction_now(self, message):
        self.sent.append(message.emoji)
        return self.report


def _gateway(store: ProcessingStore, transport: _Transport, clock: _Clock) -> EffectGateway:
    from yeoman_gateway.processing.dispatch import BusEffectExecutor

    executor = BusEffectExecutor(
        bus=object(),
        direct_sender=transport.send_now,
        direct_reaction_sender=transport.send_reaction_now,
    )
    return EffectGateway(
        store,
        authorizer=SnapshotEffectAuthorizer(
            snapshots=_Snapshots(), capabilities=_AllowAll(), clock=clock
        ),
        executor=executor,
        clock=clock,
    )


def _submit(gateway: EffectGateway) -> str:
    receipt = gateway.submit(
        EffectEnvelope(
            effect_id="fx1",
            operation_key="k1",
            payload=TextPayload(text="hi"),
            target=EffectTarget(channel="whatsapp", chat_id=CHAT),
            capability="send_text",
        )
    )
    return receipt.effect_id


@pytest.mark.asyncio
async def test_provider_message_id_is_recorded_as_a_receipt(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "p.db")
    transport = _Transport({"provider_message_id": "3EB0ABC"})
    gateway = _gateway(store, transport, _Clock())
    _submit(gateway)

    result = await gateway.execute_ready("fx1")

    assert result.state == "sent"
    assert store.effect_state("fx1") == "sent"
    receipts = store.transport_receipts("fx1")
    assert len(receipts) == 1
    newest = store.effect_transport_receipt("fx1")
    assert newest is not None and newest.provider_message_id == "3EB0ABC"
    assert newest.channel == "whatsapp" and newest.chat_id == CHAT
    assert store.effects_by_provider_message("whatsapp", CHAT, "3EB0ABC") == ("fx1",)
    # Still exactly one transport attempt for an effect delivery.
    assert transport.sent == ["hi"]
    store.close()


@pytest.mark.asyncio
async def test_adapter_without_a_provider_id_records_nothing(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "p.db")
    transport = _Transport({})
    gateway = _gateway(store, transport, _Clock())
    _submit(gateway)

    result = await gateway.execute_ready("fx1")

    assert result.state == "sent"  # local acceptance is still a success
    assert store.transport_receipts("fx1") == ()
    assert store.effect_transport_receipt("fx1") is None
    store.close()


@pytest.mark.asyncio
async def test_a_second_execution_does_not_add_a_second_receipt(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "p.db")
    transport = _Transport({"provider_message_id": "3EB0ABC"})
    gateway = _gateway(store, transport, _Clock())
    _submit(gateway)
    await gateway.execute_ready("fx1")

    await gateway.execute_ready("fx1")

    assert len(store.transport_receipts("fx1")) == 1
    assert transport.sent == ["hi"]
    store.close()


def test_receipt_for_an_unknown_effect_is_refused(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "p.db")
    with pytest.raises(ProcessingError):
        store.record_transport_receipt(
            "does-not-exist", channel="whatsapp", chat_id=CHAT, now_ms=T0
        )
    store.close()


def test_receipts_survive_a_reopen(tmp_path: Path) -> None:
    path = tmp_path / "p.db"
    store = ProcessingStore(path)
    store.enqueue_effect(
        effect_id="fx1", operation_key="k1", payload={"text": "hi"},
        target={"channel": "whatsapp", "chat_id": CHAT}, now_ms=T0,
    )
    store.record_transport_receipt(
        "fx1", channel="whatsapp", chat_id=CHAT, provider_message_id="3EB0ABC", now_ms=T0
    )
    store.close()

    store = ProcessingStore(path)
    assert store.schema_version == 3
    receipt = store.effect_transport_receipt("fx1")
    assert receipt is not None and receipt.provider_message_id == "3EB0ABC"
    store.close()
