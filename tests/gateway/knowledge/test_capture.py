"""P2.2: replay, dedup and capture authorization.

A statement is identified by its *source-bound* key: the same text from a different
source or a different audience is a different statement.  Capture permission and the
audience come from trusted runtime metadata, never from the model.
"""

from __future__ import annotations

import pytest
from yeoman_gateway.knowledge.models import (
    KnowledgeError,
    PersonLinkCandidate,
    SourceRef,
    StatementCandidate,
    TrustedCaptureContext,
)


def test_replay_is_idempotent_but_other_audience_is_independent(knowledge_harness):
    h = knowledge_harness
    tom = h.person("Tom")
    a = h.source(tom, chat="group-a", audience={h.principal_for(tom)})
    b = h.source(tom, chat="private-b", audience={h.principal_for(tom)})
    first = h.capture_text("Alex reist morgen.", a)
    replay = h.capture_text("Alex reist morgen.", a)
    independent = h.capture_text("Alex reist morgen.", b)
    assert first.statement_ids == replay.statement_ids
    assert independent.statement_ids != first.statement_ids


def test_same_text_different_audience_in_one_chat_stays_separate(knowledge_harness):
    h = knowledge_harness
    tom, alex = (h.person(n) for n in ("Tom", "Alex"))
    wide = h.source(tom, audience={h.principal_for(tom), h.principal_for(alex)})
    narrow = h.source(tom, audience={h.principal_for(tom)})
    first = h.capture_text("Alex reist morgen.", wide)
    second = h.capture_text("Alex reist morgen.", narrow)
    assert first.statement_ids != second.statement_ids


def test_replay_of_a_new_source_revision_is_a_new_statement(knowledge_harness):
    h = knowledge_harness
    tom = h.person("Tom")
    first = h.capture_text("Alex reist morgen.", h.source(tom, revision=1))
    second = h.capture_text("Alex reist morgen.", h.source(tom, revision=2))
    assert first.statement_ids != second.statement_ids


def test_replay_with_a_different_extractor_version_is_a_new_statement(knowledge_harness):
    h = knowledge_harness
    tom = h.person("Tom")
    source = h.source(tom)
    first = h.capture_text("Alex reist morgen.", source)
    candidate = h.candidate("Alex reist morgen.", source, extractor_version="extractor-2")
    second = h.service.capture(candidate, context=h.capture_context(source))
    assert first.statement_ids != second.statement_ids


def test_two_real_sources_produce_two_statements(knowledge_harness):
    h = knowledge_harness
    tom, alex = (h.person(n) for n in ("Tom", "Alex"))
    audience = {h.principal_for(tom), h.principal_for(alex)}
    first_source = h.source(tom, audience=audience)
    second_source = h.source(alex, audience=audience)
    first = h.capture_text("Alex reist morgen.", first_source)
    second = h.capture_text("Alex reist morgen.", second_source)
    assert first.statement_ids != second.statement_ids


def test_capture_without_authorization_is_refused(knowledge_harness):
    h = knowledge_harness
    tom = h.person("Tom")
    source = h.source(tom)
    context = TrustedCaptureContext(
        request_id="cap-x",
        policy_revision=h.policy.revision,
        capture_basis="user_message",
        authorized_sources=(source,),
        actor_principal="whatsapp:4910000000777",
        authorized=True,
    )
    with pytest.raises(KnowledgeError) as excinfo:
        h.service.capture(h.candidate("Alex reist.", source), context=context)
    assert excinfo.value.code == "unauthorized"


def test_capture_context_must_come_from_the_runtime(knowledge_harness):
    h = knowledge_harness
    tom = h.person("Tom")
    source = h.source(tom)
    never_issued = TrustedCaptureContext(
        request_id="never-issued-capture-request",
        policy_revision=h.policy.revision,
        capture_basis="user_message",
        authorized_sources=(source,),
        actor_principal="whatsapp:4910000000001",
        authorized=True,
    )
    with pytest.raises(KnowledgeError) as excinfo:
        h.service.capture(h.candidate("Alex reist.", source), context=never_issued)
    assert excinfo.value.code == "unauthorized"


def test_capture_with_a_stale_policy_revision_is_refused(knowledge_harness):
    h = knowledge_harness
    tom = h.person("Tom")
    source = h.source(tom)
    request_id = "cap-stale"
    h.policy.issue_capture(request_id)
    context = TrustedCaptureContext(
        request_id=request_id,
        policy_revision=0,
        capture_basis="user_message",
        authorized_sources=(source,),
        actor_principal="whatsapp:4910000000001",
        authorized=True,
    )
    with pytest.raises(KnowledgeError) as excinfo:
        h.service.capture(h.candidate("Alex reist.", source), context=context)
    assert excinfo.value.code == "stale_revision"


def test_source_outside_the_authorized_set_is_refused(knowledge_harness):
    h = knowledge_harness
    tom = h.person("Tom")
    allowed = h.source(tom)
    other = h.source(tom)
    context = h.capture_context(allowed)
    with pytest.raises(KnowledgeError) as excinfo:
        h.service.capture(h.candidate("Alex reist.", other), context=context)
    assert excinfo.value.code == "unauthorized"
    assert h.snapshot_counts()["knowledge_statements"] == 0


