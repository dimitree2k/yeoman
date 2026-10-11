"""Deterministic mention candidates stay inside verified chat identity scope."""

from __future__ import annotations

import pytest
from yeoman_gateway.core.models import InboundEvent
from yeoman_gateway.core.pipeline import PipelineContext
from yeoman_gateway.knowledge.models import (
    Identifier,
    PersonResolution,
)
from yeoman_gateway.pipeline.contacts import ContactsMiddleware

ACCOUNT = "whatsapp"
OWNER = "whatsapp:4910000000001"


def _observe_pair(h, phone: str, lid: str, *, name: str) -> PersonResolution:
    return h.observe(
        "whatsapp", "phone_jid", phone, name=name, mapping=True,
        extra=(Identifier("whatsapp", "lid", lid, namespace=ACCOUNT),),
    )


def _mention_context(h, *member_principals: str, chat: str = "mention-room"):
    return h.read_context(
        OWNER,
        chat=chat,
        recipients={OWNER, *member_principals},
    )


def test_known_lid_is_only_a_candidate_without_event_time(knowledge_harness):
    h = knowledge_harness
    phone = "4910000000042@s.whatsapp.net"
    lid = "8420000042@lid"
    member = _observe_pair(h, phone, lid, name="Mira")
    assert member.person_id is not None
    context = _mention_context(h, "whatsapp:4910000000042")

    result = h.service.resolve_mentions(
        (Identifier("whatsapp", "lid", lid, namespace=ACCOUNT),),
        context=context,
    )[0]

    assert result.status == "ambiguous"
    assert result.person_id == member.person_id
    assert result.reason == "mention_time_unknown"


def test_typed_mention_uses_historical_binding_and_never_creates_unknown_person(
    knowledge_harness,
):
    h = knowledge_harness
    lid = "8420000043@lid"
    member = _observe_pair(
        h, "4910000000043@s.whatsapp.net", lid, name="Niko"
    )
    member_id = member.person_id
    assert member_id is not None
    context = _mention_context(h, "whatsapp:4910000000043")
    known = Identifier("whatsapp", "lid", lid, namespace=ACCOUNT)
    before = h.service.stats(context=h.admin_context()).people_count

    resolved = h.service.resolve_mentions(
        (known,), at_ms=h.clock.now_ms(), context=context
    )[0]
    unknown = h.service.resolve_mentions(
        (Identifier("whatsapp", "lid", "8420000099@lid", namespace=ACCOUNT),),
        at_ms=h.clock.now_ms(),
        context=context,
    )[0]
    after = h.service.stats(context=h.admin_context()).people_count

    assert resolved.status == "resolved"
    assert resolved.person_id == member_id
    assert unknown.status == "unresolved"
    assert unknown.person_id is None
    assert after == before


def test_mention_namespace_and_membership_fail_closed(knowledge_harness):
    h = knowledge_harness
    phone = "4910000000044@s.whatsapp.net"
    lid = "8420000044@lid"
    member = _observe_pair(h, phone, lid, name="Tala")
    assert member.person_id is not None
    offered = _mention_context(h, "whatsapp:4910000000044")
    missing_account = h.service.resolve_mentions(
        (Identifier("whatsapp", "lid", lid),), at_ms=h.clock.now_ms(), context=offered
    )[0]
    outsider_context = _mention_context(h, "whatsapp:4910000000000", chat="other-room")
    outsider = h.service.resolve_mentions(
        (Identifier("whatsapp", "lid", lid, namespace=ACCOUNT),),
        at_ms=None,
        context=outsider_context,
    )[0]

    assert missing_account.status == "unresolved"
    assert missing_account.person_id is None
    assert missing_account.reason == "account_namespace_required"
    assert outsider.status == "unresolved"
    assert outsider.person_id is None
    assert outsider.reason == "mention_not_offered_member"


def test_explicit_name_candidates_are_ambiguous_and_offered_member_only(knowledge_harness):
    h = knowledge_harness
    members = (
        _observe_pair(
            h,
            "4910000000045@s.whatsapp.net",
            "8420000045@lid",
            name="Alex",
        ),
        _observe_pair(
            h,
            "4910000000046@s.whatsapp.net",
            "8420000046@lid",
            name="Alex",
        ),
    )
    outsider = _observe_pair(
        h,
        "4910000000047@s.whatsapp.net",
        "8420000047@lid",
        name="Alex",
    )
    member_ids = {item.person_id for item in members}
    assert None not in member_ids
    assert outsider.person_id not in member_ids
    context = _mention_context(h, "whatsapp:4910000000045", "whatsapp:4910000000046")

    candidates = h.service.search_mention_name_candidates("Alex", context=context)

    assert {item.person_id for item in candidates} == member_ids
    assert all(item.status == "ambiguous" for item in candidates)
    assert all(item.reason == "name_candidate_requires_confirmation" for item in candidates)


