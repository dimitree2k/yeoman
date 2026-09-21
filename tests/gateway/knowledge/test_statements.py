"""P2.1: statements carry several people in explicit roles with proven sources.

The example from the design: "Tom says Alex drives to Berlin with Maria" is *one*
statement with Tom as the proven speaker and Alex/Maria as referenced people.  It
confirms nothing about the trip.
"""

from __future__ import annotations

import pytest
from yeoman_gateway.knowledge.models import (
    KnowledgeError,
    PersonLinkCandidate,
    SourceRef,
    StatementCandidate,
    ValidationError,
)


def test_one_statement_has_multiple_people_and_keeps_speaker(knowledge_harness):
    h = knowledge_harness
    tom, alex, maria = (h.person(n) for n in ("Tom", "Alex", "Maria"))
    source = h.source(tom, audience={h.principal_for(tom), h.principal_for(alex), h.principal_for(maria)})
    candidate = h.candidate(
        "Alex faehrt mit Maria nach Berlin.",
        source,
        subjects=(alex,),
        participants=(alex, maria),
    )
    result = h.service.capture(candidate, context=h.capture_context(source))
    assert len(result.statement_ids) == 1
    sid = result.statement_ids[0]
    assert {(link.person_id, link.role) for link in h.links(sid)} == {
        (tom, "speaker"),
        (alex, "subject"),
        (alex, "participant"),
        (maria, "participant"),
    }
    assert h.statement(sid).status == "assertion"
    assert h.statement(sid).sources == (source,)


def test_reported_speaker_is_distinct_from_the_actual_speaker(knowledge_harness):
    h = knowledge_harness
    tom, alex = (h.person(n) for n in ("Tom", "Alex"))
    source = h.source(tom, audience={h.principal_for(tom), h.principal_for(alex)})
    result = h.service.capture(
        h.candidate("Alex sagt, er faehrt morgen.", source, reported_speakers=(alex,)),
        context=h.capture_context(source),
    )
    sid = result.statement_ids[0]
    roles = {(link.person_id, link.role) for link in h.links(sid)}
    assert (tom, "speaker") in roles
    assert (alex, "reported_speaker") in roles
    assert (alex, "speaker") not in roles
    # The statement keeps Tom's event as its only source.
    assert h.statement(sid).sources == (source,)


def test_same_person_may_hold_several_roles(knowledge_harness):
    h = knowledge_harness
    tom, alex = (h.person(n) for n in ("Tom", "Alex"))
    source = h.source(tom, audience={h.principal_for(tom), h.principal_for(alex)})
    result = h.service.capture(
        h.candidate(
            "Alex faehrt selbst.",
            source,
            subjects=(alex,),
            participants=(alex,),
            reported_speakers=(alex,),
        ),
        context=h.capture_context(source),
    )
    roles = {role for person, role in ((link.person_id, link.role) for link in h.links(result.statement_ids[0])) if person == alex}
    assert roles == {"subject", "participant", "reported_speaker"}


def test_no_link_is_allowed_for_a_non_personal_statement(knowledge_harness):
    h = knowledge_harness
    tom = h.person("Tom")
    source = h.source(tom)
    result = h.service.capture(
        h.candidate("Es regnet in Berlin.", source), context=h.capture_context(source)
    )
    sid = result.statement_ids[0]
    # Only the transport speaker, which the runtime sets itself.
    assert {(link.person_id, link.role) for link in h.links(sid)} == {(tom, "speaker")}


def test_person_link_to_a_foreign_uuid_is_rejected(knowledge_harness):
    h = knowledge_harness
    tom = h.person("Tom")
    source = h.source(tom)
    with pytest.raises(KnowledgeError) as excinfo:
        h.service.capture(
            h.candidate(
                "Alex reist.",
                source,
                subjects=("00000000-0000-4000-8000-000000000000",),
            ),
            context=h.capture_context(source),
        )
    assert excinfo.value.code == "invalid_input"
    assert h.snapshot_counts()["knowledge_statements"] == 0