def test_revoked_source_cannot_be_captured(knowledge_harness):
    h = knowledge_harness
    tom = h.person("Tom")
    source = h.source(tom)
    h.authority.revoke_source(source)  # noqa: SLF001 - trusted adapter state
    with pytest.raises(KnowledgeError) as excinfo:
        h.service.capture(
            h.candidate("Alex reist.", source), context=h.capture_context(source)
        )
    assert excinfo.value.code == "source_revoked"


def test_caller_supplied_audience_is_not_part_of_the_contract():
    """StatementCandidate has no audience field: the model cannot widen a read."""
    assert "audience" not in StatementCandidate.__dataclass_fields__
    assert "visibility" not in StatementCandidate.__dataclass_fields__
    assert "owner" not in StatementCandidate.__dataclass_fields__


def test_forged_person_uuid_from_the_extractor_is_rejected(knowledge_harness):
    h = knowledge_harness
    tom, alex = (h.person(n) for n in ("Tom", "Alex"))
    source = h.source(tom)
    forged = PersonLinkCandidate(
        person_id="11111111-2222-4333-8444-555555555555",
        role="subject",
        source=source,
        attribution="extracted",
    )
    candidate = StatementCandidate(
        content="Alex reist.",
        sources=(source,),
        people=(forged,),
        extractor_version="test-extractor-1",
        confidence=0.9,
    )
    with pytest.raises(KnowledgeError) as excinfo:
        h.service.capture(candidate, context=h.capture_context(source))
    assert excinfo.value.code == "invalid_input"


def test_fabricated_source_revision_is_rejected(knowledge_harness):
    """A revision that the runtime never issued is not provenance."""
    h = knowledge_harness
    tom = h.person("Tom")
    source = h.source(tom, revision=1)
    fabricated = SourceRef(
        event_id=source.event_id,
        revision=7,
        channel=source.channel,
        chat_id=source.chat_id,
        author_principal=source.author_principal,
        occurred_at_ms=source.occurred_at_ms,
    )
    candidate = StatementCandidate(
        content="Alex reist.",
        sources=(source, fabricated),
        people=(),
        extractor_version="test-extractor-1",
        confidence=0.5,
    )
    with pytest.raises(KnowledgeError) as excinfo:
        h.service.capture(candidate, context=h.capture_context(source))
    assert excinfo.value.code == "denied_unknown_basis"
    assert h.snapshot_counts()["knowledge_statements"] == 0


def test_forged_source_metadata_is_rejected(knowledge_harness):
    """A source with the right id but a rewritten author is not the issued evidence."""
    h = knowledge_harness
    tom, alex = (h.person(n) for n in ("Tom", "Alex"))
    source = h.source(tom)
    rewritten = SourceRef(
        event_id=source.event_id,
        revision=source.revision,
        channel=source.channel,
        chat_id=source.chat_id,
        author_principal=h.principal_for(alex),
        occurred_at_ms=source.occurred_at_ms,
    )
    with pytest.raises(KnowledgeError) as excinfo:
        h.service.capture(
            h.candidate("Alex reist.", rewritten), context=h.capture_context(source)
        )
    assert excinfo.value.code == "denied_unknown_basis"


def test_ambiguous_name_stays_unresolved_instead_of_picking_a_person(knowledge_harness):
    h = knowledge_harness
    tom = h.person("Tom")
    h.person("Alex")
    h.person_for("whatsapp:4910000000099", "Alex Two")
    source = h.source(tom)
    result = h.service.capture(
        h.candidate("Alex reist.", source, unresolved=("Alex",)),
        context=h.capture_context(source),
    )
    sid = result.statement_ids[0]
    assert {link.role for link in h.links(sid)} == {"speaker"}
    assert h.snapshot_counts()["contacts"] == 3


def test_capture_result_reports_rejected_entries(knowledge_harness):
    h = knowledge_harness
    tom = h.person("Tom")
    source = h.source(tom)
    result = h.service.capture(
        h.candidate("Alex reist.", source), context=h.capture_context(source)
    )
    assert result.ok
    assert result.rejected == ()


def test_capture_is_atomic_for_multiple_sources(knowledge_harness):
    h = knowledge_harness
    tom, alex = (h.person(n) for n in ("Tom", "Alex"))
    audience = {h.principal_for(tom), h.principal_for(alex)}
    first = h.source(tom, audience=audience)
    second = h.source(alex, audience=audience)
    context = h.capture_context(first, second)
    candidate = StatementCandidate(
        content="Alex reist morgen.",
        sources=(first, second),
        people=(),
        extractor_version="test-extractor-1",
        confidence=0.5,
    )
    result = h.service.capture(candidate, context=context)
    sid = result.statement_ids[0]
    assert {source.key for source in h.statement(sid).sources} == {first.key, second.key}
    # Both sources now find the same statement.
    assert h.active_statements_for_source(first) == (sid,)
    assert h.active_statements_for_source(second) == (sid,)
