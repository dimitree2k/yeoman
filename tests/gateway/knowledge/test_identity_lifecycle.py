"""Focused lifecycle regressions for verified identity and alias maintenance."""

from yeoman_gateway.knowledge.models import Identifier, IdentifierBinding


def test_ended_binding_with_unknown_end_does_not_cover_any_instant():
    binding = IdentifierBinding(
        person_id="synthetic-person",
        identifier=Identifier("whatsapp", "phone_jid", "49180000040@s.whatsapp.net"),
        evidence_ref="synthetic-evidence",
        status="ended",
        valid_from_ms=1_000,
        valid_until_ms=0,
    )

    assert not binding.covers(1_000)


def test_repeated_verified_observation_refreshes_last_seen_without_inventing_start(
    knowledge_harness,
):
    h = knowledge_harness
    value = "49180000041@s.whatsapp.net"
    first_seen = h.clock.now_ms()
    first = h.observe("whatsapp", "phone_jid", value, mapping=True)
    assert first.person_id

    identifier = Identifier("whatsapp", "phone_jid", value)
    binding = h.service._identity.binding_for(identifier)  # noqa: SLF001
    assert binding is not None
    assert binding.valid_from_ms == 0
    assert binding.observed_at_ms == first_seen

    second_seen = h.clock.advance(1_000)
    h.observe("whatsapp", "phone_jid", value, mapping=True)
    binding = h.service._identity.binding_for(identifier)  # noqa: SLF001
    assert binding is not None
    assert binding.valid_from_ms == 0
    assert binding.observed_at_ms == second_seen

    h.clock.value_ms = first_seen + 500
    h.observe("whatsapp", "phone_jid", value, mapping=True)
    binding = h.service._identity.binding_for(identifier)  # noqa: SLF001
    assert binding is not None
    assert binding.valid_from_ms == 0
    assert binding.observed_at_ms == second_seen


def test_refresh_through_merge_preserves_original_binding_for_undo(knowledge_harness):
    h = knowledge_harness
    identifier = Identifier("whatsapp", "phone_jid", "49180000043@s.whatsapp.net")
    source = h.observe(
        identifier.channel, identifier.kind, identifier.value, mapping=True
    ).person_id
    target = h.person("Merge Target")
    assert source

    original = h.service._identity.binding_for(identifier)  # noqa: SLF001
    assert original is not None and original.person_id == source
    merge = h.service.merge_people(
        target,
        source,
        expected_revision=h.identity_revision(),
        context=h.admin_context(),
    )
    h.clock.advance(1_000)

    resolved_during_merge = h.observe(
        identifier.channel, identifier.kind, identifier.value, mapping=True
    )
    assert resolved_during_merge.person_id == target
    refreshed = h.service._identity.binding_for(identifier)  # noqa: SLF001
    assert refreshed is not None
    assert refreshed.person_id == source
    assert refreshed.evidence_ref == original.evidence_ref

    h.service.undo_merge(
        merge.operation_id,
        expected_revision=h.identity_revision(),
        context=h.admin_context(),
    )
    assert h.service.resolve_identifier(identifier).person_id == source


def test_ended_unknown_start_does_not_block_a_later_proven_period(knowledge_harness):
    h = knowledge_harness
    value = "49180000042@s.whatsapp.net"
    identifier = Identifier("whatsapp", "phone_jid", value)
    old_person = h.observe("whatsapp", "phone_jid", value, mapping=True).person_id
    old_binding = h.service._identity.binding_for(identifier)  # noqa: SLF001
    assert old_person and old_binding is not None and old_binding.valid_from_ms == 0

    h.service.end_binding(
        binding_id=old_binding.binding_id,
        context=h.admin_context(),
        end_at_ms=2_000,
    )
    new_person = h.person("Later Owner")
    h.authority.issue_evidence_ref("admin-evidence-later-period")
    h.service.add_or_end_binding(
        person_id=new_person,
        identifier=identifier,
        evidence_ref="admin-evidence-later-period",
        context=h.admin_context(),
        valid_from_ms=2_001,
    )

    assert h.service.resolve_identifier(identifier, at_ms=2_001).person_id == new_person


def test_compatibility_alias_writer_populates_shared_normalized_key(knowledge_harness):
    h = knowledge_harness
    person_id = h.person("Alias Owner")
    h.service.contacts_store().upsert_alias(
        contact_id=person_id,
        alias="  MiXeD Name  ",
        source="push_name",
    )

    row = h.service._store.query_one(  # noqa: SLF001
        "SELECT normalized_alias FROM contact_aliases"
        " WHERE contact_id = ? AND source = 'push_name'",
        (person_id,),
    )
    assert row is not None
    assert row["normalized_alias"] == "mixed name"


def test_alias_search_key_backfill_is_owner_authorized_and_idempotent(knowledge_harness):
    h = knowledge_harness
    person_id = h.person("Backfill Owner")
    with h.service._store.transaction():  # noqa: SLF001 - seed a synthetic legacy row
        h.service._store.execute(  # noqa: SLF001
            "INSERT INTO contact_aliases (contact_id, alias, source, first_seen, last_seen)"
            " VALUES (?, ' Legacy Name ', 'legacy', '2020-01-01', '2020-01-01')",
            (person_id,),
        )

    report = h.service.backfill_alias_search_keys(context=h.admin_context())
    assert (report.examined, report.changed, report.denied) == (1, 1, 0)
    row = h.service._store.query_one(  # noqa: SLF001
        "SELECT normalized_alias FROM contact_aliases WHERE contact_id = ? AND source = 'legacy'",
        (person_id,),
    )
    assert row is not None and row["normalized_alias"] == "legacy name"

    second = h.service.backfill_alias_search_keys(context=h.admin_context())
    assert (second.examined, second.changed, second.denied) == (0, 0, 0)
