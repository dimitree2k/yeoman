"""Tests for contacts-based mention resolution in outbound pipeline."""

import pytest
from pathlib import Path

from yeoman.contacts.service import ContactsService
from yeoman.core.models import InboundEvent
from yeoman.core.pipeline import PipelineContext
from yeoman.core.intents import SendOutboundIntent
from yeoman.pipeline.outbound import OutboundMiddleware


def _make_event(**overrides: object) -> InboundEvent:
    defaults = {
        "channel": "whatsapp",
        "chat_id": "group@g.us",
        "sender_id": "sender@s.whatsapp.net",
        "content": "hello",
        "message_id": "msg-001",
        "is_group": True,
    }
    defaults.update(overrides)
    return InboundEvent(**defaults)


@pytest.fixture
def contacts(tmp_path: Path) -> ContactsService:
    return ContactsService(db_path=tmp_path / "contacts.db")


class TestContactsMentionResolution:
    @pytest.mark.asyncio
    async def test_resolves_name_mention_via_contacts(
        self, contacts: ContactsService,
    ) -> None:
        contacts.ensure_contact(
            channel="whatsapp", identifier="491521234567@s.whatsapp.net",
            kind="phone_jid", push_name="Alex",
        )
        mw = OutboundMiddleware(contacts=contacts)
        ctx = PipelineContext(event=_make_event())
        ctx.reply = "Hey @Alex check this out"
        ctx.decision = type("D", (), {
            "when_to_reply_mode": "all",
            "voice_output_mode": "text",
        })()

        async def next_fn(c: PipelineContext) -> None: pass
        await mw(ctx, next_fn)

        send_intents = [i for i in ctx.intents if isinstance(i, SendOutboundIntent)]
        assert len(send_intents) == 1
        metadata = send_intents[0].event.metadata
        assert "mention_candidates" in metadata
        candidates = metadata["mention_candidates"]
        assert "491521234567@s.whatsapp.net" in candidates
