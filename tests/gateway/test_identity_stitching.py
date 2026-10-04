"""Durable provider-pair evidence and guarded WhatsApp identity stitching."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from yeoman_gateway.knowledge.api import KnowledgeService, open_knowledge_store
from yeoman_gateway.knowledge.authority import FakePolicyAuthority, FakeSourceAuthority
from yeoman_gateway.knowledge.models import (
    Identifier,
    KnowledgeError,
    TrustedIdentityObservation,
)


class IdentityHarness:
    def __init__(self, path: Path) -> None:
        self.authority = FakeSourceAuthority()
        self.service = open_knowledge_store(
            path,
            workspace_id="identity-stitching-test",
            source_authority=self.authority,
            policy_authority=FakePolicyAuthority(),
        )

    def close(self) -> None:
        self.service.close()

    def identifier(self, kind: str, value: str, namespace: str = "account-a") -> Identifier:
        return Identifier("whatsapp", kind, value, namespace)

    def observation(
        self,
        identifiers: tuple[Identifier, ...],
        *,
        evidence_ref: str,
        observed_at_ms: int,
        verified: bool = False,
        name: str = "Shared push name",
    ) -> TrustedIdentityObservation:
        item = TrustedIdentityObservation(
            identifiers=identifiers,
            evidence_ref=evidence_ref,
            observed_name=name,
            observed_at_ms=observed_at_ms,
            mapping_verified=verified,
            account_namespace=identifiers[0].namespace or "",
        )
        self.authority.issue_observation(item)
        return item

    def bind_separately(self, identifier: Identifier, ref: str) -> str:
        observation = self.observation(
            (identifier,), evidence_ref=ref, observed_at_ms=1_700_000_000_000
        )
        result = self.service.resolve_observation(observation)
        assert result.status == "resolved" and result.person_id
        return result.person_id

    def pair(
        self,
        phone: Identifier,
        lid: Identifier,
        *,
        evidence_ref: str,
        observed_at_ms: int,
        verified: bool = True,
    ) -> TrustedIdentityObservation:
        return self.observation(
            (phone, lid),
            evidence_ref=evidence_ref,
            observed_at_ms=observed_at_ms,
            verified=verified,
        )


def _record_pair(service: KnowledgeService, observation: TrustedIdentityObservation):
    record = getattr(service, "record_provider_pair", None)
    assert callable(record), "KnowledgeService must expose durable provider-pair recording"
    return record(observation)


def test_conflicting_people_create_durable_proposal_without_merge(tmp_path: Path) -> None:
    harness = IdentityHarness(tmp_path / "knowledge.db")
    try:
        phone = harness.identifier("phone_jid", "491700000001@s.whatsapp.net")
        lid = harness.identifier("lid", "100000001@lid")
        phone_person = harness.bind_separately(phone, "single:phone")
        lid_person = harness.bind_separately(lid, "single:lid")

        observation = harness.pair(
            phone, lid, evidence_ref="message:pair-1", observed_at_ms=1_700_000_001_000
        )
        result = _record_pair(harness.service, observation)

        assert (result.status, result.reason) == ("conflict", "identifiers_belong_to_different_people")
        evidence = harness.service._store.query_one(
            "SELECT first_observed_at_ms, last_observed_at_ms, first_source_locator,"
            " last_source_locator FROM knowledge_provider_pair_evidence"
        )
        assert evidence is not None
        assert evidence["first_observed_at_ms"] == 1_700_000_001_000
        assert evidence["last_observed_at_ms"] == 1_700_000_001_000
        assert evidence["first_source_locator"] == "message:pair-1"
        assert evidence["last_source_locator"] == "message:pair-1"
        proposal = harness.service._store.query_one(
            "SELECT phone_person_id, lid_person_id, status FROM knowledge_provider_stitch_proposals"
        )
        assert proposal is not None
        assert proposal["phone_person_id"] == phone_person
        assert proposal["lid_person_id"] == lid_person
        assert proposal["status"] == "pending"
        assert harness.service._store.scalar(
            "SELECT COUNT(*) FROM knowledge_identity_redirects WHERE active = 1"
        ) == 0
    finally:
        harness.close()


def test_one_existing_person_is_completed_and_repeated_source_refreshes_evidence(
    tmp_path: Path,
) -> None:
    harness = IdentityHarness(tmp_path / "knowledge.db")
    try:
        phone = harness.identifier("phone_jid", "491700000002@s.whatsapp.net")
        lid = harness.identifier("lid", "100000002@lid")
        person_id = harness.bind_separately(phone, "single:phone")

        first = harness.pair(
            phone, lid, evidence_ref="message:first", observed_at_ms=1_700_000_002_000
        )
        result = harness.service.resolve_observation(first)
        assert result.status == "resolved" and result.person_id == person_id
        binding = harness.service._store.query_one(
            "SELECT person_id, valid_from_ms, mapping_verified FROM knowledge_identifier_bindings"
            " WHERE channel = 'whatsapp' AND kind = 'lid' AND namespace = 'account-a'"
        )
        assert binding is not None
        assert binding["person_id"] == person_id
        assert binding["valid_from_ms"] == 0
        assert binding["mapping_verified"] == 1

        repeated = harness.pair(
            phone, lid, evidence_ref="message:later", observed_at_ms=1_700_000_003_000
        )
        harness.service.resolve_observation(repeated)
        evidence = harness.service._store.query_one(
            "SELECT first_observed_at_ms, last_observed_at_ms, first_source_locator,"
            " last_source_locator FROM knowledge_provider_pair_evidence"
        )
        assert evidence is not None
        assert evidence["first_observed_at_ms"] == 1_700_000_002_000
        assert evidence["last_observed_at_ms"] == 1_700_000_003_000
        assert evidence["first_source_locator"] == "message:first"
        assert evidence["last_source_locator"] == "message:later"
        assert harness.service._store.scalar(
            "SELECT COUNT(*) FROM knowledge_provider_pair_evidence"
        ) == 1
    finally:
        harness.close()


def test_second_phone_for_an_existing_lid_is_blocked_by_merge_protection(tmp_path: Path) -> None:
    harness = IdentityHarness(tmp_path / "knowledge.db")
    try:
        first_phone = harness.identifier("phone_jid", "491700000003@s.whatsapp.net")
        second_phone = harness.identifier("phone_jid", "491700000004@s.whatsapp.net")
        lid = harness.identifier("lid", "100000003@lid")
        first = harness.pair(
            first_phone, lid, evidence_ref="message:first", observed_at_ms=1_700_000_004_000
        )
        person_id = harness.service.resolve_observation(first).person_id
        assert person_id

        conflicting = harness.pair(
            second_phone, lid, evidence_ref="message:second", observed_at_ms=1_700_000_005_000
        )
        result = _record_pair(harness.service, conflicting)

        assert result.status == "conflict"
        assert result.reason == "provider_pair_cardinality_conflict"
        assert harness.service._store.query_one(
            "SELECT 1 FROM knowledge_identifier_bindings WHERE kind = 'phone_jid'"
            " AND value = ? AND status = 'active'",
            (second_phone.value,),
        ) is None
        assert harness.service._store.scalar(
            "SELECT COUNT(*) FROM knowledge_identifier_bindings WHERE person_id = ?"
            " AND kind = 'phone_jid' AND status = 'active'",
            (person_id,),
        ) == 1
        proposal = harness.service._store.query_one(
            "SELECT status, reason FROM knowledge_provider_stitch_proposals"
        )
        assert proposal is not None
        assert proposal["status"] == "protected"
        assert "multiple_phone_or_lid_values" in proposal["reason"]
    finally:
        harness.close()


def test_issuer_rejects_caller_upgraded_mapping_flag(tmp_path: Path) -> None:
    harness = IdentityHarness(tmp_path / "knowledge.db")
    try:
        phone = harness.identifier("phone_jid", "491700000005@s.whatsapp.net")
        lid = harness.identifier("lid", "100000005@lid")
        issued = harness.pair(
            phone,
            lid,
            evidence_ref="message:unverified",
            observed_at_ms=1_700_000_006_000,
            verified=False,
        )
        forged = replace(issued, mapping_verified=True)

        with pytest.raises(KnowledgeError, match="does not match its evidence"):
            _record_pair(harness.service, forged)

        assert harness.service._store.scalar(
            "SELECT COUNT(*) FROM knowledge_provider_pair_evidence"
        ) == 0
        assert harness.service._store.scalar(
            "SELECT COUNT(*) FROM contacts"
        ) == 0
    finally:
        harness.close()


def test_runtime_issuer_rejects_caller_upgraded_mapping_flag() -> None:
    from yeoman_gateway.knowledge.runtime import RuntimeKnowledgeSources

    phone = Identifier("whatsapp", "phone_jid", "491700000008@s.whatsapp.net", "account-a")
    lid = Identifier("whatsapp", "lid", "100000008@lid", "account-a")
    issued = TrustedIdentityObservation(
        identifiers=(phone, lid),
        evidence_ref="message:runtime-unverified",
        observed_at_ms=1_700_000_010_000,
        mapping_verified=False,
        account_namespace="account-a",
    )
    sources = RuntimeKnowledgeSources()
    sources.observe(issued)

    with pytest.raises(KnowledgeError, match="does not match its evidence"):
        sources.verify_observation(replace(issued, mapping_verified=True))


def test_account_namespace_is_part_of_provider_pair_identity(tmp_path: Path) -> None:
    harness = IdentityHarness(tmp_path / "knowledge.db")
    try:
        phone_a = harness.identifier("phone_jid", "491700000006@s.whatsapp.net", "account-a")
        lid_a = harness.identifier("lid", "100000006@lid", "account-a")
        phone_b = harness.identifier("phone_jid", "491700000006@s.whatsapp.net", "account-b")
        lid_b = harness.identifier("lid", "100000006@lid", "account-b")

        person_a = harness.service.resolve_observation(
            harness.pair(
                phone_a, lid_a, evidence_ref="message:account-a", observed_at_ms=1_700_000_007_000
            )
        ).person_id
        person_b = harness.service.resolve_observation(
            harness.pair(
                phone_b, lid_b, evidence_ref="message:account-b", observed_at_ms=1_700_000_008_000
            )
        ).person_id

        assert person_a and person_b and person_a != person_b
        assert harness.service._store.scalar(
            "SELECT COUNT(*) FROM knowledge_provider_pair_evidence"
        ) == 2
    finally:
        harness.close()


def test_equal_names_do_not_turn_an_unverified_observation_into_a_pair(
    tmp_path: Path,
) -> None:
    harness = IdentityHarness(tmp_path / "knowledge.db")
    try:
        phone = harness.identifier("phone_jid", "491700000007@s.whatsapp.net")
        lid = harness.identifier("lid", "100000007@lid")
        observation = harness.pair(
            phone,
            lid,
            evidence_ref="message:name-only",
            observed_at_ms=1_700_000_009_000,
            verified=False,
        )

        result = harness.service.resolve_observation(observation)

        assert result.status == "resolved"
        assert harness.service._store.scalar(
            "SELECT COUNT(*) FROM knowledge_provider_pair_evidence"
        ) == 0
        assert harness.service._store.scalar(
            "SELECT COUNT(*) FROM knowledge_identifier_bindings WHERE namespace = 'account-a'"
            " AND kind IN ('phone_jid','lid') AND status = 'active'"
        ) == 1
    finally:
        harness.close()


def test_extra_verified_phone_shape_cannot_bypass_pair_cardinality_guard(
    tmp_path: Path,
) -> None:
    harness = IdentityHarness(tmp_path / "knowledge.db")
    try:
        first_phone = harness.identifier("phone_jid", "491700000009@s.whatsapp.net")
        second_phone = harness.identifier("phone_jid", "491700000010@s.whatsapp.net")
        lid = harness.identifier("lid", "100000009@lid")
        established = harness.pair(
            first_phone, lid, evidence_ref="message:established", observed_at_ms=2_000
        )
        person_id = harness.service.resolve_observation(established).person_id
        assert person_id

        observation = harness.observation(
            (first_phone, lid, second_phone),
            evidence_ref="message:extra-phone",
            observed_at_ms=3_000,
            verified=True,
        )
        result = harness.service.resolve_observation(observation)

        assert result.status == "conflict"
        assert result.reason == "provider_pair_invalid_shape"
        assert harness.service._store.query_one(
            "SELECT 1 FROM knowledge_identifier_bindings WHERE kind = 'phone_jid'"
            " AND value = ? AND status = 'active'",
            (second_phone.value,),
        ) is None
        proposal = harness.service._store.query_one(
            "SELECT status, phone_person_id, lid_person_id FROM"
            " knowledge_provider_stitch_proposals WHERE phone_value = ? AND lid_value = ?",
            (second_phone.value, lid.value),
        )
        assert proposal is not None and proposal["status"] == "protected"
        assert proposal["phone_person_id"] == proposal["lid_person_id"] == person_id
    finally:
        harness.close()


def test_missing_observation_account_fails_closed_for_known_and_unknown_pairs(
    tmp_path: Path,
) -> None:
    harness = IdentityHarness(tmp_path / "knowledge.db")
    try:
        known_phone = harness.identifier("phone_jid", "491700000011@s.whatsapp.net")
        known_lid = harness.identifier("lid", "100000011@lid")
        person_id = harness.service.resolve_observation(
            harness.pair(
                known_phone, known_lid, evidence_ref="message:known", observed_at_ms=4_000
            )
        ).person_id
        assert person_id

        second_phone = harness.identifier("phone_jid", "491700000012@s.whatsapp.net")
        known_account_missing = TrustedIdentityObservation(
            identifiers=(second_phone, known_lid),
            evidence_ref="message:missing-account-known",
            observed_at_ms=5_000,
            mapping_verified=True,
        )
        harness.authority.issue_observation(known_account_missing)
        known_result = harness.service.resolve_observation(known_account_missing)
        assert known_result.status == "conflict"
        assert known_result.reason == "provider_pair_missing_account_namespace"
        assert harness.service._store.query_one(
            "SELECT 1 FROM knowledge_identifier_bindings WHERE kind = 'phone_jid'"
            " AND value = ? AND status = 'active'",
            (second_phone.value,),
        ) is None

        contact_count = harness.service._store.scalar("SELECT COUNT(*) FROM contacts")
        unknown_phone = harness.identifier("phone_jid", "491700000013@s.whatsapp.net")
        unknown_lid = harness.identifier("lid", "100000013@lid")
        unknown = TrustedIdentityObservation(
            identifiers=(unknown_phone, unknown_lid),
            evidence_ref="message:missing-account-unknown",
            observed_at_ms=6_000,
            mapping_verified=True,
        )
        harness.authority.issue_observation(unknown)
        unknown_result = harness.service.resolve_observation(unknown)
        assert unknown_result.status == "conflict"
        assert unknown_result.reason == "provider_pair_missing_account_namespace"
        assert harness.service._store.scalar("SELECT COUNT(*) FROM contacts") == contact_count
    finally:
        harness.close()


def test_distinct_provider_sources_are_retained_and_repeats_deduplicate(
    tmp_path: Path,
) -> None:
    harness = IdentityHarness(tmp_path / "knowledge.db")
    try:
        phone = harness.identifier("phone_jid", "491700000014@s.whatsapp.net")
        lid = harness.identifier("lid", "100000014@lid")
        for source, observed_at_ms in (
            ("message:A", 2_000),
            ("message:B", 3_000),
            ("message:C", 4_000),
            ("message:B", 1_000),
        ):
            observation = harness.pair(
                phone, lid, evidence_ref=source, observed_at_ms=observed_at_ms
            )
            harness.service.resolve_observation(observation)

        assert "knowledge_provider_pair_sources" in harness.service._store.table_names()
        rows = harness.service._store.query(
            "SELECT source_locator, first_observed_at_ms, last_observed_at_ms"
            " FROM knowledge_provider_pair_sources ORDER BY source_locator"
        )
        assert [row["source_locator"] for row in rows] == [
            "message:A",
            "message:B",
            "message:C",
        ]
        repeated = next(row for row in rows if row["source_locator"] == "message:B")
        assert repeated["first_observed_at_ms"] == 1_000
        assert repeated["last_observed_at_ms"] == 3_000
        aggregate = harness.service._store.query_one(
            "SELECT first_observed_at_ms, last_observed_at_ms,"
            " first_source_locator, last_source_locator"
            " FROM knowledge_provider_pair_evidence"
        )
        assert aggregate is not None
        assert aggregate["first_observed_at_ms"] == 1_000
        assert aggregate["last_observed_at_ms"] == 4_000
        assert aggregate["first_source_locator"] == "message:B"
        assert aggregate["last_source_locator"] == "message:C"
        assert harness.service._store.scalar(
            "SELECT COUNT(*) FROM knowledge_provider_pair_evidence"
        ) == 1
    finally:
        harness.close()
