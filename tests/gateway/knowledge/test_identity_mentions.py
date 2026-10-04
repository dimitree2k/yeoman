"""Deterministic mention candidates stay inside verified chat identity scope."""

from __future__ import annotations

from yeoman_gateway.core.models import InboundEvent
from yeoman_gateway.core.pipeline import PipelineContext
from yeoman_gateway.knowledge.models import (
    Identifier,
    PersonResolution,
    TrustedIdentityObservation,
)
from yeoman_gateway.pipeline.contacts import ContactsMiddleware

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


def test_whatsapp_core_event_preserves_native_mention_jids():
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
    channel._processing_account_id = ACCOUNT

    core_event = channel._to_core_event(event, "synthetic-message")

    assert core_event.raw_metadata["mentioned_jids"] == [lid]
    assert core_event.raw_metadata["account_id"] == ACCOUNT


async def test_runtime_context_rechecks_registry_members_and_qualified_principal(tmp_path):
    from dataclasses import replace
    from pathlib import Path

    from yeoman_gateway.knowledge.api import open_knowledge_store
    from yeoman_gateway.knowledge.authority import FakeSourceAuthority
    from yeoman_gateway.knowledge.runtime import RuntimeKnowledgePolicy
    from yeoman_gateway.pipeline.contacts import build_mention_read_context
    from yeoman_gateway.policy.identity import registry_member_principals
    from yeoman_gateway.storage.chat_registry import ChatRegistry

    sender = "whatsapp:4910000000050"
    member = "whatsapp:4910000000051"

    registry = ChatRegistry(Path(":memory:"))
    registry.sync_from_bridge_metadata(
        "whatsapp",
        [
            {
                "chatJid": "synthetic@g.us",
                "participants": [
                    {
                        "id": "8420000050@lid",
                        "phoneNumber": "4910000000050@s.whatsapp.net",
                    },
                    {
                        "id": "8420000051@lid",
                        "phoneNumber": "4910000000051@s.whatsapp.net",
                    },
                    {
                        "id": "8420000052@lid",
                        "phoneNumber": "4910000000052@s.whatsapp.net",
                        "phoneJid": "4910000000053@s.whatsapp.net",
                    },
                    {"id": "8420000054@lid"},
                    "whatsapp:4910000000055",
                ],
            }
        ],
    )
    authority = FakeSourceAuthority()
    policy = RuntimeKnowledgePolicy(engine=None, chat_registry=registry)
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
            service, authority, phone, lid, name="Runtime Member"
        )
        sender_phone = "4910000000050@s.whatsapp.net"
        _observe_pair_for_service(
            service,
            authority,
            sender_phone,
            "8420000050@lid",
            name="Runtime Sender",
        )
        event = InboundEvent(
            channel="whatsapp",
            chat_id="synthetic@g.us",
            sender_id="4910000000050",
            participant="4910000000050@s.whatsapp.net",
            content="hello",
            is_group=True,
            raw_metadata={
                "account_id": ACCOUNT,
                "sender_phone_jid": sender_phone,
                "message_id": "synthetic-runtime-message",
                "mentioned_jids": [lid],
                "statement_roles": [{"role": "speaker", "principal": sender}],
                "source_ref": {"event_id": "synthetic-runtime-event"},
                "source_audience": {"status": "known", "members": [sender, member]},
            },
        )
        context = build_mention_read_context(
            event, chat_registry=registry, knowledge=service
        )
        assert context is not None
        assert context.principal_id == sender
        expected_members = frozenset(
            {
                sender,
                member,
                "8420000052@lid",
                "8420000054@lid",
                "whatsapp:4910000000055",
            }
        )
        assert context.recipient_principals == expected_members
        assert context.membership_revision
        from yeoman_gateway.knowledge._memory.read_gate import registry_members

        current_registry_members = registry_members(
            registry, channel="whatsapp", chat_id="synthetic@g.us"
        )
        assert current_registry_members == set(expected_members)
        membership = policy.membership(context)
        assert membership is not None
        assert membership.members == expected_members
        assert "whatsapp:4910000000052" not in current_registry_members
        assert "whatsapp:4910000000053" not in current_registry_members
        assert registry_member_principals(
            "telegram",
            [{"id": "telegram:synthetic-user", "phoneNumber": sender_phone}],
        ) == frozenset({"telegram:synthetic-user"})
        missing_account_event = replace(
            event,
            raw_metadata={
                key: value
                for key, value in event.raw_metadata.items()
                if key != "account_id"
            },
        )
        assert (
            build_mention_read_context(
                missing_account_event, chat_registry=registry, knowledge=service
            )
            is None
        )
        assert member_resolution.person_id is not None

        pipeline_context = PipelineContext(event=event)
        reached_next = False

        async def next_middleware(_ctx):
            nonlocal reached_next
            reached_next = True

        middleware = ContactsMiddleware(
            knowledge=service,
            observation_issuer=authority.issue_observation,
            mention_context_factory=lambda current: build_mention_read_context(
                current, chat_registry=registry, knowledge=service
            ),
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
            (Identifier("whatsapp", "lid", lid, namespace=ACCOUNT),),
            at_ms=None,
            context=context,
        )[0]
        registry.sync_from_bridge_metadata(
            "whatsapp",
            [
                {
                    "chatJid": "synthetic@g.us",
                    "participants": [
                        {
                            "id": "8420000050@lid",
                            "phoneNumber": "4910000000050@s.whatsapp.net",
                        }
                    ],
                }
            ],
        )
        after_membership_change = service.resolve_mentions(
            (Identifier("whatsapp", "lid", lid, namespace=ACCOUNT),),
            at_ms=None,
            context=context,
        )[0]

        unqualified_registry = ChatRegistry(Path(":memory:"))
        unqualified_registry.sync_from_bridge_metadata(
            "whatsapp",
            [
                {
                    "chatJid": "synthetic@g.us",
                    "participants": [{"id": "4910000000050"}],
                }
            ],
        )
        unqualified = build_mention_read_context(
            event, chat_registry=unqualified_registry, knowledge=service
        )
        assert result.status == "ambiguous"
        assert result.person_id == member_resolution.person_id
        assert after_membership_change.status == "denied"
        assert after_membership_change.person_id is None
        assert unqualified is None
        unqualified_registry.close()
    finally:
        service.close()
        registry.close()


def _observe_pair_for_service(service, authority, phone: str, lid: str, *, name: str):
    observation = TrustedIdentityObservation(
        identifiers=(
            Identifier("whatsapp", "phone_jid", phone, namespace=ACCOUNT),
            Identifier("whatsapp", "lid", lid, namespace=ACCOUNT),
        ),
        evidence_ref=f"mention-observation:{phone}",
        observed_name=name,
        observed_at_ms=1_700_000_000_000,
        mapping_verified=True,
        account_namespace=ACCOUNT,
    )
    authority.issue_observation(observation)
    return service.resolve_person(observation)
