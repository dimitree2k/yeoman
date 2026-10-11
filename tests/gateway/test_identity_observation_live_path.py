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

import pytest
from history_turn_fixtures import Projector
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


def _seed_identity(projector: Projector) -> None:
    projector.db.execute(
        "INSERT INTO contacts VALUES ('synthetic-person','person',NULL,'Synthetic Sender','confirmed',NULL,'[]')"
    )
    for kind, value in (("pn_jid", PHONE_JID), ("lid", LID)):
        projector.db.execute(
            "INSERT INTO identifier_history(contact_id,channel,kind,value,strength,evidence,source_refs) "
            "VALUES ('synthetic-person','whatsapp',?,?,'strong','native_pair','[]')",
            (kind, value),
        )
    projector.db.commit()


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
async def test_live_history_does_not_invent_a_person_for_an_unknown_sender() -> None:
    from yeoman_gateway.history.context import history_turn

    projector = Projector()
    async with history_turn(projector):
        ctx = await _resolve(ContactsMiddleware(), await _bus_event(_whatsapp_event("SYN-MSG-1")))
    assert "contact_id" not in ctx.event.raw_metadata
    projector.close()


@pytest.mark.asyncio
async def test_known_sender_resolves_from_the_current_history_snapshot() -> None:
    from yeoman_gateway.history.context import history_turn

    projector = Projector()
    _seed_identity(projector)
    layer = ContactsMiddleware()
    async with history_turn(projector):
        first = await _resolve(layer, await _bus_event(_whatsapp_event("SYN-MSG-1")))
        second = await _resolve(layer, await _bus_event(_whatsapp_event("SYN-MSG-2")))
    assert first.event.raw_metadata.get("contact_id") == "synthetic-person"
    assert second.event.raw_metadata.get("contact_id") == "synthetic-person"
    assert second.event.raw_metadata.get("identity_reason") == "history"
    projector.close()


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
    from yeoman_gateway.history.context import history_turn

    records: list[dict] = []
    sink = logger.add(lambda message: records.append(message.record), level="DEBUG")
    try:
        event = await _bus_event(_whatsapp_event("SYN-MSG-1"))
        projector = Projector()
        async with history_turn(projector):
            ctx = await _resolve(ContactsMiddleware(knowledge=_FailingKnowledge()), event)
        projector.close()
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
