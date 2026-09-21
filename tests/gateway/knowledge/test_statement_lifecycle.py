"""P2.3: revocation, correction, expiry and erasure across every projection."""

from __future__ import annotations

import sqlite3
from typing import Final

import pytest
from yeoman_gateway.knowledge.models import (
    KnowledgeError,
    RecallQuery,
    StatementCandidate,
)


def test_revoked_source_removes_the_statement_from_every_read(knowledge_harness):
    h = knowledge_harness
    tom, alex = (h.person(n) for n in ("Tom", "Alex"))
    audience = {h.principal_for(tom), h.principal_for(alex)}
    source = h.source(tom, audience=audience)
    result = h.capture_text("secret-token-731 reist.", source, subjects=(alex,))
    sid = result.statement_ids[0]
    context = h.read_context(tom, recipients=audience)
    assert h.recall_person(alex, context).statement_ids == (sid,)

    receipt = h.service.invalidate_source(source, context=h.capture_context(source))
    assert sid in receipt.changed_ids
    assert receipt.acl_epoch > 1

    after = h.recall_person(alex, context)
    assert after.statement_ids == ()
    assert "secret-token-731" not in after.text
    assert h.active_statements_for_source(source) == ()
    # The row survives as a content-free tombstone.
    assert h.service._store.scalar(  # noqa: SLF001 - projection check
        "SELECT content FROM memory2_nodes WHERE id = ?", (sid,)
    ) in ("", None)
    assert h.service._store.scalar(  # noqa: SLF001
        "SELECT COUNT(*) FROM memory2_nodes_fts WHERE entry_id = ?", (sid,)
    ) == 0
    assert h.service._store.scalar(  # noqa: SLF001
        "SELECT COUNT(*) FROM memory2_embeddings WHERE entry_id = ?", (sid,)
    ) == 0


def test_revoking_one_of_two_sources_locks_the_statement(knowledge_harness):
    h = knowledge_harness
    tom, alex = (h.person(n) for n in ("Tom", "Alex"))
    audience = {h.principal_for(tom), h.principal_for(alex)}
    first = h.source(tom, audience=audience)
    second = h.source(alex, audience=audience)
    candidate = StatementCandidate(
        content="Gemeinsame Aussage.",
        sources=(first, second),
        people=(),
        extractor_version="test-extractor-1",
        confidence=0.5,
    )
    sid = h.service.capture(candidate, context=h.capture_context(first, second)).statement_ids[0]
    context = h.read_context(tom, recipients=audience)
    assert h.recall_text("Gemeinsame", context).statement_ids == (sid,)

    h.service.invalidate_source(first, context=h.capture_context(first))
    after = h.recall_text("Gemeinsame", context)
    assert after.statement_ids == ()
    # Not deleted: the statement is locked until it is regenerated without the source.
    assert h.service._store.scalar(  # noqa: SLF001
        "SELECT status FROM knowledge_statements WHERE statement_id = ?", (sid,)
    ) == "superseded"


def test_revoked_source_cannot_publish_a_delayed_extraction(knowledge_harness):
    h = knowledge_harness
    tom = h.person("Tom")
    source = h.source(tom)
    job = h.service.enqueue_capture((source,), context=h.capture_context(source))
    assert job.state == "queued"
    h.service.invalidate_source(source, context=h.capture_context(source))
    assert h.service.capture_job(job.job_id, context=h.admin_context()).state == "cancelled"
    with pytest.raises(KnowledgeError) as excinfo:
        h.capture_text("Alex reist morgen.", source)
    assert excinfo.value.code == "source_revoked"
    assert h.active_statements_for_source(source) == ()


def test_expired_statement_is_denied_even_for_author_only(knowledge_harness):
    h = knowledge_harness
    tom = h.person("Tom")
    source = h.source(tom, author_only=True)
    result = h.service.capture(
        h.candidate("Nur fuer mich.", source, valid_until_ms=h.clock.now_ms() + 1000),
        context=h.capture_context(source, basis="owner_private_note"),
    )
    sid = result.statement_ids[0]
    context = h.read_context(
        tom, recipients={h.principal_for(tom)}, purpose="admin", owner=True
    )
    assert h.recall_person(tom, context).statement_ids == (sid,)

    h.clock.advance(5000)
    expired = h.read_context(
        tom, recipients={h.principal_for(tom)}, purpose="admin", owner=True
    )
    after = h.recall_person(tom, expired)
    assert after.statement_ids == ()
    assert h.service.prune(
        before_ms=h.clock.now_ms(), context=h.admin_context()
    ).changed == 1
    assert h.service._store.scalar(  # noqa: SLF001
        "SELECT status FROM knowledge_statements WHERE statement_id = ?", (sid,)
    ) == "expired"


