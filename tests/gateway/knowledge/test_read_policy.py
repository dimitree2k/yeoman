"""P3.1: one read gate for reader rights, output audience and source basis."""

from __future__ import annotations

import pytest
from yeoman_gateway.knowledge.models import (
    RecallQuery,
    ValidationError,
)


def test_reader_permission_does_not_authorize_group_disclosure(knowledge_harness):
    h = knowledge_harness
    tom, alex, maria = (h.person(n) for n in ("Tom", "Alex", "Maria"))
    source = h.source(tom, chat="group-a", audience={h.principal_for(tom), h.principal_for(alex)})
    h.capture_text("secret-token-731", source, subjects=(alex,))
    context = h.read_context(
        tom,
        chat="group-a",
        recipients={h.principal_for(tom), h.principal_for(alex), h.principal_for(maria)},
    )
    result = h.recall_person(alex, context)
    assert result.statement_ids == ()
    assert "secret-token-731" not in result.text
    assert not h.provider_spy.saw("secret-token-731")


def test_same_audience_group_read_is_allowed(knowledge_harness):
    h = knowledge_harness
    tom, alex, maria = (h.person(n) for n in ("Tom", "Alex", "Maria"))
    audience = {h.principal_for(tom), h.principal_for(alex), h.principal_for(maria)}
    source = h.source(tom, chat="group-a", audience=audience)
    sid = h.capture_text("Alex reist mit Maria.", source, subjects=(alex,), participants=(maria,)).statement_ids[0]
    context = h.read_context(tom, chat="group-a", recipients=audience)
    result = h.recall_person(alex, context)
    assert result.statement_ids == (sid,)
    assert "Alex reist mit Maria." in result.text
    assert result.source_refs == (source,)


def test_direct_message_claim_stays_hidden_in_the_group(knowledge_harness):
    h = knowledge_harness
    tom, alex = (h.person(n) for n in ("Tom", "Alex"))
    dm = h.source(tom, chat="dm-tom-alex", audience={h.principal_for(tom), h.principal_for(alex)})
    sid = h.capture_text("Nur im Direktchat.", dm, subjects=(alex,)).statement_ids[0]
    group = h.read_context(
        tom,
        chat="group-a",
        recipients={h.principal_for(tom), h.principal_for(alex)},
    )
    assert h.recall_person(alex, group).text == ""
    direct = h.read_context(
        tom,
        chat="dm-tom-alex",
        recipients={h.principal_for(tom), h.principal_for(alex)},
        is_direct=True,
    )
    assert h.recall_person(alex, direct).statement_ids == (sid,)


def test_membership_must_be_proven(knowledge_harness):
    h = knowledge_harness
    tom, alex = (h.person(n) for n in ("Tom", "Alex"))
    source = h.source(tom, audience={h.principal_for(tom), h.principal_for(alex)})
    h.capture_text("Alex reist.", source, subjects=(alex,))
    unknown = h.read_context(
        tom,
        recipients={h.principal_for(tom), h.principal_for(alex)},
        membership_known=False,
    )
    result = h.recall_person(alex, unknown)
    assert result.statement_ids == ()
    assert result.reason == "membership_unknown"


def test_new_group_member_blocks_the_statement(knowledge_harness):
    h = knowledge_harness
    tom, alex = (h.person(n) for n in ("Tom", "Alex"))
    source = h.source(tom, audience={h.principal_for(tom), h.principal_for(alex)})
    h.capture_text("private-itinerary", source, subjects=(alex,))
    before = h.read_context(
        tom, recipients={h.principal_for(tom), h.principal_for(alex)}
    )
    assert h.recall_person(alex, before).statement_ids
    after = h.add_recipient(before, h.person("Nadia"))
    result = h.recall_person(alex, after)
    assert result.statement_ids == ()
    assert "private-itinerary" not in result.text


def test_recipient_outside_the_chat_blocks_the_statement(knowledge_harness):
    """A verified member who is not in the statement's audience blocks the output."""
    h = knowledge_harness
    tom, alex = (h.person(n) for n in ("Tom", "Alex"))
    source = h.source(tom, audience={h.principal_for(tom), h.principal_for(alex)})
    sid = h.capture_text("Alex reist.", source, subjects=(alex,)).statement_ids[0]
    context = h.read_context(
        tom, recipients={h.principal_for(tom), h.principal_for(alex)}
    )
    assert h.recall_person(alex, context).statement_ids == (sid,)
    # The same chat gains a member the statement was never released to.
    after = h.add_recipient(context, h.person("Nadia"))
    result = h.recall_person(alex, after)
    assert result.statement_ids == ()
    assert "Alex reist." not in result.text


def test_stale_policy_revision_denies_the_read(knowledge_harness):
    h = knowledge_harness
    tom, alex = (h.person(n) for n in ("Tom", "Alex"))
    source = h.source(tom, audience={h.principal_for(tom), h.principal_for(alex)})
    h.capture_text("Alex reist.", source, subjects=(alex,))
    stale = h.read_context(
        tom,
        recipients={h.principal_for(tom), h.principal_for(alex)},
        policy_revision=0,
    )
    result = h.recall_person(alex, stale)
    assert result.statement_ids == ()
    assert result.reason == "stale_policy_revision"


