"""Regression: the live composition must actually create and refresh person identities.

Since the knowledge cutover the contacts middleware resolves the sender through
``knowledge.resolve_observation``.  The production source authority only accepts an
observation that was issued to it first, and nothing in the live composition issued one,
so every inbound message failed verification and the middleware swallowed the error: no
stub, no binding refresh, no observed name, and no log line.

These tests drive the real composition root on a temporary ``YEOMAN_HOME`` and feed it
metadata shaped exactly like the WhatsApp channel's bus message.  All identifiers are
synthetic.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from loguru import logger
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
    config = Config.model_validate(
        {
            "knowledge": {
                "enabled": True,
                "db_path": str(tmp_path / "knowledge.db"),
                "legacy_contacts_paths": [str(tmp_path / "legacy-contacts.db")],
                "legacy_memory_paths": [str(tmp_path / "legacy-memory.db")],
                "capture_enabled": False,
            },
            "personaEvolution": {"enabled": False},
            "processing": {"enabled": False, "participation": {"enabled": False}},
            "security": {"enabled": False},
        }
    )
    # Hermeticity: an unchecked field name would fall back to the real default paths.
    assert Path(config.knowledge.db_path).expanduser() == tmp_path / "knowledge.db"

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
        built.inbound_archive.close()
        built.chat_registry.close()
        built.contacts.close()
        built.memory.close()


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


def _identifier(kind: str, value: str) -> Identifier:
    return Identifier(channel="whatsapp", kind=kind, value=value, namespace=BRIDGE_ACCOUNT)


@pytest.mark.asyncio
async def test_the_live_composition_creates_a_person_for_an_unknown_sender(runtime) -> None:
    layer = _contacts_layer(runtime)
    knowledge = runtime.responder.knowledge

    ctx = await _resolve(layer, await _bus_event(_whatsapp_event("SYN-MSG-1")))

    person_id = ctx.event.raw_metadata.get("contact_id")
    assert person_id, "a proven, unknown sender must get a person"
    for kind, value in (("phone_jid", PHONE_JID), ("lid", LID)):
        resolution = knowledge.resolve_identifier(_identifier(kind, value))
        assert resolution.person_id == person_id, f"{kind} must be bound in the bridge account"


@pytest.mark.asyncio
async def test_a_known_sender_resolves_to_the_same_person_on_the_next_message(runtime) -> None:
    layer = _contacts_layer(runtime)

    first = await _resolve(layer, await _bus_event(_whatsapp_event("SYN-MSG-1")))
    second = await _resolve(layer, await _bus_event(_whatsapp_event("SYN-MSG-2")))

    assert first.event.raw_metadata.get("contact_id")
    assert second.event.raw_metadata.get("contact_id") == first.event.raw_metadata["contact_id"]
    assert second.event.raw_metadata.get("identity_reason") == "existing_binding"


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


class _FailingKnowledge:
    def resolve_observation(self, _observation: object) -> object:
        raise KnowledgeError("unauthorized", f"secret detail {PHONE_JID}")


@pytest.mark.asyncio
async def test_a_failed_resolution_is_logged_with_a_reason_code_and_no_pii() -> None:
    records: list[dict] = []
    sink = logger.add(lambda message: records.append(message.record), level="DEBUG")
    try:
        event = await _bus_event(_whatsapp_event("SYN-MSG-1"))
        ctx = await _resolve(ContactsMiddleware(knowledge=_FailingKnowledge()), event)
    finally:
        logger.remove(sink)

    assert "contact_id" not in ctx.event.raw_metadata
    warnings = [record for record in records if record["level"].name == "WARNING"]
    assert len(warnings) == 1
    text = warnings[0]["message"]
    assert "identity_resolution_failed" in text
    assert "reason=unauthorized" in text
    for secret in (PHONE, LID, CHAT, "Synthetic Sender", "SYN-MSG-1", "secret detail"):
        assert secret not in text