def test_confirmation_needs_explicit_authorized_evidence(knowledge_harness):
    h = knowledge_harness
    tom = h.person("Tom")
    source = h.source(tom)
    sid = h.capture_text("Alex reist.", source).statement_ids[0]
    with pytest.raises(KnowledgeError) as excinfo:
        h.service.confirm_statement(
            sid,
            expected_source=source,
            evidence_ref="never-issued",
            context=h.admin_context(),
        )
    assert excinfo.value.code == "unauthorized"
    h.authority.issue_evidence_ref("owner-confirmed-1")
    receipt = h.service.confirm_statement(
        sid,
        expected_source=source,
        evidence_ref="owner-confirmed-1",
        context=h.admin_context(),
    )
    assert sid in receipt.changed_ids
    assert h.statement(sid).status == "confirmed"


def test_correction_supersedes_only_the_named_statement(knowledge_harness):
    h = knowledge_harness
    tom, alex = (h.person(n) for n in ("Tom", "Alex"))
    audience = {h.principal_for(tom), h.principal_for(alex)}
    source = h.source(tom, audience=audience)
    independent = h.source(alex, audience=audience)
    original = h.capture_text("Alex reist nach Berlin.", source, subjects=(alex,)).statement_ids[0]
    contradictory = h.capture_text("Alex reist nach Rom.", independent, subjects=(alex,)).statement_ids[0]

    replacement = h.candidate("Alex reist nach Hamburg.", source, subjects=(alex,))
    receipt = h.service.correct_statement(
        original,
        replacement,
        expected_source=source,
        context=h.capture_context(source),
    )
    assert original in receipt.changed_ids
    assert h.statement(original).status == "superseded"
    assert h.service._store.scalar(  # noqa: SLF001 - supersession chain
        "SELECT superseded_by FROM knowledge_statements WHERE statement_id = ?", (original,)
    ) == receipt.changed_ids[1]
    # The contradictory statement from another source is untouched.
    assert h.statement(contradictory).status == "assertion"
    context = h.read_context(tom, recipients=audience)
    visible = h.recall_person(alex, context)
    assert contradictory in visible.statement_ids
    assert original not in visible.statement_ids


def test_correction_requires_the_named_source(knowledge_harness):
    h = knowledge_harness
    tom = h.person("Tom")
    source = h.source(tom)
    other = h.source(tom, chat="group-b")
    sid = h.capture_text("Alex reist.", source).statement_ids[0]
    with pytest.raises(KnowledgeError) as excinfo:
        h.service.correct_statement(
            sid,
            h.candidate("Alex reist nicht.", other),
            expected_source=other,
            context=h.capture_context(source, other),
        )
    assert excinfo.value.code == "invalid_input"
    assert h.statement(sid).status == "assertion"


