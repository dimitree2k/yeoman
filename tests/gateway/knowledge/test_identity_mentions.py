"""Deterministic mention candidates stay inside verified chat identity scope."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from yeoman_gateway.core.models import InboundEvent
from yeoman_gateway.core.pipeline import PipelineContext
from yeoman_gateway.knowledge.models import (
    Identifier,
    PersonResolution,
    TrustedIdentityObservation,
)
from yeoman_gateway.pipeline.contacts import ContactsMiddleware

from tests.gateway.history_test_helpers import ProjectedHistory


def _projected_mention_context(event, queries, knowledge):
    from yeoman_gateway.knowledge.models import TrustedReadContext
    from yeoman_gateway.policy.identity import canonical_user_id

    account_id = str(event.raw_metadata.get("account_id") or "").strip()
    if event.channel != "whatsapp" or not account_id:
        return None
    principal = canonical_user_id(event.channel, event.sender_id, event.raw_metadata)
    now_ms = int(event.timestamp.timestamp() * 1000)
    roster = queries.members(chat_id=event.chat_id, at_ms=now_ms)
    if roster.status != "known" or not principal or principal not in roster.members:
        return None
    return TrustedReadContext(
        principal_id=principal, channel="whatsapp", chat_id=event.chat_id,
        recipient_principals=roster.members,
        membership_revision=f"history:{queries.snapshot.generation}",
        policy_revision=knowledge.policy_revision, purpose="reply", now_ms=now_ms,
        is_direct=not event.is_group, owner=False,
    )

ACCOUNT = "synthetic-whatsapp-account"
OWNER = "whatsapp:4910000000001"


def _observe_pair(h, phone: str, lid: str, *, name: str) -> PersonResolution:
    observation = TrustedIdentityObservation(
        identifiers=(
            Identifier("whatsapp", "phone_jid", phone, namespace=ACCOUNT),
            Identifier("whatsapp", "lid", lid, namespace=ACCOUNT),
        ),
        evidence_ref=f"mention-observation:{phone}",
        observed_name=name,
        observed_at_ms=h.clock.now_ms(),
        mapping_verified=True,
        account_namespace=ACCOUNT,
    )
    h.authority.issue_observation(observation)
    return h.service.resolve_person(observation)


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
    member_id = h.person_for("whatsapp:4910000000043", "Niko")
    context = _mention_context(h, "whatsapp:4910000000043")
    h.authority.issue_evidence_ref("mention-lid-binding")
    known = Identifier("whatsapp", "lid", lid, namespace=ACCOUNT)
    h.service.add_or_end_binding(
        person_id=member_id,
        identifier=known,
        evidence_ref="mention-lid-binding",
        mapping_verified=True,
        context=h.admin_context(),
        valid_from_ms=h.clock.now_ms(),
    )
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


@pytest.mark.parametrize("account_id", [ACCOUNT, "default"])
async def test_runtime_context_rechecks_projected_members_and_qualified_principal(
    tmp_path, account_id
):
    from dataclasses import replace

    from yeoman_gateway.history.queries import HistoryQueries
    from yeoman_gateway.knowledge.api import open_knowledge_store
    from yeoman_gateway.knowledge.authority import (
        FakePolicyAuthority,
        FakeSourceAuthority,
        PolicyMembership,
    )
    from yeoman_gateway.policy.identity import registry_member_principals

    sender = "whatsapp:4910000000050"
    member = "whatsapp:4910000000051"

    history = ProjectedHistory(tmp_path / f"history-{account_id}")
    history.add_group(
        chat_id="synthetic@g.us",
        participants=[
            {"lid": "8420000050@lid", "phoneJid": "4910000000050@s.whatsapp.net"},
            {"lid": "8420000051@lid", "phoneJid": "4910000000051@s.whatsapp.net"},
            {"lid": "8420000052@lid", "phoneJid": "4910000000052@s.whatsapp.net"},
            "8420000054@lid",
        ],
        timestamp=1_700_000_000_000,
    )
    membership_view = {"value": PolicyMembership(frozenset(), "history:1")}

    class _QueryPolicy(FakePolicyAuthority):
        def membership(self, _context):
            return membership_view["value"]

    authority = FakeSourceAuthority()
    policy = _QueryPolicy(admins={OWNER}, capture_actors={OWNER})
    service = open_knowledge_store(
        tmp_path / "runtime-knowledge.db",
        workspace_id="synthetic-mention-workspace",
        source_authority=authority,
        policy_authority=policy,
    )
    try:
        phone = "4910000000051@s.whatsapp.net"
        lid = "8420000051@lid"
        member_resolution = _observe_pair_for_service(
            service, authority, phone, lid, name="Runtime Member", account=account_id
        )
        sender_phone = "4910000000050@s.whatsapp.net"
        _observe_pair_for_service(
            service,
            authority,
            sender_phone,
            "8420000050@lid",
            name="Runtime Sender",
            account=account_id,
        )
        event = InboundEvent(
            channel="whatsapp",
            chat_id="synthetic@g.us",
            sender_id="4910000000050@s.whatsapp.net",
            participant="4910000000050@s.whatsapp.net",
            content="hello",
            is_group=True,
            timestamp=datetime.fromtimestamp(1_700_000_001, UTC),
            raw_metadata={
                "account_id": account_id,
                "sender_phone_jid": sender_phone,
                "message_id": "synthetic-runtime-message",
                "mentioned_jids": [lid],
                "statement_roles": [{"role": "speaker", "principal": sender}],
                "source_ref": {"event_id": "synthetic-runtime-event"},
                "source_audience": {"status": "known", "members": [sender, member]},
            },
        )
        snapshot = history.snapshot()
        queries = HistoryQueries(snapshot)
        roster = queries.members(chat_id=event.chat_id, at_ms=1_700_000_001_000)
        membership_view["value"] = PolicyMembership(roster.members, "history:1")
        context = _projected_mention_context(event, queries, service)
        assert context is not None
        assert context.principal_id == sender
        expected_members = frozenset({sender, member, "whatsapp:4910000000052", "whatsapp:8420000054@lid"})
        assert context.recipient_principals == expected_members
        assert context.membership_revision
        assert set(roster.members) == set(expected_members)
        membership = policy.membership(context)
        assert membership is not None
        assert membership.members == expected_members
        assert registry_member_principals(
            "whatsapp", [{"id": "8420000052@lid", "phoneNumber": "4910000000052@s.whatsapp.net",
                          "phoneJid": "4910000000053@s.whatsapp.net"}]
        ) == frozenset({"8420000052@lid"})
        assert registry_member_principals(
            "telegram",
            [{"id": "telegram:synthetic-user", "phoneNumber": sender_phone}],
        ) == frozenset({"telegram:synthetic-user"})
        assert registry_member_principals("whatsapp", [{"id": "whatsapp:4910000000055"}]) == frozenset({"whatsapp:4910000000055"})
        missing_account_event = replace(
            event,
            raw_metadata={
                key: value
                for key, value in event.raw_metadata.items()
                if key != "account_id"
            },
        )
        assert _projected_mention_context(missing_account_event, queries, service) is None
        assert member_resolution.person_id is not None

        pipeline_context = PipelineContext(event=event)
        reached_next = False

        async def next_middleware(_ctx):
            nonlocal reached_next
            reached_next = True

        middleware = ContactsMiddleware(
            knowledge=service,
            observation_issuer=authority.issue_observation,
            mention_context_factory=lambda _current: context,
        )
        await middleware(pipeline_context, next_middleware)
        assert reached_next
        runtime_candidate = pipeline_context.event.raw_metadata[
            "mentioned_person_candidates"
        ][0]
        assert runtime_candidate["person_id"] == member_resolution.person_id
        assert runtime_candidate["status"] == "ambiguous"
        assert pipeline_context.event.raw_metadata["mentioned_jids"] == [lid]
        for metadata_key in ("statement_roles", "source_ref", "source_audience"):
            assert pipeline_context.event.raw_metadata[metadata_key] == event.raw_metadata[
                metadata_key
            ]

        result = service.resolve_mentions(
            (Identifier("whatsapp", "lid", lid, namespace=account_id),),
            at_ms=None,
            context=context,
            account_namespace=account_id,
        )[0]
        mismatch = service.resolve_mentions(
            (Identifier("whatsapp", "lid", lid, namespace=account_id),),
            context=context,
            account_namespace=ACCOUNT if account_id == "default" else "other-account",
        )[0]
        assert mismatch.status == "unresolved"
        assert mismatch.reason == "account_namespace_mismatch"
        history.add_group(
            chat_id="synthetic@g.us",
            participants=[{"lid": "8420000050@lid", "phoneJid": "4910000000050@s.whatsapp.net"}],
            timestamp=1_700_000_000_500,
        )
        next_snapshot = history.snapshot()
        new_roster = HistoryQueries(next_snapshot).members(
            chat_id=event.chat_id, at_ms=1_700_000_001_000
        )
        membership_view["value"] = PolicyMembership(new_roster.members, "history:2")
        after_membership_change = service.resolve_mentions(
            (Identifier("whatsapp", "lid", lid, namespace=ACCOUNT),),
            at_ms=None,
            context=context,
        )[0]

        unqualified_event = replace(
            event,
            timestamp=datetime.fromtimestamp(1_700_000_003, UTC),
            raw_metadata={"account_id": account_id},
        )
        unqualified_history = ProjectedHistory(tmp_path / f"unqualified-{account_id}")
        unqualified_history.add_group(
            chat_id="synthetic@g.us", participants=["4910000000050"], timestamp=1_700_000_002_000,
        )
        unqualified_snapshot = unqualified_history.snapshot()
        unqualified = _projected_mention_context(
            unqualified_event, HistoryQueries(unqualified_snapshot), service
        )
        assert result.status == "ambiguous"
        assert result.person_id == member_resolution.person_id
        assert after_membership_change.status == "denied"
        assert after_membership_change.person_id is None
        assert unqualified is None
        snapshot.close()
        next_snapshot.close()
        unqualified_snapshot.close()
        unqualified_history.close()
    finally:
        service.close()
        history.close()


def _observe_pair_for_service(
    service, authority, phone: str, lid: str, *, name: str, account: str = ACCOUNT
):
    observation = TrustedIdentityObservation(
        identifiers=(
            Identifier("whatsapp", "phone_jid", phone, namespace=account),
            Identifier("whatsapp", "lid", lid, namespace=account),
        ),
        evidence_ref=f"mention-observation:{phone}",
        observed_name=name,
        observed_at_ms=1_700_000_000_000,
        mapping_verified=True,
        account_namespace=account,
    )
    authority.issue_observation(observation)
    return service.resolve_person(observation)