def test_person_link_must_use_one_of_the_statements_sources(knowledge_harness):
    h = knowledge_harness
    tom, alex = (h.person(n) for n in ("Tom", "Alex"))
    source = h.source(tom)
    other = h.source(alex, chat="group-b")
    link = PersonLinkCandidate(
        person_id=alex, role="subject", source=other, attribution="extracted"
    )
    candidate = StatementCandidate(
        content="Alex reist.",
        sources=(source,),
        people=(link,),
        extractor_version="test-extractor-1",
        confidence=0.4,
    )
    with pytest.raises(KnowledgeError) as excinfo:
        h.service.capture(candidate, context=h.capture_context(source))
    assert excinfo.value.code == "invalid_input"


def test_invalid_role_and_too_many_links_are_rejected_before_writing(knowledge_harness):
    h = knowledge_harness
    tom = h.person("Tom")
    source = h.source(tom)
    with pytest.raises(ValidationError):
        PersonLinkCandidate(person_id=tom, role="boss", source=source, attribution="extracted")
    with pytest.raises(ValidationError):
        StatementCandidate(
            content="x",
            sources=(),
            extractor_version="v",
        )
    with pytest.raises(ValidationError):
        StatementCandidate(
            content="x",
            sources=tuple(
                SourceRef(
                    event_id=f"e{index}",
                    revision=1,
                    channel="whatsapp",
                    chat_id="group-a",
                    author_principal="whatsapp:4910000000002",
                    occurred_at_ms=1,
                )
                for index in range(40)
            ),
            extractor_version="v",
        )
    assert h.snapshot_counts()["knowledge_statements"] == 0


def test_unresolved_names_are_kept_as_metadata_not_as_people(knowledge_harness):
    h = knowledge_harness
    tom = h.person("Tom")
    source = h.source(tom)
    result = h.service.capture(
        h.candidate("Alex und Bea reisen.", source, unresolved=("Alex", "Bea")),
        context=h.capture_context(source),
    )
    sid = result.statement_ids[0]
    row = h.service._store.query_one(  # noqa: SLF001 - proves no guessed person was created
        "SELECT unresolved_mentions_json FROM knowledge_statements WHERE statement_id = ?",
        (sid,),
    )
    assert row is not None
    assert "Alex" in str(row["unresolved_mentions_json"])
    # No person was invented from a bare first name.
    assert h.snapshot_counts()["contacts"] == 1


def test_confidence_never_confirms_a_statement(knowledge_harness):
    h = knowledge_harness
    tom = h.person("Tom")
    source = h.source(tom)
    result = h.service.capture(
        h.candidate("Alex reist.", source, confidence=0.99),
        context=h.capture_context(source),
    )
    assert h.statement(result.statement_ids[0]).status == "assertion"


def test_unknown_source_evidence_is_rejected(knowledge_harness):
    h = knowledge_harness
    tom = h.person("Tom")
    unissued = SourceRef(
        event_id="never-issued",
        revision=1,
        channel="whatsapp",
        chat_id="group-a",
        author_principal=h.principal_for(tom),
        occurred_at_ms=1,
    )
    with pytest.raises(KnowledgeError) as excinfo:
        h.service.capture(
            h.candidate("Alex reist.", unissued), context=h.capture_context(unissued)
        )
    assert excinfo.value.code == "denied_unknown_basis"


def test_unknown_audience_is_denied_and_not_defaulted_to_normal(knowledge_harness):
    h = knowledge_harness
    tom = h.person("Tom")
    source = h.source(tom, unknown_audience=True)
    with pytest.raises(KnowledgeError) as excinfo:
        h.service.capture(
            h.candidate("Alex reist.", source), context=h.capture_context(source)
        )
    assert excinfo.value.code == "denied_unknown_basis"
    assert h.snapshot_counts()["knowledge_statements"] == 0


