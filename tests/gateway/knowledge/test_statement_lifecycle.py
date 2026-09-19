"""P2.3: revocation, correction, expiry and erasure across every projection."""

from __future__ import annotations

import pytest

from yeoman_gateway.knowledge.models import (
    KnowledgeError,
    StatementCandidate,
    ValidationError,
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
