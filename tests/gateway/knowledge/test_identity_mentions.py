"""Deterministic mention candidates stay inside verified chat identity scope."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from yeoman_gateway.core.models import InboundEvent
from yeoman_gateway.knowledge.models import (
    Identifier,
    PersonResolution,
    TrustedIdentityObservation,
)

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
    from tests.gateway.history_test_helpers import prepare_history_retirement_knowledge

    service = open_knowledge_store(
        prepare_history_retirement_knowledge(tmp_path, name="runtime-knowledge"),
        workspace_id="synthetic-mention-workspace",
        source_authority=authority,
        policy_authority=policy,
    )
    try:
        lid = "8420000051@lid"
        sender_phone = "4910000000050@s.whatsapp.net"
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
        member_person_id = queries.resolve_identifier(
            lid, at_ms=1_700_000_001_000, time_basis="native"
        )
        assert member_person_id is not None

        from yeoman_gateway.history.context import history_knowledge_scope

        with history_knowledge_scope(snapshot, service):
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
        with history_knowledge_scope(next_snapshot, service):
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
        assert result.status == ("ambiguous" if account_id == "default" else "unresolved")
        assert result.person_id == (member_person_id if account_id == "default" else None)
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