def test_transport_speaker_cannot_be_rewritten_by_a_model(knowledge_harness):
    h = knowledge_harness
    tom, alex = (h.person(n) for n in ("Tom", "Alex"))
    source = h.source(tom, audience={h.principal_for(tom), h.principal_for(alex)})
    forged = PersonLinkCandidate(
        person_id=alex, role="speaker", source=source, attribution="transport"
    )
    candidate = StatementCandidate(
        content="Alex reist.",
        sources=(source,),
        people=(forged,),
        extractor_version="test-extractor-1",
        confidence=0.5,
    )
    with pytest.raises(KnowledgeError) as excinfo:
        h.service.capture(candidate, context=h.capture_context(source))
    assert excinfo.value.code == "unauthorized"


def test_extractor_may_confirm_the_transport_speaker(knowledge_harness):
    h = knowledge_harness
    tom = h.person("Tom")
    source = h.source(tom)
    agreed = PersonLinkCandidate(
        person_id=tom, role="speaker", source=source, attribution="transport"
    )
    candidate = StatementCandidate(
        content="Ich reise.",
        sources=(source,),
        people=(agreed,),
        extractor_version="test-extractor-1",
        confidence=0.5,
    )
    result = h.service.capture(candidate, context=h.capture_context(source))
    assert {(link.person_id, link.role) for link in h.links(result.statement_ids[0])} == {
        (tom, "speaker")
    }


def test_merging_people_keeps_statement_edges_on_original_ids(knowledge_harness):
    """A merge during/after capture must not rewrite historical edges."""
    h = knowledge_harness
    tom, alex, maria = (h.person(n) for n in ("Tom", "Alex", "Maria"))
    source = h.source(tom, audience={h.principal_for(tom), h.principal_for(alex), h.principal_for(maria)})
    result = h.service.capture(
        h.candidate("Alex reist mit Maria.", source, subjects=(alex,), participants=(maria,)),
        context=h.capture_context(source),
    )
    sid = result.statement_ids[0]
    before = h.service._store.query(  # noqa: SLF001 - original edges must not move
        "SELECT person_id, role FROM knowledge_statement_people WHERE statement_id = ?"
        " ORDER BY person_id, role",
        (sid,),
    )
    h.service.merge_people(
        alex, maria, expected_revision=h.identity_revision(), context=h.admin_context()
    )
    after = h.service._store.query(  # noqa: SLF001
        "SELECT person_id, role FROM knowledge_statement_people WHERE statement_id = ?"
        " ORDER BY person_id, role",
        (sid,),
    )
    assert [tuple(row) for row in before] == [tuple(row) for row in after]
    # The canonical view, however, now resolves both original ids to one person.
    context = h.read_context(tom, recipients={h.principal_for(tom), h.principal_for(alex), h.principal_for(maria)})
    assert h.recall_person(alex, context).statement_ids == (sid,)
    assert h.recall_person(maria, context).statement_ids == (sid,)


def test_rollback_keeps_identity_and_statement_changes_together(knowledge_harness):
    h = knowledge_harness
    tom = h.person("Tom")
    source = h.source(tom)
    before = h.snapshot_counts()
    h.fail_next_commit()
    with pytest.raises(KnowledgeError):
        h.service.capture(
            h.candidate("Alex reist.", source), context=h.capture_context(source)
        )
    assert h.snapshot_counts() == before
    assert h.active_statements_for_source(source) == ()


# ── roles, facets and the transport speaker (T11-T13, §7.4) ──────────────────


def test_a_third_party_subject_is_not_the_speaker(knowledge_harness):
    """T11: the authenticated sender stays ``speaker``; the subject stays the subject."""
    h = knowledge_harness
    tom = h.person("Tom")
    alex = h.person("Alex")
    source = h.source(tom)
    statement_id = h.capture_text(
        "Alex wohnt in Köln.", source, subjects=(alex,), reported_speakers=(tom,)
    ).statement_ids[0]
    links = h.links(statement_id)
    by_role: dict[str, set[str]] = {}
    for link in links:
        by_role.setdefault(link.role, set()).add(link.person_id)
    assert by_role["speaker"] == {tom}
    assert by_role["subject"] == {alex}
    assert by_role["reported_speaker"] == {tom}
    # A statement about Alex is not evidence that Alex said it.
    assert tom not in by_role["subject"]


