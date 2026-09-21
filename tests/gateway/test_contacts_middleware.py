"""Tests for ContactsMiddleware.

The middleware is a *bridge*: a channel adapter proves a platform mapping, the knowledge
facade decides the person.  These tests cover the bridge itself - which identifiers are
handed over, which are refused, and what happens when knowledge is degraded.  All values
are synthetic.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from yeoman_gateway.core.models import ArchivedMessage, InboundEvent
from yeoman_gateway.core.pipeline import PipelineContext
from yeoman_gateway.knowledge._contacts.service import ContactsService
from yeoman_gateway.pipeline.contacts import ContactsMiddleware, event_timestamp_ms
from yeoman_gateway.pipeline.reply_context import ReplyContextMiddleware

OBSERVED_AT_S = 1_700_000_000
OBSERVED_AT_MS = OBSERVED_AT_S * 1000


def _make_event(**overrides: object) -> InboundEvent:
    defaults = {
        "channel": "whatsapp",
        "chat_id": "test-chat@g.us",
        "sender_id": "491521234567",
        "content": "hello",
        "message_id": "msg-001",
        "raw_metadata": {
            "message_id": "msg-001",
            "sender_name": "Alex",
            "account_id": "account-synthetic-1",
            "sender_phone_jid": "491521234567@s.whatsapp.net",
            "timestamp": OBSERVED_AT_MS,
        },
    }
    defaults.update(overrides)
    return InboundEvent(**defaults)


@pytest.fixture
def contacts(tmp_path: Path) -> ContactsService:
    return ContactsService(db_path=tmp_path / "contacts.db")


@pytest.fixture
def knowledge(tmp_path: Path):
    """A synthetic knowledge facade on a temporary database."""
    from yeoman_gateway.knowledge.api import open_knowledge_store
    from yeoman_gateway.knowledge.authority import (
        EvidenceAudience,
        FakeClock,
        FakePolicyAuthority,
        FakeSourceAuthority,
    )

    authority = FakeSourceAuthority()
    clock = FakeClock()
    service = open_knowledge_store(
        tmp_path / "knowledge.db",
        workspace_id="contacts-middleware-tests",
        source_authority=authority,
        policy_authority=FakePolicyAuthority(
            admins={"whatsapp:4910000000001"}, capture_actors={"whatsapp:4910000000001"}
        ),
        clock=clock,
    )
    del EvidenceAudience  # imported for parity with the other suites; unused here

    class _IssuingService:
        """The facade plus a way to issue the evidence a channel adapter would prove."""

        def __init__(self, inner, authority) -> None:
            self._inner = inner
            self._authority = authority

        def issue_for(self, event: InboundEvent) -> None:
            """Register exactly the observation the middleware will build for ``event``."""
            observation = ContactsMiddleware(knowledge=None)._observation(  # noqa: SLF001
                event.channel, event.participant, event.sender_id, dict(event.raw_metadata)
            )
            if observation is None:
                # Nothing the adapter could prove (for example a bare number): the
                # middleware will not resolve anything either.
                return
            self._authority.issue_observation(observation)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    wrapper = _IssuingService(service, authority)
    try:
        yield wrapper
    finally:
        service.close()


async def _run(
    middleware: ContactsMiddleware, event: InboundEvent, *, issue: bool = True
) -> PipelineContext:
    if issue and middleware._knowledge is not None:  # noqa: SLF001 - adapter evidence seam
        issuer = getattr(middleware._knowledge, "issue_for", None)  # noqa: SLF001
        if callable(issuer):
            issuer(event)
    ctx = PipelineContext(event=event)
    called = False

    async def next_fn(c: PipelineContext) -> None:
        nonlocal called
        called = True

    await middleware(ctx, next_fn)
    assert called, "the middleware must always continue the pipeline"
    return ctx


class TestContactsMiddleware:
    @pytest.mark.asyncio
    async def test_a_proven_phone_jid_resolves_to_one_person(self, knowledge) -> None:
        mw = ContactsMiddleware(knowledge=knowledge)
        ctx = await _run(mw, _make_event())
        person_id = ctx.event.raw_metadata.get("contact_id")
        assert person_id
        assert ctx.event.raw_metadata["identity_reason"] == "created_stub_from_verified_identifier"
        # The binding is the active, proven one for exactly that identifier.
        from yeoman_gateway.knowledge.models import Identifier

        resolved = knowledge.resolve_identifier(
            Identifier(
                "whatsapp", "phone_jid", "491521234567@s.whatsapp.net", "account-synthetic-1"
            )
        )
        assert resolved.status == "resolved"
        assert resolved.person_id == person_id

    @pytest.mark.asyncio
    async def test_repeated_observation_reuses_the_same_person(self, knowledge) -> None:
        mw = ContactsMiddleware(knowledge=knowledge)
        first = await _run(mw, _make_event())
        second = await _run(mw, _make_event())
        assert first.event.raw_metadata["contact_id"] == second.event.raw_metadata["contact_id"]
        assert second.event.raw_metadata["identity_reason"] == "existing_binding"

    @pytest.mark.asyncio
    async def test_a_bare_number_is_not_promoted_to_a_phone_jid(self, knowledge) -> None:
        """The adapter did not issue a phone JID, so the middleware does not invent one."""
        mw = ContactsMiddleware(knowledge=knowledge)
        event = _make_event(
            sender_id="491521234567",
            raw_metadata={
                "message_id": "msg-002",
                "timestamp": OBSERVED_AT_MS,
                "account_id": "account-synthetic-1",
            },
        )
        ctx = await _run(mw, event)
        assert "contact_id" not in ctx.event.raw_metadata

    @pytest.mark.asyncio
    async def test_a_lid_alone_stays_a_lid(self, knowledge) -> None:
        mw = ContactsMiddleware(knowledge=knowledge)
        event = _make_event(
            sender_id="140960843485342@lid",
            raw_metadata={
                "message_id": "msg-003",
                "timestamp": OBSERVED_AT_MS,
                "account_id": "account-synthetic-1",
            },
        )
        ctx = await _run(mw, event)
        person_id = ctx.event.raw_metadata.get("contact_id")
        assert person_id
        from yeoman_gateway.knowledge.models import Identifier

        assert (
            knowledge.resolve_identifier(
                Identifier("whatsapp", "phone_jid", "140960843485342", "account-synthetic-1")
            ).status
            == "unresolved"
        )

    @pytest.mark.asyncio
    async def test_two_accounts_never_share_one_binding(self, knowledge) -> None:
        mw = ContactsMiddleware(knowledge=knowledge)
        first = await _run(mw, _make_event())
        other = await _run(
            mw,
            _make_event(
                raw_metadata={
                    "message_id": "msg-004",
                    "timestamp": OBSERVED_AT_MS,
                    "account_id": "account-synthetic-2",
                    "sender_phone_jid": "491521234567@s.whatsapp.net",
                }
            ),
        )
        # Same value, different platform account: two people, not one.
        assert other.event.raw_metadata.get("contact_id") != first.event.raw_metadata.get(
            "contact_id"
        )

    @pytest.mark.asyncio
    async def test_the_observation_time_comes_from_the_event_not_the_wall_clock(
        self, knowledge
    ) -> None:
        mw = ContactsMiddleware(knowledge=knowledge)
        await _run(mw, _make_event())
        row = knowledge._store.query_one(  # noqa: SLF001 - asserting the stored observation time
            "SELECT observed_at_ms FROM knowledge_identifier_bindings WHERE status = 'active'"
        )
        assert row is not None
        assert int(row["observed_at_ms"]) == OBSERVED_AT_MS

    @pytest.mark.asyncio
    async def test_knowledge_outage_attaches_no_person_and_does_not_fail(self, knowledge) -> None:
        """Degraded knowledge loses enrichment, never the turn and never a guess."""
        mw = ContactsMiddleware(knowledge=knowledge)
        knowledge.close()
        ctx = await _run(mw, _make_event())
        assert "contact_id" not in ctx.event.raw_metadata

    @pytest.mark.asyncio
    async def test_without_knowledge_the_middleware_is_a_no_op(self) -> None:
        mw = ContactsMiddleware(knowledge=None)
        ctx = await _run(mw, _make_event())
        assert "contact_id" not in ctx.event.raw_metadata

    @pytest.mark.asyncio
    async def test_non_identity_channels_are_skipped(self, knowledge) -> None:
        mw = ContactsMiddleware(knowledge=knowledge)
        ctx = await _run(mw, _make_event(channel="system"))
        assert "contact_id" not in ctx.event.raw_metadata

    @pytest.mark.asyncio
    async def test_a_provider_mapping_conflict_is_not_treated_as_verified(
        self, knowledge
    ) -> None:
        """A conflicted pair is never merged into one person on the adapter's word."""
        mw = ContactsMiddleware(knowledge=knowledge)
        event = _make_event(
            raw_metadata={
                "message_id": "msg-005",
                "timestamp": OBSERVED_AT_MS,
                "account_id": "account-synthetic-1",
                "sender_phone_jid": "491521234567@s.whatsapp.net",
                "participant_lid": "140960843485342@lid",
                "lid_conflict": True,
            }
        )
        ctx = await _run(mw, event)
        person_id = ctx.event.raw_metadata.get("contact_id")
        assert person_id
        # The LID stayed a separate, unproven candidate rather than joining the phone JID.
        from yeoman_gateway.knowledge.models import Identifier

        lid = knowledge.resolve_identifier(
            Identifier("whatsapp", "lid", "140960843485342@lid", "account-synthetic-1"),
            at_ms=OBSERVED_AT_MS,
        )
        assert lid.person_id != person_id


