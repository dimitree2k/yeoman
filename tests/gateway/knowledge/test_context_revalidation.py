"""P3.2: a prepared context is re-checked before prompt and before output."""

from __future__ import annotations

from yeoman_gateway.knowledge.models import (
    KnowledgeContext,
    RecallQuery,
)

FORBIDDEN = "private-itinerary"


def _prepare(h, reader, chat="group-a", recipients=None):
    tom, alex = h.person("Tom"), h.person("Alex")
    audience = {h.principal_for(tom), h.principal_for(alex)}
    source = h.source(tom, chat=chat, audience=audience)
    sid = h.capture_text(f"{FORBIDDEN} reist.", source, subjects=(alex,)).statement_ids[0]
    context = h.read_context(
        reader if reader != "tom" else tom,
        chat=chat,
        recipients=recipients or audience,
    )
    return tom, alex, source, sid, context


def test_membership_change_invalidates_prepared_context(knowledge_harness):
    h = knowledge_harness
    tom, alex, source, sid, before = _prepare(h, "tom")
    prepared = h.recall_person(alex, before)
    assert prepared.statement_ids == (sid,)
    after = h.add_recipient(before, h.person("Nadia"))
    checked = h.service.revalidate(prepared, context=after)
    assert checked.statement_ids == ()
    assert FORBIDDEN not in checked.text
    assert checked.reason in ("stale_context", "empty")


def test_revocation_between_generation_and_output_drops_the_draft(knowledge_harness):
    h = knowledge_harness
    tom, alex, source, sid, context = _prepare(h, "tom")
    prepared = h.recall_person(alex, context)
    assert prepared.statement_ids == (sid,)
    h.service.invalidate_source(source, context=h.capture_context(source))
    checked = h.service.revalidate(prepared, context=context)
    assert checked.statement_ids == ()
    assert checked.text == ""
    assert FORBIDDEN not in checked.text


def test_policy_epoch_change_invalidates_prepared_context(knowledge_harness):
    h = knowledge_harness
    tom, alex, source, sid, context = _prepare(h, "tom")
    prepared = h.recall_person(alex, context)
    h.policy.policy_revision += 1
    stale_context = h.read_context(
        tom,
        recipients={h.principal_for(tom), h.principal_for(alex)},
        policy_revision=1,
    )
    checked = h.service.revalidate(prepared, context=stale_context)
    assert checked.statement_ids == ()
    assert checked.reason == "stale_policy_revision"


def test_acl_epoch_change_is_detected_even_without_membership_change(knowledge_harness):
    h = knowledge_harness
    tom, alex, source, sid, context = _prepare(h, "tom")
    prepared = h.recall_person(alex, context)
    assert prepared.acl_epoch == h.acl_epoch()
    # A revocation anywhere bumps the epoch; the prepared context records the old one.
    other = h.source(tom, chat="group-a", audience={h.principal_for(tom), h.principal_for(alex)})
    h.capture_text("another claim", other)
    h.service.invalidate_source(other, context=h.capture_context(other))
    assert h.acl_epoch() > prepared.acl_epoch
    checked = h.service.revalidate(prepared, context=context)
    # Native contexts fence the complete prepared draft against the ACL epoch.
    assert checked.statement_ids == ()
    assert checked.reason == "stale_context"
    assert checked.acl_epoch == h.acl_epoch()
    fresh_context = h.read_context(
        tom, recipients={h.principal_for(tom), h.principal_for(alex)}
    )
    assert h.recall_person(alex, fresh_context).statement_ids == (sid,)


def test_native_revalidation_discards_changed_draft_and_fresh_recall_keeps_allowed_text(knowledge_harness):
    h = knowledge_harness
    tom, alex = h.person("Tom"), h.person("Alex")
    audience = {h.principal_for(tom), h.principal_for(alex)}
    first = h.source(tom, audience=audience)
    second = h.source(tom, audience=audience)
    keep = h.capture_text("keep-token one", first).statement_ids[0]
    drop = h.capture_text("drop-token two", second).statement_ids[0]
    context = h.read_context(tom, recipients=audience)
    prepared = h.service.recall(RecallQuery(text="token"), context=context)
    assert set(prepared.statement_ids) == {keep, drop}
    h.service.invalidate_source(second, context=h.capture_context(second))
    checked = h.service.revalidate(prepared, context=context)
    assert checked.statement_ids == ()
    assert checked.reason == "stale_context"
    assert checked.text == ""
    fresh = h.service.recall(RecallQuery(text="token"), context=context)
    assert fresh.statement_ids == (keep,)
    assert "drop-token" not in fresh.text
    assert "keep-token" in fresh.text


