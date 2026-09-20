"""Phase 2 / Task 1: source-linked relational conversation membership.

A conversation thread describes subject-matter connection, not runtime routing.  One
source revision may belong to several threads; the text is never copied.  Membership is
always keyed by ``(conversation_id, source_event_id, source_revision)`` and every read
goes through the Phase-1 read gate, so a thread can never widen access to another chat.

The suite is self-contained and offline: temporary databases, synthetic principals, no
provider call and no live runtime state.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import pytest
from yeoman_gateway.knowledge.api import KnowledgeService, open_knowledge_store
from yeoman_gateway.knowledge.authority import (
    EvidenceAudience,
    FakeClock,
    FakePolicyAuthority,
    FakeSourceAuthority,
)
from yeoman_gateway.knowledge.models import (
    CONVERSATION_ORIGINS,
    CONVERSATION_RELATIONS,
    KnowledgeError,
    SourceRef,
    TrustedCaptureContext,
    TrustedReadContext,
    ValidationError,
)

WORKSPACE_ID = "conversation-workspace"
OWNER = "whatsapp:4910000000001"
TOM = "whatsapp:4910000000002"
ALEX = "whatsapp:4910000000003"
GROUP_A = "group-a"
GROUP_B = "group-b"


@dataclass
class ConversationHarness:
    """A fully synthetic knowledge runtime bound to a temporary database."""

    tmp_path: Path

    def __post_init__(self) -> None:
        self.clock = FakeClock()
        self.authority = FakeSourceAuthority()
        self.policy = FakePolicyAuthority(admins={OWNER}, capture_actors={OWNER})
        self.service: KnowledgeService = open_knowledge_store(
            self.tmp_path / "knowledge.db",
            workspace_id=WORKSPACE_ID,
            source_authority=self.authority,
            policy_authority=self.policy,
            clock=self.clock,
        )
        self._counter = 0

    # ── lifecycle ────────────────────────────────────────────────────────────

    def close(self) -> None:
        self.service.close()

    # ── sources ──────────────────────────────────────────────────────────────

    def source(
        self,
        *,
        chat: str = GROUP_A,
        author: str = TOM,
        revision: int = 1,
        audience: set[str] | None = None,
        author_only: bool = False,
        unknown_audience: bool = False,
        channel: str = "whatsapp",
    ) -> SourceRef:
        self._counter += 1
        source = SourceRef(
            event_id=f"event-{self._counter}",
            revision=int(revision),
            channel=channel,
            chat_id=chat,
            author_principal=author,
            occurred_at_ms=self.clock.now_ms(),
        )
        if unknown_audience:
            self.authority.issue_source(source, EvidenceAudience.unknown())
        elif author_only:
            self.authority.issue_source(source, EvidenceAudience.author_only())
        else:
            members = set(audience) if audience is not None else {author}
            self.authority.issue_source(
                source,
                EvidenceAudience.known(members, snapshot_id=f"snap-{source.event_id}"),
            )
        return source

    def unknown_source(self, *, chat: str = GROUP_A, author: str = TOM) -> SourceRef:
        """A source shape that was never issued to the authority."""
        self._counter += 1
        return SourceRef(
            event_id=f"forged-{self._counter}",
            revision=1,
            channel="whatsapp",
            chat_id=chat,
            author_principal=author,
            occurred_at_ms=self.clock.now_ms(),
        )

    # ── contexts ─────────────────────────────────────────────────────────────

    def capture_context(self, *sources: SourceRef) -> TrustedCaptureContext:
        request_id = f"cap-{'/'.join(item.event_id for item in sources) or 'empty'}"
        self.policy.issue_capture(request_id)
        return TrustedCaptureContext(
            request_id=request_id,
            policy_revision=self.policy.revision,
            capture_basis="user_message",
            authorized_sources=tuple(sources),
            actor_principal=OWNER,
            authorized=True,
        )

    def read_context(
        self,
        reader: str,
        *,
        chat: str = GROUP_A,
        recipients: set[str] | None = None,
        channel: str = "whatsapp",
    ) -> TrustedReadContext:
        members = set(recipients) if recipients is not None else {reader}
        revision = f"mem-{chat}-{len(members)}"
        self.policy.set_members(
            TrustedReadContext(
                principal_id=reader,
                channel=channel,
                chat_id=chat,
                recipient_principals=frozenset(members),
                membership_revision=revision,
                policy_revision=self.policy.revision,
                purpose="reply",
                now_ms=self.clock.now_ms(),
            ),
            members,
            revision=revision,
        )
        return TrustedReadContext(
            principal_id=reader,
            channel=channel,
            chat_id=chat,
            recipient_principals=frozenset(members),
            membership_revision=revision,
            policy_revision=self.policy.revision,
            purpose="reply",
            now_ms=self.clock.now_ms(),
        )

    def non_member_read_context(self, reader: str, *, chat: str = GROUP_A) -> TrustedReadContext:
        """A context whose chat membership provably excludes the reader."""
        members = {OWNER}
        self.policy.set_members(
            TrustedReadContext(
                principal_id=reader,
                channel="whatsapp",
                chat_id=chat,
                recipient_principals=frozenset(members),
                membership_revision=f"mem-{chat}-nonmember",
                policy_revision=self.policy.revision,
                purpose="reply",
                now_ms=self.clock.now_ms(),
            ),
            members,
            revision=f"mem-{chat}-nonmember",
        )
        return TrustedReadContext(
            principal_id=reader,
            channel="whatsapp",
            chat_id=chat,
            recipient_principals=frozenset(members),
            membership_revision=f"mem-{chat}-nonmember",
            policy_revision=self.policy.revision,
            purpose="reply",
            now_ms=self.clock.now_ms(),
        )

    # ── raw inspection (white-box assertions only) ───────────────────────────
    def raw_rows(self, table: str) -> list[sqlite3.Row]:
        return self.service._store.query(f"SELECT * FROM {table}")  # noqa: SLF001

    def table_columns(self, table: str) -> tuple[str, ...]:
        rows = self.service._store.query(  # noqa: SLF001
            f"PRAGMA table_info({table})"
        )
        return tuple(str(row["name"]) for row in rows)


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[ConversationHarness]:
    built = ConversationHarness(tmp_path)
    try:
        yield built
    finally:
        built.close()


# ── multi-membership ─────────────────────────────────────────────────────────


def test_one_source_revision_joins_two_conversations_without_copying_text(
    harness: ConversationHarness,
) -> None:
    h = harness
    source = h.source()
    context = h.capture_context(source)

    first = h.service.attach_conversation_membership(source, context=context)
    second = h.service.attach_conversation_membership(source, context=context)

    assert first.conversation_id != second.conversation_id
    for conversation_id in (first.conversation_id, second.conversation_id):
        view = h.service.conversation(conversation_id, context=h.read_context(TOM))
        assert [(item.source.event_id, item.source.revision) for item in view.memberships] == [
            (source.event_id, source.revision)
        ]

    # The membership rows carry the source key, never the source text.
    rows = h.raw_rows("conversation_memberships")
    assert len(rows) == 2
    assert {(str(row["conversation_id"]), str(row["source_event_id"])) for row in rows} == {
        (first.conversation_id, source.event_id),
        (second.conversation_id, source.event_id),
    }
    assert all("text" not in h.table_columns("conversations") for _ in (0,))
    assert "content" not in h.table_columns("conversations")
    assert "content" not in h.table_columns("conversation_memberships")


def test_replaying_a_membership_is_idempotent(harness: ConversationHarness) -> None:
    h = harness
    source = h.source()
    context = h.capture_context(source)

    first = h.service.attach_conversation_membership(source, context=context)
    replay = h.service.attach_conversation_membership(
        source, conversation_id=first.conversation_id, context=context
    )

    assert replay.conversation_id == first.conversation_id
    assert replay.new_memberships == 0
    assert len(h.raw_rows("conversation_memberships")) == 1
    assert len(h.raw_rows("conversations")) == 1


def test_membership_persists_origin_confidence_classifier_version_and_offsets(
    harness: ConversationHarness,
) -> None:
    h = harness
    source = h.source()
    receipt = h.service.attach_conversation_membership(
        source,
        origin="explicit_quote",
        confidence=0.42,
        classifier_version="conversation-classifier-v1",
        text_offsets=(3, 17),
        context=h.capture_context(source),
    )
    assert receipt.origin == "explicit_quote"
    assert receipt.confidence == pytest.approx(0.42)
    assert receipt.classifier_version == "conversation-classifier-v1"
    assert receipt.text_offsets == (3, 17)

    view = h.service.conversation(receipt.conversation_id, context=h.read_context(TOM))
    membership = view.memberships[0]
    assert membership.origin == "explicit_quote"
    assert membership.confidence == pytest.approx(0.42)
    assert membership.classifier_version == "conversation-classifier-v1"
    assert membership.text_offsets == (3, 17)


# ── explicit reply / quote ───────────────────────────────────────────────────


@pytest.mark.parametrize("kind", ["reply", "quote"])
def test_explicit_reply_or_quote_creates_a_relation_candidate_only(
    harness: ConversationHarness, kind: str
) -> None:
    h = harness
    earlier = h.source()
    later = h.source()
    context = h.capture_context(earlier, later)

    receipt = h.service.record_conversation_reference(
        later, refers_to=earlier, kind=kind, context=context
    )

    assert receipt.conversation_id != receipt.related_conversation_id
    assert [relation.relation for relation in receipt.relations] == ["related_to"]
    # Topic identity is not forced: each source stays in its own single conversation.
    assert len(h.raw_rows("conversations")) == 2
    memberships = h.raw_rows("conversation_memberships")
    assert len(memberships) == 2
    assert {str(row["conversation_id"]) for row in memberships} == {
        receipt.conversation_id,
        receipt.related_conversation_id,
    }
    view = h.service.conversation(receipt.related_conversation_id, context=h.read_context(TOM))
    assert [(item.relation, item.from_conversation_id, item.to_conversation_id) for item in view.relations] == [
        ("related_to", receipt.conversation_id, receipt.related_conversation_id)
    ]


def test_replaying_an_explicit_reference_is_idempotent(harness: ConversationHarness) -> None:
    h = harness
    earlier = h.source()
    later = h.source()
    context = h.capture_context(earlier, later)

    first = h.service.record_conversation_reference(
        later, refers_to=earlier, kind="reply", context=context
    )
    second = h.service.record_conversation_reference(
        later, refers_to=earlier, kind="reply", context=context
    )

    assert second.relation_created is False
    assert len(h.raw_rows("conversation_relations")) == 1
    assert first.conversation_id == second.conversation_id


def test_unsupported_relation_kind_is_rejected(harness: ConversationHarness) -> None:
    h = harness
    earlier = h.source()
    later = h.source()
    context = h.capture_context(earlier, later)
    with pytest.raises(ValidationError):
        h.service.record_conversation_reference(
            later, refers_to=earlier, kind="same_topic", context=context
        )
    assert set(CONVERSATION_RELATIONS) == {"branches_from", "merged_into", "related_to"}
    assert "same_topic" not in CONVERSATION_RELATIONS


# ── split and merge ──────────────────────────────────────────────────────────


def test_split_retains_prior_ids_and_memberships(harness: ConversationHarness) -> None:
    h = harness
    first = h.source()
    second = h.source()
    context = h.capture_context(first, second)
    original = h.service.attach_conversation_membership(first, context=context)
    h.service.attach_conversation_membership(
        second, conversation_id=original.conversation_id, context=context
    )

    split = h.service.split_conversation(
        original.conversation_id, sources=(second,), context=context
    )

    assert split.conversation_id != original.conversation_id
    assert split.origin == "split"
    # The prior conversation id and its memberships survive the split.
    surviving = h.service.conversation(original.conversation_id, context=h.read_context(TOM))
    assert [(item.source.event_id) for item in surviving.memberships] == [first.event_id, second.event_id]
    # The new branch is linked to the prior one and carries the moved membership.
    branched = h.service.conversation(split.conversation_id, context=h.read_context(TOM))
    assert [(item.relation, item.from_conversation_id, item.to_conversation_id) for item in branched.relations] == [
        ("branches_from", split.conversation_id, original.conversation_id)
    ]
    assert [item.source.event_id for item in branched.memberships] == [second.event_id]


def test_merge_retains_prior_ids_and_memberships_and_redirects(
    harness: ConversationHarness,
) -> None:
    h = harness
    first = h.source()
    second = h.source()
    context = h.capture_context(first, second)
    left = h.service.attach_conversation_membership(first, context=context)
    right = h.service.attach_conversation_membership(second, context=context)

    merged = h.service.merge_conversations(
        (left.conversation_id,), target_id=right.conversation_id, context=context
    )

    assert merged.conversation_id == right.conversation_id
    assert [
        (relation.relation, relation.from_conversation_id, relation.to_conversation_id)
        for relation in merged.relations
    ] == [("merged_into", left.conversation_id, right.conversation_id)]

    # The retired id is still present, still linked to its own membership, and redirects.
    rows = {str(row["conversation_id"]): row for row in h.raw_rows("conversations")}
    assert left.conversation_id in rows
    assert str(rows[left.conversation_id]["status"]) == "merged"
    assert str(rows[left.conversation_id]["merged_into"]) == right.conversation_id
    assert {
        (str(row["conversation_id"]), str(row["source_event_id"]))
        for row in h.raw_rows("conversation_memberships")
    } == {
        (left.conversation_id, first.event_id),
        (right.conversation_id, second.event_id),
    }

    redirected = h.service.conversation(left.conversation_id, context=h.read_context(TOM))
    assert redirected.conversation_id == right.conversation_id
    assert redirected.redirected_from == left.conversation_id
    assert [item.source.event_id for item in redirected.memberships] == [second.event_id]


# ── source authority ─────────────────────────────────────────────────────────


def test_missing_source_cannot_be_newly_attached(harness: ConversationHarness) -> None:
    h = harness
    forged = h.unknown_source()
    with pytest.raises(KnowledgeError) as excinfo:
        h.service.attach_conversation_membership(forged, context=h.capture_context(forged))
    assert excinfo.value.code in ("denied_unknown_basis", "unauthorized")
    assert h.raw_rows("conversation_memberships") == []
    assert h.raw_rows("conversations") == []


def test_revoked_source_cannot_be_newly_attached(harness: ConversationHarness) -> None:
    h = harness
    source = h.source()
    h.authority.revoke_source(source)
    with pytest.raises(KnowledgeError) as excinfo:
        h.service.attach_conversation_membership(source, context=h.capture_context(source))
    assert excinfo.value.code == "source_revoked"
    assert h.raw_rows("conversation_memberships") == []


def test_unauthorized_source_cannot_be_newly_attached(harness: ConversationHarness) -> None:
    h = harness
    known = h.source()
    other = h.source()
    with pytest.raises(KnowledgeError) as excinfo:
        h.service.attach_conversation_membership(other, context=h.capture_context(known))
    assert excinfo.value.code == "unauthorized"
    assert h.raw_rows("conversation_memberships") == []


def test_revoked_source_is_dropped_from_the_read_view(harness: ConversationHarness) -> None:
    h = harness
    first = h.source()
    second = h.source()
    context = h.capture_context(first, second)
    receipt = h.service.attach_conversation_membership(first, context=context)
    h.service.attach_conversation_membership(
        second, conversation_id=receipt.conversation_id, context=context
    )
    assert len(h.service.conversation(receipt.conversation_id, context=h.read_context(TOM)).memberships) == 2

    h.authority.revoke_source(second)

    view = h.service.conversation(receipt.conversation_id, context=h.read_context(TOM))
    assert [item.source.event_id for item in view.memberships] == [first.event_id]
    assert view.withheld_memberships == 1


# ── no cross-chat authority ──────────────────────────────────────────────────


def test_membership_never_grants_access_to_another_chat(harness: ConversationHarness) -> None:
    h = harness
    foreign = h.source(chat=GROUP_B)
    receipt = h.service.attach_conversation_membership(foreign, context=h.capture_context(foreign))

    # A reader in group A cannot read a conversation rooted in group B, ...
    denied = h.service.conversation(receipt.conversation_id, context=h.read_context(TOM, chat=GROUP_A))
    assert denied.memberships == ()
    assert denied.reason == "scope_mismatch"

    # ... and a source from another chat cannot be attached to a conversation of this one.
    local_source = h.source(chat=GROUP_A)
    local = h.service.attach_conversation_membership(
        local_source, context=h.capture_context(local_source)
    )
    with pytest.raises(KnowledgeError) as excinfo:
        h.service.attach_conversation_membership(
            foreign, conversation_id=local.conversation_id, context=h.capture_context(foreign)
        )
    assert excinfo.value.code in ("unauthorized", "denied_unknown_basis")
    assert len(h.raw_rows("conversation_memberships")) == 2


def test_no_membership_means_no_read(harness: ConversationHarness) -> None:
    h = harness
    source = h.source()
    receipt = h.service.attach_conversation_membership(source, context=h.capture_context(source))
    denied = h.service.conversation(receipt.conversation_id, context=h.non_member_read_context(ALEX))
    assert denied.memberships == ()
    assert denied.reason == "reader_not_a_member"


def test_reader_outside_the_source_audience_sees_nothing(harness: ConversationHarness) -> None:
    h = harness
    source = h.source(audience={TOM})
    receipt = h.service.attach_conversation_membership(source, context=h.capture_context(source))
    allowed = h.service.conversation(receipt.conversation_id, context=h.read_context(TOM))
    assert len(allowed.memberships) == 1

    denied = h.service.conversation(receipt.conversation_id, context=h.read_context(ALEX))
    assert denied.memberships == ()
    assert denied.withheld_memberships == 1


def test_unknown_audience_fails_closed(harness: ConversationHarness) -> None:
    h = harness
    source = h.source(audience={TOM})
    receipt = h.service.attach_conversation_membership(source, context=h.capture_context(source))
    # The authority now reports an unproven audience for the very same revision.
    h.authority.audiences[source.key] = EvidenceAudience.unknown()
    denied = h.service.conversation(receipt.conversation_id, context=h.read_context(TOM))
    assert denied.memberships == ()
    assert denied.withheld_memberships == 1


# ── relational truth, never writable JSON ────────────────────────────────────


def test_api_output_is_derived_from_relational_rows(harness: ConversationHarness) -> None:
    h = harness
    first = h.source()
    second = h.source()
    context = h.capture_context(first, second)
    receipt = h.service.attach_conversation_membership(first, context=context)

    # No JSON blob is a second truth for membership.
    forbidden = {"thread_ids", "conversation_ids", "conversation_ids_json", "memberships_json"}
    assert forbidden.isdisjoint(h.table_columns("conversations"))
    assert forbidden.isdisjoint(h.table_columns("conversation_memberships"))

    assert len(h.service.conversation(receipt.conversation_id, context=h.read_context(TOM)).memberships) == 1
    h.service.attach_conversation_membership(
        second, conversation_id=receipt.conversation_id, context=context
    )
    view = h.service.conversation(receipt.conversation_id, context=h.read_context(TOM))
    assert [item.source.event_id for item in view.memberships] == [first.event_id, second.event_id]


def test_conversation_view_lists_memberships_for_every_covered_revision(
    harness: ConversationHarness,
) -> None:
    h = harness
    source = h.source(revision=1)
    replacement = h.source(revision=2)
    context = h.capture_context(source, replacement)
    receipt = h.service.attach_conversation_membership(source, context=context)
    h.service.attach_conversation_membership(
        replacement, conversation_id=receipt.conversation_id, context=context
    )
    view = h.service.conversation(receipt.conversation_id, context=h.read_context(TOM))
    assert [(item.source.revision) for item in view.memberships] == [1, 2]


def test_origin_enumeration_is_closed(harness: ConversationHarness) -> None:
    h = harness
    source = h.source()
    assert set(CONVERSATION_ORIGINS) >= {"explicit_reply", "explicit_quote", "manual", "split", "merge"}
    with pytest.raises(ValidationError):
        h.service.attach_conversation_membership(
            source, origin="topic_classifier", context=h.capture_context(source)
        )
    assert h.raw_rows("conversation_memberships") == []
