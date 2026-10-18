"""Migrated group-notes batch excerpts are not their last sender's statements."""

from __future__ import annotations


def test_batch_excerpt_speaker_link_does_not_answer_a_person_lookup(knowledge_harness):
    h = knowledge_harness
    tom, alex = (h.person(n) for n in ("Tom", "Alex"))
    audience = {h.principal_for(tom), h.principal_for(alex)}
    source = h.source(tom, chat="group-a", audience=audience)
    own = h.capture_text("Tom faehrt im Mai nach Lissabon.", source).statement_ids[0]
    batch = h.capture_text(
        "[group_notes_batch] [4910000000002] Alex kauft ein Boot [4910000000001] cool",
        h.source(tom, chat="group-a", audience=audience),
    ).statement_ids[0]
    context = h.read_context(tom, chat="group-a", recipients=audience)

    assert h.recall_person(tom, context).statement_ids == (own,)
    # Topic search still reaches the excerpt.
    assert batch in h.recall_text("Boot", context).statement_ids


def test_batch_excerpt_keeps_explicit_subject_links(knowledge_harness):
    h = knowledge_harness
    tom, alex = (h.person(n) for n in ("Tom", "Alex"))
    audience = {h.principal_for(tom), h.principal_for(alex)}
    source = h.source(tom, chat="group-a", audience=audience)
    batch = h.capture_text(
        "[group_notes_batch] [4910000000002] Alex kauft ein Boot",
        source,
        subjects=(alex,),
    ).statement_ids[0]
    context = h.read_context(tom, chat="group-a", recipients=audience)

    assert h.recall_person(alex, context).statement_ids == (batch,)
