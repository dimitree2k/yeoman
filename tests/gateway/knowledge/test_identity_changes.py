"""P1.3: reversible merges that keep original ids, sources and rights."""

from __future__ import annotations

import pytest
from yeoman_gateway.knowledge.models import (
    KnowledgeError,
    TrustedAdminContext,
)


def test_merge_can_be_undone_without_moving_source_bindings(knowledge_harness):
    h = knowledge_harness
    a, b = h.two_people()
    original = h.original_bindings()
    principals_before = h.security_fingerprint()
    revision = h.identity_revision()
    op = h.service.merge_people(
        a, b, expected_revision=revision, context=h.admin_context()
    )
    assert op.identity_revision == revision + 1
    assert h.resolve_original(b).person_id == a
    assert h.original_bindings() == original
    # Both person rows survive; nothing was deleted or rewritten.
    assert h.snapshot_counts()["contacts"] == 2
    assert h.security_fingerprint() == principals_before

    h.service.undo_merge(
        op.operation_id, expected_revision=op.identity_revision, context=h.admin_context()
    )
    assert h.resolve_original(b).person_id == b
    assert h.original_bindings() == original
    assert h.security_fingerprint() == principals_before


def test_stale_revision_is_rejected_without_changes(knowledge_harness):
    h = knowledge_harness
    a, b = h.two_people()
    stale = h.identity_revision() - 1
    before = h.snapshot_counts()
    with pytest.raises(KnowledgeError) as excinfo:
        h.service.merge_people(a, b, expected_revision=stale, context=h.admin_context())
    assert excinfo.value.code == "stale_revision"
    assert h.snapshot_counts() == before
    assert h.identity_revision() == stale + 1


def test_self_merge_and_unknown_person_are_rejected(knowledge_harness):
    h = knowledge_harness
    a, b = h.two_people()
    with pytest.raises(KnowledgeError) as self_merge:
        h.service.merge_people(a, a, expected_revision=h.identity_revision(), context=h.admin_context())
    assert self_merge.value.code == "invalid_input"
    with pytest.raises(KnowledgeError) as unknown:
        h.service.merge_people(
            a, "00000000-0000-4000-8000-000000000000",
            expected_revision=h.identity_revision(), context=h.admin_context(),
        )
    assert unknown.value.code == "unresolved"


def test_merge_cycle_is_rejected(knowledge_harness):
    h = knowledge_harness
    a, b = h.two_people()
    first = h.service.merge_people(a, b, expected_revision=h.identity_revision(), context=h.admin_context())
    with pytest.raises(KnowledgeError) as cycle:
        h.service.merge_people(
            b, a, expected_revision=first.identity_revision, context=h.admin_context()
        )
    assert cycle.value.code in ("identity_conflict", "invalid_input")
    # The first redirect is untouched and still resolvable.
    assert h.resolve_original(b).person_id == a


def test_unauthorized_actor_cannot_merge(knowledge_harness):
    h = knowledge_harness
    a, b = h.two_people()
    before = h.snapshot_counts()
    outsider = TrustedAdminContext(
        actor_principal="whatsapp:4910000000777",
        policy_revision=1,
        authorization_ref="whatever",
        owner=True,
    )
    with pytest.raises(KnowledgeError) as excinfo:
        h.service.merge_people(a, b, expected_revision=h.identity_revision(), context=outsider)
    assert excinfo.value.code == "unauthorized"
    assert h.snapshot_counts() == before


def test_dependent_merge_blocks_undo_and_names_the_blocker(knowledge_harness):
    h = knowledge_harness
    a, b = h.two_people()
    c = h.person("Maria")
    # First "Maria is Alex" (c -> b), then "Alex is Tom" (b -> a): the second redirect
    # builds on the first one, because its source is that redirect's target.
    first = h.service.merge_people(
        b, c, expected_revision=h.identity_revision(), context=h.admin_context()
    )
    second = h.service.merge_people(
        a, b, expected_revision=first.identity_revision, context=h.admin_context()
    )
    assert h.resolve_original(c).person_id == a
    with pytest.raises(KnowledgeError) as blocked:
        h.service.undo_merge(
            first.operation_id,
            expected_revision=second.identity_revision,
            context=h.admin_context(),
        )
    assert blocked.value.code == "dependent_merge"
    # Exactly the operation that builds on the first redirect is named as the blocker.
    assert second.operation_id in str(blocked.value)
    assert first.operation_id not in str(blocked.value)
    assert h.resolve_original(c).person_id == a
    # Undoing the dependent operation first works, then the original one.
    h.service.undo_merge(
        second.operation_id,
        expected_revision=h.identity_revision(),
        context=h.admin_context(),
    )
    h.service.undo_merge(
        first.operation_id,
        expected_revision=h.identity_revision(),
        context=h.admin_context(),
    )
    assert h.resolve_original(b).person_id == b
    assert h.resolve_original(c).person_id == c


def test_three_step_chain_requires_reverse_order_undo(knowledge_harness):
    """A redirect whose target is redirected again cannot be undone first."""
    h = knowledge_harness
    a, b = h.two_people()
    c = h.person("Maria")
    d = h.person("Nadia")
    first = h.service.merge_people(
        b, c, expected_revision=h.identity_revision(), context=h.admin_context()
    )
    second = h.service.merge_people(
        a, b, expected_revision=first.identity_revision, context=h.admin_context()
    )
    third = h.service.merge_people(
        a, d, expected_revision=second.identity_revision, context=h.admin_context()
    )
    assert h.resolve_original(c).person_id == a
    with pytest.raises(KnowledgeError) as blocked:
        h.service.undo_merge(
            first.operation_id,
            expected_revision=h.identity_revision(),
            context=h.admin_context(),
        )
    assert blocked.value.code == "dependent_merge"
    assert second.operation_id in str(blocked.value)
    # The independent redirect may be undone at any time.
    h.service.undo_merge(
        third.operation_id, expected_revision=h.identity_revision(), context=h.admin_context()
    )
    h.service.undo_merge(
        second.operation_id, expected_revision=h.identity_revision(), context=h.admin_context()
    )
    h.service.undo_merge(
        first.operation_id, expected_revision=h.identity_revision(), context=h.admin_context()
    )
    assert h.resolve_original(c).person_id == c
    assert h.resolve_original(d).person_id == d


def test_independent_merges_can_be_undone_separately(knowledge_harness):
    """Two people merging into the same target do not depend on each other."""
    h = knowledge_harness
    a, b = h.two_people()
    c = h.person("Maria")
    first = h.service.merge_people(
        a, b, expected_revision=h.identity_revision(), context=h.admin_context()
    )
    second = h.service.merge_people(
        a, c, expected_revision=first.identity_revision, context=h.admin_context()
    )
    h.service.undo_merge(
        first.operation_id,
        expected_revision=second.identity_revision,
        context=h.admin_context(),
    )
    assert h.resolve_original(b).person_id == b
    assert h.resolve_original(c).person_id == a
    h.service.undo_merge(
        second.operation_id, expected_revision=h.identity_revision(), context=h.admin_context()
    )
    assert h.resolve_original(c).person_id == c


def test_merging_an_already_redirected_person_is_rejected(knowledge_harness):
    """A person with an active redirect is not a valid merge source again."""
    h = knowledge_harness
    a, b = h.two_people()
    c = h.person("Maria")
    h.service.merge_people(a, b, expected_revision=h.identity_revision(), context=h.admin_context())
    with pytest.raises(KnowledgeError) as excinfo:
        h.service.merge_people(
            c, b, expected_revision=h.identity_revision(), context=h.admin_context()
        )
    assert excinfo.value.code == "identity_conflict"
