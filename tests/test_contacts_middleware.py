"""Tests for ContactsMiddleware."""

import pytest
from dataclasses import replace
from pathlib import Path

from yeoman.contacts.service import ContactsService
from yeoman.core.models import ArchivedMessage, InboundEvent
from yeoman.core.pipeline import PipelineContext
from yeoman.pipeline.contacts import ContactsMiddleware
from yeoman.pipeline.reply_context import ReplyContextMiddleware


def _make_event(**overrides: object) -> InboundEvent:
    defaults = {
        "channel": "whatsapp",
        "chat_id": "test-chat@g.us",
        "sender_id": "491521234567@s.whatsapp.net",
        "content": "hello",
        "message_id": "msg-001",
        "raw_metadata": {"sender_name": "Alex"},
    }
    defaults.update(overrides)
    return InboundEvent(**defaults)


@pytest.fixture
def contacts(tmp_path: Path) -> ContactsService:
    return ContactsService(db_path=tmp_path / "contacts.db")


class TestContactsMiddleware:
    @pytest.mark.asyncio
    async def test_creates_stub_for_new_jid(self, contacts: ContactsService) -> None:
        mw = ContactsMiddleware(contacts=contacts)
        ctx = PipelineContext(event=_make_event())
        called = False
        async def next_fn(c: PipelineContext) -> None:
            nonlocal called
            called = True
        await mw(ctx, next_fn)
        assert called
        assert "contact_id" in ctx.event.raw_metadata
        assert "491521234567@s.whatsapp.net" in contacts.known_jids

    @pytest.mark.asyncio
    async def test_reuses_existing_contact(self, contacts: ContactsService) -> None:
        contacts.ensure_contact(
            channel="whatsapp", identifier="491521234567@s.whatsapp.net",
            kind="phone_jid", push_name="Alex",
        )
        mw = ContactsMiddleware(contacts=contacts)
        ctx = PipelineContext(event=_make_event())
        async def next_fn(c: PipelineContext) -> None: pass
        await mw(ctx, next_fn)
        assert ctx.event.raw_metadata["contact_id"] == contacts.known_jids["491521234567@s.whatsapp.net"]

    @pytest.mark.asyncio
    async def test_uses_sender_id_when_no_participant(self, contacts: ContactsService) -> None:
        mw = ContactsMiddleware(contacts=contacts)
        event = _make_event(participant=None)
        ctx = PipelineContext(event=event)
        async def next_fn(c: PipelineContext) -> None: pass
        await mw(ctx, next_fn)
        assert "491521234567@s.whatsapp.net" in contacts.known_jids

    @pytest.mark.asyncio
    async def test_skips_non_chat_channels(self, contacts: ContactsService) -> None:
        mw = ContactsMiddleware(contacts=contacts)
        ctx = PipelineContext(event=_make_event(channel="system"))
        async def next_fn(c: PipelineContext) -> None: pass
        await mw(ctx, next_fn)
        assert "contact_id" not in ctx.event.raw_metadata

    @pytest.mark.asyncio
    async def test_infers_kind_for_whatsapp(self, contacts: ContactsService) -> None:
        mw = ContactsMiddleware(contacts=contacts)
        event = _make_event(sender_id="140960843485342@lid")
        ctx = PipelineContext(event=event)
        async def next_fn(c: PipelineContext) -> None: pass
        await mw(ctx, next_fn)
        idents = contacts.store.get_identifiers(
            contacts.known_jids["140960843485342@lid"]
        )
        assert idents[0].kind == "lid"


class _FakeArchive:
    def __init__(self, messages: list[ArchivedMessage] | None = None) -> None:
        self._messages = messages or []

    def record_inbound(self, event: InboundEvent) -> None: pass
    def lookup_message(self, channel, chat_id, message_id): return None
    def lookup_message_any_chat(self, channel, message_id, preferred_chat_id=None): return None
    def lookup_messages_before(self, channel, chat_id, anchor_id, limit=8):
        return self._messages


class TestSpeakerLabelResolution:
    @pytest.mark.asyncio
    async def test_ambient_window_uses_contact_display_name(
        self, contacts: ContactsService,
    ) -> None:
        cid = contacts.ensure_contact(
            channel="whatsapp", identifier="jid1@s.whatsapp.net",
            kind="phone_jid", push_name="Pikachu123",
        )
        contacts.update_display_name(cid, "Alex")

        archived = ArchivedMessage(
            channel="whatsapp", chat_id="test-chat@g.us",
            message_id="old-msg-001", text="hey there",
            sender_id="jid1@s.whatsapp.net",
            participant=None, timestamp=None,
            created_at="2026-03-10T12:00:00Z",
        )
        archive = _FakeArchive(messages=[archived])
        mw = ReplyContextMiddleware(archive=archive, contacts=contacts)
        event = _make_event(message_id="msg-002")
        ctx = PipelineContext(event=event)

        async def next_fn(c: PipelineContext) -> None: pass
        await mw(ctx, next_fn)

        ambient = ctx.event.raw_metadata.get("ambient_context_window", [])
        assert len(ambient) == 1
        assert ambient[0].startswith("[Alex]")  # display_name, not pushName