def test_revalidate_without_statement_ids_is_empty_not_an_error(knowledge_harness):
    h = knowledge_harness
    tom = h.person("Tom")
    context = h.read_context(tom)
    empty = KnowledgeContext(
        text="", statement_ids=(), context_revision="whatever", acl_epoch=h.acl_epoch()
    )
    checked = h.service.revalidate(empty, context=context)
    assert checked.statement_ids == ()
    assert checked.reason in ("stale_context", "empty", "ok")


def test_context_revision_changes_when_the_basis_changes(knowledge_harness):
    h = knowledge_harness
    tom, alex = h.person("Tom"), h.person("Alex")
    audience = {h.principal_for(tom), h.principal_for(alex)}
    source = h.source(tom, audience=audience)
    h.capture_text("Alex reist.", source, subjects=(alex,))
    context = h.read_context(tom, recipients=audience)
    first = h.service.recall(RecallQuery(person_ids=(alex,)), context=context)
    second = h.service.recall(RecallQuery(person_ids=(alex,)), context=context)
    assert first.context_revision == second.context_revision
    changed = h.add_recipient(context, h.person("Nadia"))
    third = h.service.revalidate(first, context=changed)
    assert third.context_revision != first.context_revision


def test_owner_only_diagnostics_do_not_change_user_visible_behaviour(knowledge_harness):
    """Only an owner-authorized reader sees a denied count, and only in diagnostics."""
    h = knowledge_harness
    tom, alex = h.person("Tom"), h.person("Alex")
    owner = h.person_for("whatsapp:4910000000001", "Owner")
    audience = {h.principal_for(tom), h.principal_for(alex), h.principal_for(owner)}
    # Seed the narrower historical roster before the current owner-visible roster.
    narrow = h.source(tom, audience={h.principal_for(tom), h.principal_for(alex)})
    h.capture_text("hidden-claim", narrow, subjects=(alex,))
    source = h.source(tom, audience=audience)
    visible = h.capture_text("private-itinerary reist.", source, subjects=(alex,)).statement_ids[0]

    normal = h.read_context(tom, recipients=audience)
    assert h.recall_person(alex, normal).denied_count == 0
    as_owner = h.read_context(owner, recipients=audience, owner=True)
    owner_result = h.service.recall(RecallQuery(person_ids=(alex,)), context=as_owner)
    assert owner_result.denied_count >= 1, owner_result

    # A non-owner context never receives the diagnostic number, and no context
    # exposes the denied statements themselves.
    assert owner_result.statement_ids == (visible,)
    assert h.recall_person(alex, normal).statement_ids == (visible,)
    assert "hidden-claim" not in owner_result.text


def test_revalidate_rejects_a_forged_result_object(knowledge_harness):
    h = knowledge_harness
    tom = h.person("Tom")
    context = h.read_context(tom)
    forged = KnowledgeContext(
        text="forged text",
        statement_ids=("00000000-0000-4000-8000-000000000000",),
    )
    checked = h.service.revalidate(forged, context=context)
    assert checked.statement_ids == ()
    assert "forged text" not in checked.text


def test_prepared_context_is_bound_to_source_revisions(knowledge_harness):
    h = knowledge_harness
    tom, alex = h.person("Tom"), h.person("Alex")
    audience = {h.principal_for(tom), h.principal_for(alex)}
    source = h.source(tom, audience=audience, revision=1)
    h.capture_text("Alex reist.", source, subjects=(alex,))
    context = h.read_context(tom, recipients=audience)
    prepared = h.service.recall(RecallQuery(person_ids=(alex,)), context=context)
    assert prepared.source_refs == (source,)
    assert prepared.context_revision


def test_no_transport_attempt_for_a_stale_draft(knowledge_harness):
    """The output boundary must drop the whole draft, not filter single sentences."""
    h = knowledge_harness
    tom, alex, source, sid, context = _prepare(h, "tom")
    prepared = h.recall_person(alex, context)
    generated_draft = f"Sure: {prepared.text}"
    after = h.add_recipient(context, h.person("Nadia"))
    checked = h.service.revalidate(prepared, context=after)
    delivered = generated_draft if checked.statement_ids else ""
    assert delivered == ""
    assert FORBIDDEN not in delivered