def test_event_timestamp_is_normalized_without_guessing() -> None:
    assert event_timestamp_ms({"timestamp": OBSERVED_AT_MS}) == OBSERVED_AT_MS
    assert event_timestamp_ms({"timestamp": OBSERVED_AT_S}) == OBSERVED_AT_MS
    assert event_timestamp_ms({"timestamp": datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC)}) == (
        OBSERVED_AT_MS
    )
    assert event_timestamp_ms({}) == 0
    assert event_timestamp_ms({"timestamp": True}) == 0


class _FakeArchive:
    def __init__(self, messages: list[ArchivedMessage] | None = None) -> None:
        self._messages = messages or []

    def record_inbound(self, event: InboundEvent) -> None:
        pass

    def lookup_message(self, channel, chat_id, message_id):
        return None

    def lookup_message_any_chat(self, channel, message_id, preferred_chat_id=None):
        return None

    def lookup_messages_before(self, channel, chat_id, anchor_id, limit=8):
        return self._messages


class TestSpeakerLabelResolution:
    @pytest.mark.asyncio
    async def test_ambient_window_uses_contact_display_name(
        self,
        contacts: ContactsService,
    ) -> None:
        cid = contacts.ensure_contact(
            channel="whatsapp",
            identifier="jid1@s.whatsapp.net",
            kind="phone_jid",
            push_name="Pikachu123",
        )
        contacts.update_display_name(cid, "Alex")

        archived = ArchivedMessage(
            channel="whatsapp",
            chat_id="test-chat@g.us",
            message_id="old-msg-001",
            text="hey there",
            sender_id="jid1@s.whatsapp.net",
            participant=None,
            timestamp=None,
            created_at="2026-03-10T12:00:00Z",
        )
        archive = _FakeArchive(messages=[archived])
        mw = ReplyContextMiddleware(archive=archive, contacts=contacts)
        event = _make_event(message_id="msg-002", is_group=True)
        ctx = PipelineContext(event=event)

        async def next_fn(c: PipelineContext) -> None:
            pass

        await mw(ctx, next_fn)

        ambient = ctx.event.raw_metadata.get("ambient_context_window", [])
        assert len(ambient) == 1
        assert ambient[0].startswith("[Alex]")  # display_name, not pushName