def test_a_facet_requires_an_active_subject_role(knowledge_harness):
    """§7.4: the speaker alone does not make a statement about the speaker."""
    from yeoman_gateway.knowledge.models import (
        AttributeCandidate,
        AttributeValue,
        PersonLinkCandidate,
        StatementCandidate,
    )

    h = knowledge_harness
    tom = h.person("Tom")
    alex = h.person("Alex")
    source = h.source(tom)
    candidate = StatementCandidate(
        content="Alex wohnt in Köln.",
        sources=(source,),
        people=(
            PersonLinkCandidate(
                person_id=alex, role="subject", source=source, attribution="extracted"
            ),
        ),
        attributes=(
            AttributeCandidate(
                person_id=alex,
                attribute_key="residence",
                value=AttributeValue("Köln", precision="exact"),
            ),
            # Tom is only the speaker here, so his facet must not be published.
            AttributeCandidate(person_id=tom, attribute_key="residence", value=AttributeValue("Bonn")),
        ),
        extractor_version="test-extractor-1",
        confidence=0.5,
    )
    result = h.service.capture(candidate, context=h.capture_context(source))
    statement_id = result.statement_ids[0]
    facets = h.service._statements.attributes_of(statement_id)  # noqa: SLF001
    assert [item["person_id"] for item in facets] == [alex]
    assert facets[0]["attribute_key"] == "residence"
    assert facets[0]["value"] == {"text": "Köln", "precision": "exact"}


def test_a_facet_is_unreadable_as_soon_as_its_statement_is(knowledge_harness):
    """A revoked statement locks its facet too, before any physical index cleanup."""
    from yeoman_gateway.knowledge.models import (
        AttributeCandidate,
        AttributeValue,
        PersonLinkCandidate,
        StatementCandidate,
    )

    h = knowledge_harness
    tom = h.person("Tom")
    alex = h.person("Alex")
    source = h.source(tom)
    candidate = StatementCandidate(
        content="Alex wohnt in Köln.",
        sources=(source,),
        people=(
            PersonLinkCandidate(
                person_id=alex, role="subject", source=source, attribution="extracted"
            ),
        ),
        attributes=(
            AttributeCandidate(
                person_id=alex, attribute_key="residence", value=AttributeValue("Köln")
            ),
        ),
        extractor_version="test-extractor-1",
        confidence=0.5,
    )
    statement_id = h.service.capture(candidate, context=h.capture_context(source)).statement_ids[0]
    assert h.service._statements.attributes_of(statement_id)  # noqa: SLF001

    h.service.invalidate_source(source, context=h.capture_context(source))
    # The row is still stored (a revoke is not a delete) but no longer reader-visible.
    assert h.service._store.scalar(  # noqa: SLF001 - white-box lock assertion
        "SELECT COUNT(*) FROM knowledge_person_attributes WHERE statement_id = ?",
        (statement_id,),
    ) == 1
    assert h.service._statements.attributes_of(statement_id) == ()  # noqa: SLF001


def test_facets_are_published_in_one_transaction_with_the_statement(knowledge_harness):
    """A failing commit leaves neither the statement nor its facet behind."""
    from yeoman_gateway.knowledge.models import (
        AttributeCandidate,
        AttributeValue,
        PersonLinkCandidate,
        StatementCandidate,
    )

    h = knowledge_harness
    tom = h.person("Tom")
    alex = h.person("Alex")
    source = h.source(tom)
    candidate = StatementCandidate(
        content="Alex wohnt in Köln.",
        sources=(source,),
        people=(
            PersonLinkCandidate(
                person_id=alex, role="subject", source=source, attribution="extracted"
            ),
        ),
        attributes=(
            AttributeCandidate(
                person_id=alex, attribute_key="residence", value=AttributeValue("Köln")
            ),
        ),
        extractor_version="test-extractor-1",
        confidence=0.5,
    )
    before_statements = h.snapshot_counts()["knowledge_statements"]
    h.fail_next_commit()
    with pytest.raises(KnowledgeError):
        h.service.capture(candidate, context=h.capture_context(source))
    counts = h.snapshot_counts()
    assert counts["knowledge_statements"] == before_statements
    assert counts.get("knowledge_person_attributes", 0) == 0