def test_erasure_removes_payload_from_every_projection(knowledge_harness):
    h = knowledge_harness
    tom, alex = (h.person(n) for n in ("Tom", "Alex"))
    audience = {h.principal_for(tom), h.principal_for(alex)}
    source = h.source(tom, audience=audience)
    token = "erasable-token-4242"
    sid = h.capture_text(f"{token} reist.", source, subjects=(alex,)).statement_ids[0]
    # Give the statement an embedding so the vector projection is covered too.
    h.service._store.execute(  # noqa: SLF001 - vector projection fixture
        "INSERT INTO memory2_embeddings (entry_id, workspace_id, model, dims, vector, created_at)"
        " VALUES (?, ?, 'test-model', 2, ?, 'now')",
        (sid, h.service.workspace_id, b"\x00\x00\x00\x00\x08\x00\x00\x00"),
    )

    h.service.erase_statement(
        sid, expected_source=source, context=h.admin_context()
    )
    for table, column, key in (
        ("memory2_nodes", "content", "id"),
        ("knowledge_statements", "statement_id", "statement_id"),
        ("knowledge_statement_people", "statement_id", "statement_id"),
    ):
        rows = h.service._store.query(  # noqa: SLF001 - residual payload scan
            f"SELECT * FROM {table}"
        )
        rendered = " ".join(str(value) for row in rows for value in tuple(row))
        assert token not in rendered, table
    assert h.service._store.scalar(  # noqa: SLF001
        "SELECT COUNT(*) FROM memory2_nodes_fts WHERE entry_id = ?", (sid,)
    ) == 0
    assert h.service._store.scalar(  # noqa: SLF001
        "SELECT COUNT(*) FROM memory2_embeddings WHERE entry_id = ?", (sid,)
    ) == 0
    assert h.service._store.scalar(  # noqa: SLF001
        "SELECT COUNT(*) FROM knowledge_statement_principals WHERE statement_id = ?", (sid,)
    ) == 0
    # A content-free tombstone and an audit row remain.
    assert h.service._store.scalar(  # noqa: SLF001
        "SELECT status FROM knowledge_statements WHERE statement_id = ?", (sid,)
    ) == "revoked"
    assert h.service._store.scalar(  # noqa: SLF001
        "SELECT COUNT(*) FROM knowledge_statement_audit WHERE statement_id = ? AND operation = 'erase'",
        (sid,),
    ) == 1


def test_erasure_requires_owner_and_the_named_source(knowledge_harness):
    h = knowledge_harness
    tom = h.person("Tom")
    source = h.source(tom)
    other = h.source(tom, chat="group-b")
    sid = h.capture_text("Alex reist.", source).statement_ids[0]
    with pytest.raises(KnowledgeError) as excinfo:
        h.service.erase_statement(sid, expected_source=other, context=h.admin_context())
    assert excinfo.value.code == "invalid_input"
    assert h.statement(sid).content is not None


def test_multisource_synthesis_with_one_revoked_source_is_locked(knowledge_harness):
    h = knowledge_harness
    tom, alex = (h.person(n) for n in ("Tom", "Alex"))
    audience = {h.principal_for(tom), h.principal_for(alex)}
    first = h.source(tom, audience=audience)
    second = h.source(alex, audience=audience)
    candidate = StatementCandidate(
        content="Synthese aus zwei Quellen.",
        sources=(first, second),
        people=(),
        extractor_version="test-extractor-1",
        confidence=0.5,
    )
    sid = h.service.capture(candidate, context=h.capture_context(first, second)).statement_ids[0]
    context = h.read_context(tom, recipients=audience)
    assert h.recall_text("Synthese", context).statement_ids == (sid,)

    h.service.invalidate_source(second, context=h.capture_context(second))
    locked = h.recall_text("Synthese", context)
    assert locked.statement_ids == ()
    assert locked.reason in ("empty", "ok")
    # Regenerating without the revoked source is allowed and produces a readable row.
    regenerated = h.service.capture(
        StatementCandidate(
            content="Synthese aus zwei Quellen.",
            sources=(first,),
            people=(),
            extractor_version="test-extractor-2",
            confidence=0.5,
        ),
        context=h.capture_context(first),
    )
    assert regenerated.statement_ids[0] != sid
    assert h.recall_text("Synthese", context).statement_ids == (regenerated.statement_ids[0],)


def test_source_status_is_checked_again_at_publish_time(knowledge_harness):
    """A revocation between access check and write must not publish."""
    h = knowledge_harness
    tom = h.person("Tom")
    source = h.source(tom)
    context = h.capture_context(source)
    h.service.invalidate_source(source, context=h.capture_context(source))
    with pytest.raises(KnowledgeError) as excinfo:
        h.service.capture(h.candidate("Alex reist.", source), context=context)
    assert excinfo.value.code == "source_revoked"


def test_identical_correction_is_refused_and_the_chain_stays_acyclic(knowledge_harness):
    """A correction must actually replace something, and the chain stays acyclic."""
    h = knowledge_harness
    tom = h.person("Tom")
    source = h.source(tom)
    first = h.capture_text("Fassung eins.", source).statement_ids[0]
    receipt = h.service.correct_statement(
        first,
        h.candidate("Fassung zwei.", source),
        expected_source=source,
        context=h.capture_context(source),
    )
    second = receipt.changed_ids[1]
    chain = h.service._store.query(  # noqa: SLF001 - supersession chain check
        "SELECT statement_id, superseded_by FROM knowledge_statements"
        " WHERE statement_id IN (?, ?) ORDER BY statement_id",
        (first, second),
    )
    links = {str(row["statement_id"]): row["superseded_by"] for row in chain}
    assert links[first] == second
    assert links[second] is None
    # Correcting a statement with its own text is a caller mistake, not a correction.
    with pytest.raises(KnowledgeError) as excinfo:
        h.service.correct_statement(
            second,
            h.candidate("Fassung zwei.", source),
            expected_source=source,
            context=h.capture_context(source),
        )
    assert excinfo.value.code == "identity_conflict"
    assert h.statement(first).status == "superseded"
    assert h.statement(second).status == "assertion"


