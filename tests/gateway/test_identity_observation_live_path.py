"""Live WhatsApp sender identity resolves from projected native history."""

from __future__ import annotations

from pathlib import Path

import pytest
from yeoman_gateway.app.bootstrap import _inbound_message_to_event
from yeoman_gateway.bus.queue import MessageBus
from yeoman_gateway.channels.whatsapp import InboundEvent as WhatsAppInboundEvent
from yeoman_gateway.channels.whatsapp import WhatsAppChannel
from yeoman_gateway.core.models import InboundEvent
from yeoman_gateway.core.pipeline import PipelineContext
from yeoman_gateway.knowledge.models import (
    DEFAULT_NAMESPACE,
    Identifier,
    KnowledgeError,
    TrustedIdentityObservation,
)
from yeoman_gateway.knowledge.runtime import RuntimeKnowledgeSources
from yeoman_gateway.pipeline.contacts import ContactsMiddleware
from yeoman_shared.config.schema import WhatsAppConfig

PHONE = "491520000001"
PHONE_JID = f"{PHONE}@s.whatsapp.net"
LID = "100000000000001@lid"
CHAT = "synthetic-group@g.us"
#: What the bridge reports as its account today; the migrated bindings carry it too.
BRIDGE_ACCOUNT = DEFAULT_NAMESPACE
OBSERVED_AT_MS = 1_790_000_000_000