def test_reader_outside_the_target_audience_is_denied(knowledge_harness):
    h = knowledge_harness
    tom, alex, maria = (h.person(n) for n in ("Tom", "Alex", "Maria"))
    source = h.source(tom, audience={h.principal_for(tom), h.principal_for(alex)})
    h.capture_text("Alex reist.", source, subjects=(alex,))
    # The chat contains Tom, Alex and Maria; only Tom and Alex were in the audience.
    as_maria = h.read_context(
        maria,
        recipients={h.principal_for(tom), h.principal_for(alex), h.principal_for(maria)},
    )
    assert as_maria.principal_id == h.principal_for(maria)
    result = h.recall_person(alex, as_maria)
    assert result.statement_ids == ()
    assert "Alex reist." not in result.text
    # With only the two authorized members as recipients the same reader sees it.
    two_members = h.read_context(
        alex, recipients={h.principal_for(tom), h.principal_for(alex)}
    )
    assert h.recall_person(alex, two_members).statement_ids


def test_revoked_and_expired_statements_are_denied_after_the_prefilter(knowledge_harness):
    h = knowledge_harness
    tom, alex = (h.person(n) for n in ("Tom", "Alex"))
    audience = {h.principal_for(tom), h.principal_for(alex)}
    source = h.source(tom, audience=audience)
    sid = h.capture_text("Alex reist.", source, subjects=(alex,)).statement_ids[0]
    context = h.read_context(tom, recipients=audience)
    assert h.recall_person(alex, context).statement_ids == (sid,)

    prepared = h.service.recall(
        RecallQuery(person_ids=(alex,)), context=context
    )
    h.service.invalidate_source(source, context=h.capture_context(source))
    assert h.service.revalidate(prepared, context=context).statement_ids == ()


def test_role_filter_distinguishes_source_and_subject(knowledge_harness):
    h = knowledge_harness
    tom, alex, maria = (h.person(n) for n in ("Tom", "Alex", "Maria"))
    audience = {h.principal_for(tom), h.principal_for(alex), h.principal_for(maria)}
    source = h.source(tom, audience=audience)
    sid = h.capture_text(
        "Alex faehrt mit Maria nach Berlin.", source, subjects=(alex,), participants=(maria,)
    ).statement_ids[0]
    context = h.read_context(tom, recipients=audience)

    as_speaker = h.service.recall(
        RecallQuery(person_ids=(tom,), roles=("speaker",)), context=context
    )
    as_subject = h.service.recall(
        RecallQuery(person_ids=(tom,), roles=("subject",)), context=context
    )
    assert as_speaker.statement_ids == (sid,)
    assert as_subject.statement_ids == ()
    assert h.recall_person(alex, context).statement_ids == (sid,)
    assert h.recall_person(maria, context).statement_ids == (sid,)


def test_query_limits_and_cursor_validation(knowledge_harness):
    h = knowledge_harness
    tom = h.person("Tom")
    source = h.source(tom)
    h.capture_text("Alex reist.", source)
    with pytest.raises(ValidationError):
        RecallQuery(limit=0)
    with pytest.raises(ValidationError):
        RecallQuery(limit=51)
    with pytest.raises(ValidationError):
        RecallQuery(roles=("boss",))
    page = h.service.list_statements(cursor=None, limit=1, context=h.admin_context())
    assert len(page.items) == 1
    assert page.next_cursor is None
    with pytest.raises(ValidationError):
        h.service.list_statements(cursor="bogus:1", limit=1, context=h.admin_context())


def test_admin_inspection_requires_owner_and_admin_purpose(knowledge_harness):
    h = knowledge_harness
    tom = h.person("Tom")
    source = h.source(tom)
    h.capture_text("Alex reist.", source)
    reader = h.read_context(tom, purpose="reply")
    with pytest.raises(Exception):
        h.service._require_admin_read(reader)  # noqa: SLF001 - contract check
    admin_reader = h.read_context(
        tom, purpose="admin", owner=True, recipients={h.principal_for(tom)}
    )
    assert h.service._require_admin_read(admin_reader) is admin_reader  # noqa: SLF001


def test_empty_result_is_not_a_storage_error(knowledge_harness):
    h = knowledge_harness
    tom = h.person("Tom")
    context = h.read_context(tom)
    result = h.recall_text("nothing matches this", context)
    assert result.statement_ids == ()
    assert result.reason in ("empty", "ok")
    assert h.provider_spy.saw("")  # the read completed and produced empty text


def test_recall_only_ranks_permitted_candidates(knowledge_harness):
    """A forbidden statement must not influence the ranking of permitted ones."""
    h = knowledge_harness
    tom, alex, maria = (h.person(n) for n in ("Tom", "Alex", "Maria"))
    wide = {h.principal_for(tom), h.principal_for(alex), h.principal_for(maria)}
    narrow = {h.principal_for(tom), h.principal_for(alex)}
    shared = h.source(tom, audience=wide)
    restricted = h.source(tom, audience=narrow)
    allowed = h.capture_text("alpha-beta-gamma", shared).statement_ids[0]
    h.capture_text("alpha-beta-gamma forbidden extra", restricted)

    # Marie reads with a three-member audience: only the wide statement qualifies.
    context = h.read_context(tom, recipients=wide)
    result = h.recall_text("alpha beta gamma", context)
    assert result.statement_ids == (allowed,)
    assert "forbidden" not in result.text


def test_profile_uses_the_same_gate_as_recall(knowledge_harness):
    h = knowledge_harness
    tom, alex, maria = (h.person(n) for n in ("Tom", "Alex", "Maria"))
    source = h.source(tom, audience={h.principal_for(tom), h.principal_for(alex)})
    h.capture_text("Alex reist.", source, subjects=(alex,))
    denied = h.read_context(
        tom,
        recipients={h.principal_for(tom), h.principal_for(alex), h.principal_for(maria)},
    )
    allowed = h.read_context(
        tom, recipients={h.principal_for(tom), h.principal_for(alex)}
    )
    assert h.service.profile(alex, context=denied).context.statement_ids == ()
    assert h.service.profile(alex, context=allowed).context.statement_ids