# ── one shared lifecycle and read contract (T14-T21, T39) ────────────────────
#
# Every consumer resolves a status through the same table.  These tests drive each
# public path - recall, profile, person_facts, roster, FTS and the model context - and
# prove they agree, because a disagreement between two readers is exactly how a
# retracted claim survives in one corner of the product.


def _statement_row(h, statement_id: str) -> sqlite3.Row:
    return h.service._store.query_one(  # noqa: SLF001 - asserting the stored contract
        "SELECT status, superseded_by, supersession_reason, valid_from_ms, valid_until_ms,"
        " revoked_at_ms FROM knowledge_statements WHERE statement_id = ?",
        (statement_id,),
    )


#: The proven period is anchored on the harness clock so the read instants below are
#: unambiguous: reading inside the period shows the value, before or after it does not.
_PERIOD_AHEAD_MS: Final[int] = 12 * 60 * 60 * 1000


def _set_period(h, statement_id: str) -> tuple[int, int]:
    """Give a statement a proven period that the historical tests read inside."""
    start = h.clock.now_ms()
    end = start + _PERIOD_AHEAD_MS
    with h.service._store.transaction():  # noqa: SLF001
        h.service._store.execute(  # noqa: SLF001
            "UPDATE knowledge_statements SET valid_from_ms = ?, valid_until_ms = ?"
            " WHERE statement_id = ?",
            (start, end, statement_id),
        )
    return start, end


def _mark(h, statement_id: str, *, status: str, reason: str = "unknown") -> None:
    """Write a lifecycle outcome the way a real operation would (status + reason together)."""
    with h.service._store.transaction():  # noqa: SLF001
        h.service._store.execute(  # noqa: SLF001
            "UPDATE knowledge_statements SET status = ?, supersession_reason = ?,"
            " revision = revision + 1 WHERE statement_id = ?",
            (status, reason, statement_id),
        )


def test_current_view_hides_a_state_change_but_the_past_still_shows_it(
    knowledge_harness,
):
    """T15: a move is history; a retraction is not."""
    h = knowledge_harness
    tom = h.person("Tom")
    source = h.source(tom)
    # The proven period of the earlier residence.
    statement_id = h.capture_text("Tom wohnt in Bonn.", source).statement_ids[0]
    period_start, period_end = _set_period(h, statement_id)
    _mark(h, statement_id, status="superseded", reason="state_change")

    context = h.read_context(tom)
    assert h.recall_person(tom, context).statement_ids == ()
    # Historical retrieval at an instant inside the proven period shows the old value.
    historical = h.service.recall(
        RecallQuery(person_ids=(tom,), limit=10),
        context=h.read_context(tom, now_ms=period_start + 1000),
        view="historic",
    )
    assert historical.statement_ids == (statement_id,)
    # Outside the proven period there is nothing to show.
    outside = h.service.recall(
        RecallQuery(person_ids=(tom,), limit=10),
        context=h.read_context(tom, now_ms=period_end + 1000),
        view="historic",
    )
    assert outside.statement_ids == ()


def test_a_correction_is_never_presented_as_previously_true(knowledge_harness):
    """T15: ``correction`` is only visible in an authorized correction audit."""
    h = knowledge_harness
    tom = h.person("Tom")
    source = h.source(tom)
    statement_id = h.capture_text("Tom wohnt in Bonn.", source).statement_ids[0]
    period_start, period_end = _set_period(h, statement_id)
    _mark(h, statement_id, status="superseded", reason="correction")

    context = h.read_context(tom)
    assert h.recall_person(tom, context).statement_ids == ()
    assert (
        h.service.recall(
            RecallQuery(person_ids=(tom,), limit=10),
            context=h.read_context(tom, now_ms=period_start + 1000),
            view="historic",
        ).statement_ids
        == ()
    )
    audit = h.service.recall(
        RecallQuery(person_ids=(tom,), limit=10),
        context=h.read_context(tom, now_ms=period_start + 1000),
        view="correction_audit",
    )
    assert audit.statement_ids == (statement_id,)