@pytest.fixture
def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from yeoman_gateway.app.bootstrap import build_gateway_runtime
    from yeoman_gateway.policy.engine import PolicyEngine
    from yeoman_gateway.policy.schema import PolicyConfig
    from yeoman_shared.config.schema import Config

    class _NeverProvider:
        async def chat(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("building the runtime must not call a model")

        def get_default_model(self) -> str:
            return "test/provider"

    monkeypatch.setenv("YEOMAN_HOME", str(tmp_path))
    from tests.gateway.history_test_helpers import prepare_history_retirement_knowledge

    knowledge_path = prepare_history_retirement_knowledge(tmp_path)
    config = Config.model_validate(
        {
            "knowledge": {
                "enabled": True,
                "db_path": str(knowledge_path),
                "capture_enabled": False,
                "legacy_contacts_paths": [str(tmp_path / "legacy-contacts.db")],
                "legacy_memory_paths": [str(tmp_path / "legacy-memory.db")],
            },
            "personaEvolution": {"enabled": False},
            "processing": {"enabled": False, "participation": {"enabled": False}},
            "security": {"enabled": False},
        }
    )
    # Hermeticity: an unchecked field name would fall back to the real default paths.
    assert Path(config.knowledge.db_path).expanduser() == knowledge_path

    built = build_gateway_runtime(
        config=config,
        provider=_NeverProvider(),  # type: ignore[arg-type]
        policy_engine=PolicyEngine(PolicyConfig(), workspace=tmp_path),
        policy_path=None,
        workspace=tmp_path / "workspace",
        bus=MessageBus(),
    )
    try:
        yield built
    finally:
        # These are the owned resources in the history-native runtime. The former
        # archive/registry/contacts/memory fields no longer exist on GatewayRuntime.
        built.responder.knowledge.close()
        if built.processing is not None:
            built.processing.close()


def _contacts_layer(runtime) -> ContactsMiddleware:
    layers = runtime.orchestrator._orchestrator._pipeline._layers  # noqa: SLF001 - wiring
    found = [layer for layer in layers if isinstance(layer, ContactsMiddleware)]
    assert len(found) == 1, "the knowledge composition must wire identity resolution"
    return found[0]


def _whatsapp_event(message_id: str, *, with_lid: bool = True) -> WhatsAppInboundEvent:
    return WhatsAppInboundEvent(
        message_id=message_id,
        chat_jid=CHAT,
        participant_jid=LID if with_lid else PHONE_JID,
        sender_id=LID if with_lid else PHONE_JID,
        sender_phone_jid=PHONE_JID,
        is_group=True,
        text="synthetic text",
        timestamp=OBSERVED_AT_MS,
        mentioned_jids=[],
        mentioned_bot=False,
        reply_to_bot=False,
        reply_to_message_id=None,
        reply_to_participant=None,
        reply_to_text=None,
        media_kind=None,
        media_type=None,
        media_file_name=None,
        media_path=None,
        media_bytes=None,
        media_description=None,
        voice_transcript=None,
        sender_name="Synthetic Sender",
    )


async def _bus_event(event: WhatsAppInboundEvent) -> InboundEvent:
    """What the orchestrator receives for one channel event: channel -> bus -> core."""
    bus = MessageBus()
    channel = WhatsAppChannel(WhatsAppConfig(), bus)
    # The bridge frame handler records the account before any message is published.
    channel._processing_account_id = BRIDGE_ACCOUNT  # noqa: SLF001 - bridge frame state
    await channel._publish_event(event)  # noqa: SLF001 - the classic publish path
    return _inbound_message_to_event(await bus.consume_inbound())


async def _resolve(layer: ContactsMiddleware, event: InboundEvent) -> PipelineContext:
    ctx = PipelineContext(event=event)
    called = False

    async def next_fn(_ctx: PipelineContext) -> None:
        nonlocal called
        called = True

    await layer(ctx, next_fn)
    assert called, "the middleware must always continue the pipeline"
    return ctx


async def _history_events(runtime, events, callback):
    from yeoman_gateway.history.context import history_knowledge_scope, history_turn
    from yeoman_gateway.history.project import project
    from yeoman_shared.raw_archive.writer import RawEvent

    archive = runtime.channels.raw_archive
    projector = runtime.history_projector
    for message_id in events:
        archive.append_durable(RawEvent(
            channel="whatsapp", kind="message", direction="in", received_ms=OBSERVED_AT_MS,
            native={"type": "message", "payload": {
                "chatJid": CHAT, "messageId": message_id, "participantJid": LID,
                "senderId": LID, "senderPhoneJid": PHONE_JID,
                "text": "synthetic text", "timestamp": OBSERVED_AT_MS // 1000,
            }},
        ))
    project([archive.root], projector.db_path, publish_lineage_root=archive.root)
    await projector.start()
    if projector._startup_task is not None:
        await projector._startup_task
    try:
        async with history_turn(projector) as snapshot:
            with history_knowledge_scope(snapshot, runtime.responder.knowledge):
                return await callback(snapshot)
    finally:
        await projector.stop()


def _identifier(kind: str, value: str) -> Identifier:
    return Identifier(channel="whatsapp", kind=kind, value=value, namespace=BRIDGE_ACCOUNT)


@pytest.mark.asyncio
async def test_the_live_composition_creates_a_person_for_an_unknown_sender(runtime) -> None:
    layer = _contacts_layer(runtime)
    async def check(snapshot):
        from yeoman_gateway.history.queries import HistoryQueries

        ctx = await _resolve(layer, await _bus_event(_whatsapp_event("SYN-MSG-1")))
        person_id = ctx.event.raw_metadata.get("contact_id")
        assert person_id, "a proven sender must resolve from the selected history"
        queries = HistoryQueries(snapshot)
        for value in (PHONE_JID, LID):
            assert queries.resolve_identifier(value, at_ms=OBSERVED_AT_MS, time_basis="native") == person_id
        assert ctx.event.raw_metadata.get("account_id") == BRIDGE_ACCOUNT
    await _history_events(runtime, ("SYN-MSG-1",), check)


@pytest.mark.asyncio
async def test_a_known_sender_resolves_to_the_same_person_on_the_next_message(runtime) -> None:
    layer = _contacts_layer(runtime)
    async def check(_snapshot):
        first = await _resolve(layer, await _bus_event(_whatsapp_event("SYN-MSG-1")))
        second = await _resolve(layer, await _bus_event(_whatsapp_event("SYN-MSG-2")))
        assert first.event.raw_metadata.get("contact_id")
        assert second.event.raw_metadata.get("contact_id") == first.event.raw_metadata["contact_id"]
        assert second.event.raw_metadata.get("identity_reason") == "history"
    await _history_events(runtime, ("SYN-MSG-1", "SYN-MSG-2"), check)


@pytest.mark.asyncio
async def test_the_bus_message_carries_the_bridge_account() -> None:
    """The namespace of every identifier is the bridge account, not the channel name."""
    event = await _bus_event(_whatsapp_event("SYN-MSG-1"))

    assert event.raw_metadata.get("account_id") == BRIDGE_ACCOUNT


@pytest.mark.asyncio
async def test_a_bus_message_without_a_recorded_account_carries_none() -> None:
    bus = MessageBus()
    channel = WhatsAppChannel(WhatsAppConfig(), bus)

    await channel._publish_event(_whatsapp_event("SYN-MSG-1"))  # noqa: SLF001

    message = await bus.consume_inbound()
    assert "account_id" not in message.metadata, "an unknown account is never invented"


def test_issued_observations_are_bounded() -> None:
    sources = RuntimeKnowledgeSources(max_observations=2)

    def observation(index: int) -> TrustedIdentityObservation:
        return TrustedIdentityObservation(
            identifiers=(_identifier("phone_jid", f"49152000000{index}@s.whatsapp.net"),),
            evidence_ref=f"observation:whatsapp:SYN-{index}",
        )

    for index in range(3):
        sources.observe(observation(index))

    with pytest.raises(KnowledgeError):
        sources.verify_observation(observation(0))
    assert sources.verify_observation(observation(2)) == "observation:whatsapp:SYN-2"