async def test_plaintext_alias_candidates_use_boundaries_and_current_members_only(
    knowledge_harness,
):
    h = knowledge_harness
    members = (
        _observe_pair(
            h,
            "4910000000101@s.whatsapp.net",
            "8420000101@lid",
            name="Alex",
        ),
        _observe_pair(
            h,
            "4910000000102@s.whatsapp.net",
            "8420000102@lid",
            name="Alex",
        ),
    )
    outsider = _observe_pair(
        h,
        "4910000000103@s.whatsapp.net",
        "8420000103@lid",
        name="Alex",
    )
    member_ids = {item.person_id for item in members}
    assert None not in member_ids
    context = _mention_context(h, "whatsapp:4910000000101", "whatsapp:4910000000102")
    middleware = ContactsMiddleware(
        knowledge=h.service,
        mention_context_factory=lambda _: context,
    )
    people_before = h.service.stats(context=h.admin_context()).people_count

    async def candidates_for(content: str):
        event = InboundEvent(
            channel="whatsapp",
            chat_id="mention-room",
            sender_id="untyped-sender",
            participant=None,
            content=content,
            raw_metadata={"account_id": ACCOUNT},
        )
        pipeline_context = PipelineContext(event=event)

        async def next_middleware(_ctx):
            return None

        await middleware(pipeline_context, next_middleware)
        return pipeline_context.event.raw_metadata.get("mentioned_person_candidates", [])

    mentioned = await candidates_for("Alex, what do you think?")
    substring_only = await candidates_for("Alexandra, what do you think?")
    people_after = h.service.stats(context=h.admin_context()).people_count

    assert {item["person_id"] for item in mentioned} == member_ids
    assert outsider.person_id not in {item["person_id"] for item in mentioned}
    assert all(item["source"] == "plaintext_alias" for item in mentioned)
    assert all(item["status"] == "ambiguous" for item in mentioned)
    assert substring_only == []
    assert people_after == people_before


async def test_contacts_middleware_attaches_mentions_separately_and_preserves_metadata(
    knowledge_harness,
):
    h = knowledge_harness
    phone = "4910000000048@s.whatsapp.net"
    lid = "8420000048@lid"
    member = _observe_pair(h, phone, lid, name="Ria")
    assert member.person_id is not None
    context = _mention_context(h, "whatsapp:4910000000048")
    original_metadata = {
        "account_id": ACCOUNT,
        "mentioned_jids": [lid],
        "mentioned_name_tokens": ["Ria"],
        "statement_roles": [{"role": "speaker", "principal": "synthetic:sender"}],
        "source_ref": {"event_id": "synthetic-event"},
        "source_audience": {"status": "known", "members": ["synthetic:reader"]},
    }
    event = InboundEvent(
        channel="whatsapp",
        chat_id="mention-room",
        sender_id="untyped-sender",
        participant=None,
        content="Ria, what do you think?",
        raw_metadata=original_metadata,
    )
    ctx = PipelineContext(event=event)
    passed = False

    async def next_middleware(next_ctx):
        nonlocal passed
        passed = True

    middleware = ContactsMiddleware(
        knowledge=h.service,
        mention_context_factory=lambda _: context,
    )
    await middleware(ctx, next_middleware)

    assert passed
    assert ctx.event.sender_id == event.sender_id
    assert ctx.event.content == event.content
    assert ctx.event.raw_metadata["statement_roles"] == original_metadata["statement_roles"]
    assert ctx.event.raw_metadata["source_ref"] == original_metadata["source_ref"]
    assert ctx.event.raw_metadata["source_audience"] == original_metadata["source_audience"]
    candidates = ctx.event.raw_metadata["mentioned_person_candidates"]
    assert [item["person_id"] for item in candidates] == [member.person_id, member.person_id]
    assert [item["source"] for item in candidates] == ["native_identifier", "explicit_name_token"]
    assert ctx.event.raw_metadata["mentioned_jids"] == [lid]


@pytest.mark.parametrize("account_id", [ACCOUNT, "default"])
def test_whatsapp_core_event_preserves_native_mention_jids(account_id):
    from yeoman_gateway.channels.whatsapp import InboundEvent as WhatsAppInboundEvent
    from yeoman_gateway.channels.whatsapp import WhatsAppChannel

    lid = "8420000049@lid"
    event = WhatsAppInboundEvent(
        message_id="synthetic-message",
        chat_jid="synthetic@g.us",
        participant_jid="8420000001@lid",
        sender_id="8420000001@lid",
        sender_phone_jid=None,
        is_group=True,
        text="hello",
        timestamp=1_700_000_000,
        mentioned_jids=[lid],
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
    )
    channel = object.__new__(WhatsAppChannel)
    channel._processing_account_id = account_id

    core_event = channel._to_core_event(event, "synthetic-message")

    assert core_event.raw_metadata["mentioned_jids"] == [lid]
    assert core_event.raw_metadata["account_id"] == account_id