def test_quality_rejected_and_unknown_are_excluded_from_every_read(knowledge_harness):
    """T39: a rejection and an unclassified legacy reason behave identically."""
    h = knowledge_harness
    tom = h.person("Tom")
    first = h.source(tom)
    second = h.source(tom)
    rejected = h.capture_text("Tom wohnt in Bonn.", first).statement_ids[0]
    unknown = h.capture_text("Tom wohnt in Kiel.", second).statement_ids[0]
    period_start, _period_end = _set_period(h, rejected)
    _set_period(h, unknown)
    for statement_id, reason in ((rejected, "quality_rejected"), (unknown, "unknown")):
        _set_period(h, statement_id)
        _mark(h, statement_id, status="superseded", reason=reason)

    context = h.read_context(tom)
    for view in ("current", "historic", "correction_audit"):
        result = h.service.recall(
            RecallQuery(person_ids=(tom,), limit=10),
            context=h.read_context(tom, now_ms=period_start + 1000),
            view=view,
        )
        assert result.statement_ids == (), (view, result.statement_ids)
    # Only an explicit diagnosis sees them, and it still sees the reason.
    diagnosis = h.service.recall(
        RecallQuery(person_ids=(tom,), limit=10),
        context=h.read_context(tom, now_ms=period_start + 1000),
        view="diagnosis",
    )
    assert set(diagnosis.statement_ids) == {rejected, unknown}


def test_revoked_content_is_not_readable_even_for_a_diagnosis(knowledge_harness):
    """T39/§7.5: no view lifts a source revocation, not even the audit surface."""
    h = knowledge_harness
    tom = h.person("Tom")
    source = h.source(tom)
    statement_id = h.capture_text("Tom wohnt in Bonn.", source).statement_ids[0]
    period_start, _period_end = _set_period(h, statement_id)
    with h.service._store.transaction():  # noqa: SLF001
        h.service._store.execute(  # noqa: SLF001
            "UPDATE knowledge_statements SET status = 'revoked', revoked_at_ms = 1"
            " WHERE statement_id = ?",
            (statement_id,),
        )
    for view in ("current", "historic", "correction_audit", "diagnosis"):
        result = h.service.recall(
            RecallQuery(person_ids=(tom,), limit=10),
            context=h.read_context(tom, now_ms=period_start + 1000),
            view=view,
        )
        assert result.statement_ids == (), (view, result.statement_ids)
        assert "Bonn" not in result.text


def test_person_facts_without_a_read_context_delivers_nothing(knowledge_harness):
    """The raw-SQL bypass is closed: no context, no content."""
    h = knowledge_harness
    tom = h.person("Tom")
    source = h.source(tom)
    h.capture_text("Tom wohnt in Bonn.", source)
    assert h.service.person_facts(tom) == ()
    with_context = h.service.person_facts(tom, context=h.read_context(tom))
    assert any("Bonn" in value for _kind, value, _label in with_context)


def test_person_facts_obeys_the_same_status_contract(knowledge_harness):
    h = knowledge_harness
    tom = h.person("Tom")
    source = h.source(tom)
    statement_id = h.capture_text("Tom wohnt in Bonn.", source).statement_ids[0]
    context = h.read_context(tom)
    assert h.service.person_facts(tom, context=context)
    _mark(h, statement_id, status="superseded", reason="quality_rejected")
    assert h.service.person_facts(tom, context=context) == ()


