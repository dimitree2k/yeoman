"""P1.3: reversible merges that keep original ids, sources and rights."""

from __future__ import annotations

import pytest
from yeoman_gateway.knowledge.models import (
    Identifier,
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


# ── temporal bindings, aliases and identifier resolution (T01-T10) ───────────
#
# Identity maintenance is an audited operation on a *concrete* row with an expected
# revision.  These tests use synthetic values only and never touch a live database.


def _binding_of(h, person_id: str, value: str):
    identifier = Identifier("whatsapp", "phone_jid", value)
    return h.service._identity.binding_for(identifier)  # noqa: SLF001 - row under test


def test_a_new_identifier_creates_exactly_one_stub(knowledge_harness):
    """T01: a parallel observation must not create a second active contact."""
    h = knowledge_harness
    first = h.observe("whatsapp", "phone_jid", "49180000001@s.whatsapp.net")
    second = h.observe("whatsapp", "phone_jid", "49180000001@s.whatsapp.net")
    assert first.person_id == second.person_id
    assert h.snapshot_counts()["contacts"] == 1
    active = h.service._store.query(  # noqa: SLF001 - white-box identity assertion
        "SELECT COUNT(*) AS n FROM knowledge_identifier_bindings WHERE status = 'active'"
    )
    assert int(active[0]["n"]) == 1


def test_a_verified_phone_lid_pair_shares_one_person_but_a_lid_is_not_a_number(
    knowledge_harness,
):
    """T02: the pair resolves together; a bare LID never becomes a phone number."""
    h = knowledge_harness
    phone = Identifier("whatsapp", "phone_jid", "49180000002@s.whatsapp.net")
    lid = Identifier("whatsapp", "lid", "22000000000001@lid")
    created = h.observe("whatsapp", "phone_jid", phone.value, mapping=True, extra=(lid,))

    via_phone = h.service.resolve_identifier(phone)
    via_lid = h.service.resolve_identifier(lid)
    assert via_phone.status == "resolved"
    assert via_lid.status == "resolved"
    assert via_phone.person_id == via_lid.person_id == created.person_id

    # A LID-only lookup yields the LID, never a synthesized phone number.
    assert via_lid.identifier is not None
    assert via_lid.identifier.kind == "lid"
    assert "@s.whatsapp.net" not in via_lid.identifier.value


def test_an_unproven_pair_is_refused_and_a_group_is_not_a_person_channel(
    knowledge_harness,
):
    """T03: no untyped number, no group, no newsletter is turned into a phone number."""
    h = knowledge_harness
    h.observe("whatsapp", "phone_jid", "49180000003@s.whatsapp.net")
    ambiguous = h.observe(
        "whatsapp",
        "phone_jid",
        "49180000003@s.whatsapp.net",
        mapping=False,
        extra=(Identifier("whatsapp", "lid", "22000000000003@lid"),),
    )
    assert ambiguous.status == "ambiguous"
    assert ambiguous.person_id is None

    # A bare number is not a phone JID: the caller has to type the identifier.
    bare = h.service.resolve_identifier(Identifier("whatsapp", "handle", "49180000003"))
    assert bare.status == "unresolved"
    # And no stub was minted for the refused candidates.
    assert h.snapshot_counts()["contacts"] == 1


def test_number_reassignment_ends_the_old_binding_and_keeps_history(knowledge_harness):
    """T09: a re-assigned number stops resolving; the earlier period stays readable."""
    h = knowledge_harness
    value = "49180000004@s.whatsapp.net"
    identifier = Identifier("whatsapp", "phone_jid", value)
    first = h.observe("whatsapp", "phone_jid", value, name="First Owner")
    assert first.person_id

    # The first claim gets an explicitly proven period, so history is checkable.
    start_ms = h.clock.now_ms()
    old_binding = _binding_of(h, first.person_id, value)
    assert old_binding is not None
    h.authority.issue_evidence_ref("admin-evidence-period")
    h.service.add_or_end_binding(
        person_id=first.person_id,
        identifier=identifier,
        evidence_ref="admin-evidence-period",
        context=h.admin_context(),
        expected_revision=h.identity_revision(),
        valid_from_ms=start_ms,
    )
    old_binding = _binding_of(h, first.person_id, value)
    assert old_binding is not None and old_binding.valid_from_ms == start_ms

    # A new person claims the number; the old binding is ended in the same operation.
    new_person = h.person_for("whatsapp:49180000099", "Second Owner")
    handover_ms = start_ms + 60_000
    h.authority.issue_evidence_ref("admin-evidence-reassign")
    receipt = h.service.add_or_end_binding(
        person_id=new_person,
        identifier=identifier,
        evidence_ref="admin-evidence-reassign",
        context=h.admin_context(),
        expected_revision=h.identity_revision(),
        valid_from_ms=handover_ms,
        end_binding_id=old_binding.binding_id,
        end_at_ms=handover_ms,
    )
    assert receipt.changed_ids

    # The hand-over ends the old binding and the new one is the active answer.
    ended = h.service._identity.binding_by_id(old_binding.binding_id)  # noqa: SLF001
    assert ended is not None and ended.status == "ended"
    assert ended.valid_until_ms == handover_ms
    assert h.service.resolve_identifier(identifier).person_id == new_person

    # A message from *before* the hand-over still resolves to the earlier person.
    historic = h.service.resolve_identifier(identifier, at_ms=start_ms + 1)
    assert historic.status == "resolved"
    assert historic.person_id == first.person_id


def test_ending_a_binding_makes_the_identifier_unresolved(knowledge_harness):
    h = knowledge_harness
    value = "49180000005@s.whatsapp.net"
    identifier = Identifier("whatsapp", "phone_jid", value)
    person = h.observe("whatsapp", "phone_jid", value)
    assert person.person_id
    binding = _binding_of(h, person.person_id, value)
    assert binding is not None
    h.service.end_binding(
        binding_id=binding.binding_id,
        context=h.admin_context(),
        expected_revision=h.identity_revision(),
    )
    assert h.service.resolve_identifier(identifier).status == "unresolved"
    # The principal no longer maps to a person either.
    assert h.service.person_for_principal("whatsapp:49180000005") is None
    # The row survives as audit history rather than being deleted.
    assert h.service._identity.binding_by_id(binding.binding_id).status == "ended"  # noqa: SLF001


def test_stale_revision_refuses_a_binding_change(knowledge_harness):
    h = knowledge_harness
    person = h.person("Tom")
    h.authority.issue_evidence_ref("admin-evidence-stale")
    before = h.snapshot_counts()
    with pytest.raises(KnowledgeError) as excinfo:
        h.service.add_or_end_binding(
            person_id=person,
            identifier=Identifier("whatsapp", "phone_jid", "49180000006@s.whatsapp.net"),
            evidence_ref="admin-evidence-stale",
            context=h.admin_context(),
            expected_revision=h.identity_revision() - 1,
        )
    assert excinfo.value.code == "stale_revision"
    assert h.snapshot_counts() == before


def test_binding_maintenance_requires_owner_authority(knowledge_harness):
    h = knowledge_harness
    person = h.person("Tom")
    h.authority.issue_evidence_ref("admin-evidence-noauth")
    context = TrustedAdminContext(
        actor_principal="whatsapp:4910000000001",
        policy_revision=1,
        authorization_ref="ref",
        owner=False,
    )
    with pytest.raises(KnowledgeError) as excinfo:
        h.service.add_or_end_binding(
            person_id=person,
            identifier=Identifier("whatsapp", "phone_jid", "49180000007@s.whatsapp.net"),
            evidence_ref="admin-evidence-noauth",
            context=context,
        )
    assert excinfo.value.code == "unauthorized"


def test_alias_needs_a_scope_and_is_not_an_address_by_default(knowledge_harness):
    """T05/T07: a name released in one context is not a global address."""
    h = knowledge_harness
    person = h.person("Vinzent")
    alias = h.service.observe_alias(
        person_id=person,
        name="Vinz",
        alias_kind="short_name",
        scope_key="channel:whatsapp:chat:group-a",
        evidence_ref="evidence-vinz",
        status="confirmed",
    )
    assert alias.status == "confirmed"
    assert alias.address_allowed is False
    assert h.service.address_aliases_of(person) == ()

    preferred = h.service.set_alias_preference(
        alias_id=alias.revision and _alias_id(h, person, "Vinz"),
        context=h.admin_context(),
        expected_revision=h.identity_revision(),
    )
    assert preferred.changed_ids == (person,)
    addressed = h.service.address_aliases_of(person, scope_key="channel:whatsapp:chat:group-a")
    assert [item.name for item in addressed] == ["Vinz"]
    # Another context is not covered by that preference.
    assert h.service.address_aliases_of(person, scope_key="channel:whatsapp:chat:other") == ()


def test_a_retired_alias_stops_addressing_but_stays_searchable(knowledge_harness):
    """T07: "bitte nicht mehr so nennen" withdraws the address, not the history."""
    h = knowledge_harness
    person = h.person("Wimsekt")
    alias = h.service.observe_alias(
        person_id=person,
        name="Wim",
        alias_kind="nickname",
        scope_key="channel:whatsapp:chat:group-a",
        evidence_ref="evidence-wim",
        status="confirmed",
        address_allowed=True,
    )
    alias_id = _alias_id(h, person, "Wim")
    assert h.service.address_aliases_of(person, scope_key="channel:whatsapp:chat:group-a")

    h.service.retire_alias(
        alias_id=alias_id,
        context=h.admin_context(),
        expected_revision=h.identity_revision(),
        reason="not_wanted",
    )
    assert h.service.address_aliases_of(person, scope_key="channel:whatsapp:chat:group-a") == ()
    # The name is still findable inside its existing rights.
    assert "Wim" in {
        item.name for item in h.service._identity.searchable_aliases_of(person)  # noqa: SLF001
    }
    assert alias.status == "confirmed"


def test_at_most_one_preferred_alias_per_context(knowledge_harness):
    """Two usable addresses, exactly one preference - and the demoted one keeps its row."""
    h = knowledge_harness
    person = h.person("Tom")
    scope = "channel:whatsapp:chat:group-a"
    for name in ("Tommy", "Tomas"):
        h.service.observe_alias(
            person_id=person,
            name=name,
            alias_kind="nickname",
            scope_key=scope,
            evidence_ref=f"evidence-{name}",
            status="confirmed",
            address_allowed=True,
        )
    h.service.set_alias_preference(
        alias_id=_alias_id(h, person, "Tommy"),
        context=h.admin_context(),
        expected_revision=h.identity_revision(),
    )
    h.service.set_alias_preference(
        alias_id=_alias_id(h, person, "Tomas"),
        context=h.admin_context(),
        expected_revision=h.identity_revision(),
    )
    addressed = h.service.address_aliases_of(person, scope_key=scope)
    # Both names stay usable as addresses; exactly one is the preference and sorts first.
    assert [(item.name, item.is_preferred) for item in addressed] == [
        ("Tomas", True),
        ("Tommy", False),
    ]
    # The demoted alias keeps its observation and its searchability.
    demoted = h.service._identity.alias_by_id(_alias_id(h, person, "Tommy"))  # noqa: SLF001
    assert demoted is not None
    assert demoted.status == "confirmed"
    assert demoted.findable is True


def test_owner_flag_alias_and_merge_never_change_rights(knowledge_harness):
    """T10: no identity operation widens policy authority, principals or quotas."""
    h = knowledge_harness
    a, b = h.two_people()
    before_acl = h.acl_epoch()

    def authorization_rows() -> tuple[tuple[str, str], ...]:
        return tuple(
            (str(row["id"]), str(row["is_owner"]))
            for row in h.service._store.query(  # noqa: SLF001 - authorization assertion
                "SELECT id, is_owner FROM contacts ORDER BY id"
            )
        ) + tuple(
            (str(row["principal_id"]), str(row["role"]))
            for row in h.service._store.query(  # noqa: SLF001
                "SELECT principal_id, role FROM knowledge_statement_principals"
                " ORDER BY principal_id, role"
            )
        )

    before = authorization_rows()
    h.service.observe_alias(
        person_id=a,
        name="Boss",
        alias_kind="nickname",
        scope_key="channel:whatsapp:chat:group-a",
        evidence_ref="evidence-boss",
        status="confirmed",
        address_allowed=True,
    )
    # A name that claims authority is still only a name.
    assert all(
        int(row["is_owner"]) == 0
        for row in h.service._store.query(  # noqa: SLF001
            "SELECT is_owner FROM contacts WHERE id = ?", (a,)
        )
    )
    h.service.merge_people(a, b, expected_revision=h.identity_revision(), context=h.admin_context())
    assert authorization_rows() == before
    assert h.acl_epoch() == before_acl


def test_two_people_with_one_alias_stay_ambiguous(knowledge_harness):
    """T04: a shared alias never picks a person."""
    h = knowledge_harness
    first = h.person("Alex")
    second = h.person_for("whatsapp:4910000000099", "Alex Two")
    context = h.read_context("whatsapp:4910000000099")
    found = h.service.search_people("Alex", context=context)
    assert {item.person_id for item in found} == {first, second}


def _alias_id(h, person_id: str, name: str) -> int:
    row = h.service._store.query_one(  # noqa: SLF001 - locating the row under test
        "SELECT id FROM contact_aliases WHERE contact_id = ? AND alias = ?",
        (person_id, name),
    )
    assert row is not None, f"no alias {name!r} for {person_id}"
    return int(row["id"])
