"""Owner review queue for duplicate person evidence."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner
from yeoman_gateway.knowledge import open_knowledge_store, workspace_id_for
from yeoman_gateway.knowledge.authority import FakePolicyAuthority, FakeSourceAuthority
from yeoman_gateway.knowledge.models import (
    Identifier,
    IdentityCandidateWeights,
    KnowledgeError,
    TrustedAdminContext,
    TrustedIdentityObservation,
    TrustedReadContext,
)


def _runtime(tmp_path: Path):
    source = FakeSourceAuthority()
    policy = FakePolicyAuthority(admins={"owner:1"})
    knowledge = open_knowledge_store(
        tmp_path / "knowledge.db",
        workspace_id=workspace_id_for(tmp_path),
        source_authority=source,
        policy_authority=policy,
    )
    context = TrustedAdminContext("owner:1", 1, "admin-ref-1", owner=True)
    return knowledge, source, context


def _person(knowledge, source, number: int, *, name: str) -> str:
    observation = TrustedIdentityObservation(
        identifiers=(Identifier("telegram", "telegram_id", str(number)),),
        evidence_ref=f"identity:{number}",
        observed_name=f"Contact {number}",
        mapping_verified=True,
        channel_hint="telegram",
    )
    source.issue_observation(observation)
    resolved = knowledge.resolve_person(observation)
    assert resolved.person_id is not None
    knowledge.observe_alias(
        person_id=resolved.person_id,
        name=name,
        alias_kind="nickname",
        scope_key="global",
        evidence_ref=f"nickname:{number}",
        status="confirmed",
        source="owner_confirmed",
    )
    return resolved.person_id


def test_duplicate_nickname_is_queued_without_merging(tmp_path: Path) -> None:
    knowledge, source, context = _runtime(tmp_path)
    try:
        first = _person(knowledge, source, 101, name="Sam")
        second = _person(knowledge, source, 202, name=" sam ")

        propose = getattr(knowledge, "propose_identity_candidates", None)
        assert callable(propose), "owner candidate queue API is missing"
        candidates = propose(context=context)

        assert len(candidates) == 1
        candidate = candidates[0]
        assert candidate.person_ids == tuple(sorted((first, second)))
        assert candidate.status == "pending"
        assert candidate.evidence_version == 1
        assert candidate.score is None
        assert dict(candidate.evidence) == {"normalized_name_match": 1}
        assert candidate.common_names == ("sam",)
        repeated = knowledge.propose_identity_candidates(context=context)
        assert repeated[0].candidate_id == candidate.candidate_id
        assert repeated[0].evidence_version == candidate.evidence_version

        rejected = knowledge.decide_identity_candidate(
            candidate.candidate_id,
            decision="not_same",
            expected_candidate_revision=candidate.candidate_revision,
            expected_identity_revision=candidate.identity_revision,
            context=context,
        )
        assert rejected.status == "rejected"

        for person_id, number in ((first, 101), (second, 202)):
            knowledge.observe_alias(
                person_id=person_id,
                name="Sammie",
                alias_kind="nickname",
                scope_key="global",
                evidence_ref=f"extra-nickname:{number}",
                status="confirmed",
                source="owner_confirmed",
            )
        refreshed = knowledge.propose_identity_candidates(context=context)[0]
        assert refreshed.status == "rejected"
        assert refreshed.evidence_version == candidate.evidence_version + 1
        assert refreshed.candidate_revision > rejected.candidate_revision
        assert refreshed.common_names == ("sam", "sammie")
        with pytest.raises(KnowledgeError) as exc_info:
            knowledge.decide_identity_candidate(
                candidate.candidate_id,
                decision="later",
                expected_candidate_revision=candidate.candidate_revision,
                expected_identity_revision=refreshed.identity_revision,
                context=context,
            )
        assert exc_info.value.code == "stale_revision"
        with pytest.raises(KnowledgeError):
            knowledge.list_identity_candidates(
                context=TrustedReadContext(
                    principal_id="person:101",
                    channel="telegram",
                    chat_id="direct:101",
                    recipient_principals=frozenset({"person:101"}),
                    membership_revision="fixture",
                    policy_revision=1,
                    purpose="profile",
                    now_ms=1,
                    is_direct=True,
                )
            )
        assert knowledge.resolve_person(
            TrustedIdentityObservation(
                identifiers=(Identifier("telegram", "telegram_id", "101"),),
                evidence_ref="identity:101",
                observed_name="Contact 101",
                mapping_verified=True,
                channel_hint="telegram",
            )
        ).person_id == first
        assert knowledge.resolve_person(
            TrustedIdentityObservation(
                identifiers=(Identifier("telegram", "telegram_id", "202"),),
                evidence_ref="identity:202",
                observed_name="Contact 202",
                mapping_verified=True,
                channel_hint="telegram",
            )
        ).person_id == second

        later_first = _person(knowledge, source, 501, name="Pat")
        later_second = _person(knowledge, source, 502, name="pat")
        later_candidate = next(
            item
            for item in knowledge.propose_identity_candidates(context=context)
            if set(item.person_ids) == {later_first, later_second}
        )
        later = knowledge.decide_identity_candidate(
            later_candidate.candidate_id,
            decision="later",
            expected_candidate_revision=later_candidate.candidate_revision,
            expected_identity_revision=later_candidate.identity_revision,
            context=context,
        )
        assert later.status == "later"
    finally:
        knowledge.close()


def test_candidate_api_rejects_forged_and_stale_admin_contexts(tmp_path: Path) -> None:
    knowledge, source, context = _runtime(tmp_path)
    try:
        _person(knowledge, source, 601, name="Taylor")
        _person(knowledge, source, 602, name="taylor")
        forged_contexts = (
            TrustedAdminContext("outsider:1", 1, "admin-ref-1", owner=True),
            TrustedAdminContext("owner:1", 99, "admin-ref-1", owner=True),
            TrustedAdminContext("owner:1", 1, "never-issued", owner=True),
        )

        for forged in forged_contexts:
            with pytest.raises(KnowledgeError):
                knowledge.propose_identity_candidates(context=forged)
        assert knowledge._store.query_one("SELECT COUNT(*) AS count FROM knowledge_identity_candidates")[
            "count"
        ] == 0

        candidate = knowledge.propose_identity_candidates(context=context)[0]
        for forged in forged_contexts:
            with pytest.raises(KnowledgeError):
                knowledge.list_identity_candidates(context=forged)
            with pytest.raises(KnowledgeError):
                knowledge.decide_identity_candidate(
                    candidate.candidate_id,
                    decision="not_same",
                    expected_candidate_revision=candidate.candidate_revision,
                    expected_identity_revision=candidate.identity_revision,
                    context=forged,
                )
        assert knowledge.list_identity_candidates(context=context)[0].status == "pending"
    finally:
        knowledge.close()


def test_admin_note_subject_links_do_not_count_as_activity_evidence(tmp_path: Path) -> None:
    knowledge, source, context = _runtime(tmp_path)
    try:
        first = _person(knowledge, source, 611, name="Jordan")
        second = _person(knowledge, source, 612, name="jordan")
        knowledge._policy.admin_actor = lambda: "owner:1"
        knowledge._policy.capture_actors.add("owner:1")
        for person in (first, second):
            knowledge.record_note(
                person,
                content="synthetic owner note",
                channel="whatsapp",
                chat_id="same-chat",
            )

        candidate = next(
            item
            for item in knowledge.propose_identity_candidates(context=context)
            if set(item.person_ids) == {first, second}
        )

        assert dict(candidate.evidence) == {"normalized_name_match": 1}
    finally:
        knowledge.close()


def test_provider_pair_candidates_keep_each_account_namespace_separate(tmp_path: Path) -> None:
    knowledge, source, context = _runtime(tmp_path)
    try:
        expected: dict[tuple[str, str], str] = {}
        for index, namespace in enumerate(("account-a", "account-b"), start=1):
            phone_person = _person(
                knowledge, source, 300 + index * 2, name=f"{namespace} phone owner"
            )
            lid_person = _person(
                knowledge, source, 301 + index * 2, name=f"{namespace} lid owner"
            )
            phone = Identifier(
                "whatsapp", "phone_jid", "491511111111@s.whatsapp.net", namespace
            )
            lid = Identifier("whatsapp", "lid", "123456789@lid", namespace)
            for person_id, identifier, evidence_ref in (
                (phone_person, phone, f"binding:{namespace}:phone"),
                (lid_person, lid, f"binding:{namespace}:lid"),
            ):
                source.issue_evidence_ref(evidence_ref)
                knowledge.bind_identifier(
                    person_id,
                    identifier,
                    evidence_ref=evidence_ref,
                    mapping_verified=True,
                    context=context,
                )
            observation = TrustedIdentityObservation(
                identifiers=(phone, lid),
                evidence_ref=f"provider-pair:{namespace}",
                observed_at_ms=1_700_000_000_000 + index,
                mapping_verified=True,
                channel_hint="whatsapp",
                account_namespace=namespace,
            )
            source.issue_observation(observation)
            assert knowledge.record_provider_pair(observation).status == "conflict"
            expected[tuple(sorted((phone_person, lid_person)))] = namespace

        first = _person(knowledge, source, 390, name="unsupported phone")
        second = _person(knowledge, source, 391, name="unsupported lid")
        knowledge._store.execute(
            "INSERT INTO knowledge_provider_stitch_proposals"
            " (channel, namespace, phone_value, lid_value, phone_person_id, lid_person_id,"
            " status, reason, evidence_ref, created_ms, updated_ms)"
            " VALUES ('whatsapp', 'account-c', '491511111111@s.whatsapp.net', '123456789@lid',"
            " ?, ?, 'protected', 'malformed', 'forged:proposal', 1, 1)",
            (first, second),
        )

        candidates = knowledge.propose_identity_candidates(context=context)
        assert {candidate.person_ids for candidate in candidates} == set(expected)
        assert {
            candidate.person_ids: candidate.provider_pair_sources for candidate in candidates
        } == {
            pair: (("whatsapp", namespace, 1),) for pair, namespace in expected.items()
        }

        explicit = knowledge.propose_identity_candidates(
            context=context,
            weights=IdentityCandidateWeights(
                version="synthetic-test-v1",
                weights=(("provider_pair_source_count", 2.5),),
            ),
        )
        assert {candidate.score for candidate in explicit} == {2.5}
        assert {candidate.weights_version for candidate in explicit} == {"synthetic-test-v1"}
        assert {candidate.status for candidate in explicit} == {"pending"}
    finally:
        knowledge.close()


def test_candidate_proposal_cli_defaults_to_a_preview(monkeypatch) -> None:
    from yeoman_gateway.cli import knowledge_commands

    class FakeKnowledge:
        def __init__(self) -> None:
            self.persist: bool | None = None

        def admin_context_for(self, *, reason: str):
            assert reason == "cli_person_candidates_propose"
            return object()

        def propose_identity_candidates(self, *, context, weights, persist: bool):
            assert context is not None and weights is None
            self.persist = persist
            return ()

        def close(self) -> None:
            return None

    fake = FakeKnowledge()
    monkeypatch.setattr(knowledge_commands, "_open_admin_knowledge", lambda _db, _policy: fake)
    result = CliRunner().invoke(
        knowledge_commands.knowledge_app,
        ["person-candidates-propose", "--db", "/synthetic/knowledge.db"],
    )
    assert fake.persist is False
    assert result.exit_code == 0, result.output
    assert "would queue 0 candidate(s)" in result.output


def test_candidate_merge_uses_reversible_owner_redirect_and_guard(tmp_path: Path) -> None:
    knowledge, source, context = _runtime(tmp_path)
    try:
        first = _person(knowledge, source, 401, name="Alex")
        second = _person(knowledge, source, 402, name="alex")
        candidate = knowledge.propose_identity_candidates(context=context)[0]
        merged = knowledge.decide_identity_candidate(
            candidate.candidate_id,
            decision="merge",
            expected_candidate_revision=candidate.candidate_revision,
            expected_identity_revision=candidate.identity_revision,
            context=context,
            target_id=first,
        )
        assert merged.status == "merged"
        assert merged.operation_id is not None
        assert merged.identity_revision > candidate.identity_revision
        second_observation = TrustedIdentityObservation(
            identifiers=(Identifier("telegram", "telegram_id", "402"),),
            evidence_ref="identity:402",
            observed_name="Contact 402",
            mapping_verified=True,
            channel_hint="telegram",
        )
        assert knowledge.resolve_person(second_observation).person_id == first
        knowledge.undo_merge(
            merged.operation_id,
            expected_revision=merged.identity_revision,
            context=context,
        )
        assert knowledge.resolve_person(second_observation).person_id == second

        guarded_first = _person(knowledge, source, 403, name="Morgan")
        redirected_member = _person(knowledge, source, 404, name="Another")
        guarded_second = _person(knowledge, source, 405, name="morgan")
        for person_id, value in (
            (guarded_first, "491511111111@s.whatsapp.net"),
            (redirected_member, "491522222222@s.whatsapp.net"),
        ):
            evidence_ref = f"binding:guard:{value}"
            source.issue_evidence_ref(evidence_ref)
            knowledge.bind_identifier(
                person_id,
                Identifier("whatsapp", "phone_jid", value, "account-a"),
                evidence_ref=evidence_ref,
                mapping_verified=True,
                context=context,
            )
        knowledge.merge_people(
            guarded_first,
            redirected_member,
            expected_revision=knowledge._store.identity_revision,
            context=context,
        )
        guarded = next(
            item
            for item in knowledge.propose_identity_candidates(context=context)
            if set(item.person_ids) == {guarded_first, guarded_second}
        )
        with pytest.raises(KnowledgeError) as exc_info:
            knowledge.decide_identity_candidate(
                guarded.candidate_id,
                decision="merge",
                expected_candidate_revision=guarded.candidate_revision,
                expected_identity_revision=guarded.identity_revision,
                context=context,
                target_id=guarded_first,
            )
        assert exc_info.value.code == "identity_conflict"
        assert knowledge.resolve_person(
            TrustedIdentityObservation(
                identifiers=(Identifier("telegram", "telegram_id", "404"),),
                evidence_ref="identity:404",
                observed_name="Contact 404",
                mapping_verified=True,
                channel_hint="telegram",
            )
        ).person_id == guarded_first
        assert knowledge.resolve_person(
            TrustedIdentityObservation(
                identifiers=(Identifier("telegram", "telegram_id", "405"),),
                evidence_ref="identity:405",
                observed_name="Contact 405",
                mapping_verified=True,
                channel_hint="telegram",
            )
        ).person_id == guarded_second
    finally:
        knowledge.close()