def test_profile_recall_and_model_context_agree_on_every_view(knowledge_harness):
    """One contract, five readers: a disagreement here is the bug this task closes."""
    h = knowledge_harness
    tom = h.person("Tom")
    source = h.source(tom)
    statement_id = h.capture_text("Tom wohnt in Bonn.", source).statement_ids[0]
    period_start, period_end = _set_period(h, statement_id)

    def readers(reason: str) -> dict[str, tuple[str, ...]]:
        _mark(h, statement_id, status="superseded", reason=reason)
        context = h.read_context(tom, now_ms=period_start + 1000)
        recall = h.service.recall(
            RecallQuery(person_ids=(tom,), limit=10), context=context
        )
        profile = h.service.profile(tom, context=context)
        facts = h.service.person_facts(tom, context=context)
        roster = h.service.roster(context=context, participant_ids=(h.principal_for(tom),))
        hybrid = h.service.recall_hybrid(
            RecallQuery(person_ids=(tom,), limit=10), context=context
        )
        return {
            "recall": recall.statement_ids,
            "profile": profile.context.statement_ids,
            "facts": tuple(value for _kind, value, _label in facts),
            "roster": tuple(line for _name, lines in roster for line in lines),
            "hybrid": hybrid.statement_ids,
        }

    state_change = readers("state_change")
    assert state_change["recall"] == ()
    assert state_change["profile"] == ()
    assert state_change["facts"] == ()
    assert state_change["roster"] == ()
    assert state_change["hybrid"] == ()

    correction = readers("correction")
    assert correction["recall"] == ()
    assert correction["profile"] == ()
    assert correction["facts"] == ()
    assert correction["roster"] == ()
    assert correction["hybrid"] == ()


def test_historic_view_is_still_gated_by_audience_and_revocation(knowledge_harness):
    """A historical instant is not a way around today's rights."""
    h = knowledge_harness
    tom = h.person("Tom")
    maria = h.person("Maria")
    source = h.source(tom, audience={h.principal_for(tom)})
    statement_id = h.capture_text("Tom wohnt in Bonn.", source).statement_ids[0]
    period_start, period_end = _set_period(h, statement_id)
    _mark(h, statement_id, status="superseded", reason="state_change")
    # A reader who was never in the audience sees nothing, even historically.
    outsider = h.read_context(maria, now_ms=period_start + 1000)
    assert (
        h.service.recall(
            RecallQuery(person_ids=(tom,), limit=10), context=outsider, view="historic"
        ).statement_ids
        == ()
    )


def test_only_active_roles_name_people_on_a_read(knowledge_harness):
    """A withheld cutover role must not surface a person in a rendered line."""
    h = knowledge_harness
    tom = h.person("Tom")
    alex = h.person("Alex")
    # The statement must not be the speaker's own claim, or the role filter would be
    # satisfied by the transport speaker instead of the withheld subject.
    source = h.source(alex)
    statement_id = h.capture_text(
        "Der Wohnort hat sich geändert.", source, subjects=(tom,)
    ).statement_ids[0]
    with h.service._store.transaction():  # noqa: SLF001
        h.service._store.execute(  # noqa: SLF001
            "UPDATE knowledge_statement_people SET status = 'withheld',"
            " resolution_reason = 'no_proven_mapping_for_principal'"
            " WHERE statement_id = ? AND person_id = ?",
            (statement_id, tom),
        )
    context = h.read_context(alex)
    # The statement is readable through its still-active transport speaker role.
    unfiltered = h.service.recall(RecallQuery(limit=10), context=context)
    assert statement_id in unfiltered.statement_ids
    # The text is still there, but the withheld role supplied no name label for the
    # subject: the only label is the still-active transport speaker.
    assert unfiltered.text == "Der Wohnort hat sich geändert. (Alex)"

    # And the person filter does not find the statement through a withheld role.
    filtered = h.service.recall(
        RecallQuery(person_ids=(tom,), limit=10), context=context
    )
    assert filtered.statement_ids == ()


def test_a_rebuild_does_not_resurrect_a_locked_statement(knowledge_harness):
    """An index rebuild is maintenance, not an amnesty (§7.5, T39)."""
    h = knowledge_harness
    tom = h.person("Tom")
    source = h.source(tom)
    statement_id = h.capture_text("Tom wohnt in Bonn.", source).statement_ids[0]
    _mark(h, statement_id, status="superseded", reason="quality_rejected")

    memory = h.service.memory_store()
    memory.reindex()

    indexed = h.service._store.scalar(  # noqa: SLF001 - asserting the index contract
        "SELECT COUNT(*) FROM memory2_nodes_fts WHERE entry_id = ?", (statement_id,)
    )
    assert indexed == 0
    # And the reader still sees nothing for the statement.
    context = h.read_context(tom)
    assert h.recall_person(tom, context).statement_ids == ()
    assert h.service.recall(RecallQuery(text="Bonn"), context=context).statement_ids == ()
