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


def _empty_support_candidate(knowledge, source, context, *, first_number: int):
    first = _person(knowledge, source, first_number, name="Shared Name")
    second = _person(knowledge, source, first_number + 1, name=" shared name ")
    candidate = next(
        item
        for item in knowledge.propose_identity_candidates(context=context)
        if set(item.person_ids) == {first, second}
    )
    alias = knowledge._store.query_one(
        "SELECT id FROM contact_aliases WHERE contact_id = ? AND normalized_alias = ?",
        (first, "shared name"),
    )
    assert alias is not None
    knowledge.retire_alias(
        alias_id=int(alias["id"]), context=context, correct_mapping=True
    )
    knowledge.propose_identity_candidates(context=context)
    refreshed = next(
        item
        for item in knowledge.list_identity_candidates(
            context=context, statuses=("pending",)
        )
        if item.candidate_id == candidate.candidate_id
    )
    assert refreshed.evidence == ()
    assert refreshed.evidence_version == candidate.evidence_version + 1
    return first, second, refreshed


@pytest.mark.parametrize(
    ("decision", "status"), (("not_same", "rejected"), ("later", "later"))
)
def test_empty_support_candidate_can_be_dismissed(
    tmp_path: Path, decision: str, status: str
) -> None:
    knowledge, source, context = _runtime(tmp_path)
    try:
        first, second, candidate = _empty_support_candidate(
            knowledge, source, context, first_number=801
        )
        people_before = tuple(
            tuple(row) for row in knowledge._store.query("SELECT * FROM contacts ORDER BY id")
        )
        bindings_before = tuple(
            tuple(row)
            for row in knowledge._store.query(
                "SELECT * FROM knowledge_identifier_bindings ORDER BY binding_id"
            )
        )
        evidence_before = tuple(
            tuple(row)
            for row in knowledge._store.query(
                "SELECT * FROM knowledge_identity_candidate_evidence"
                " WHERE candidate_id = ? ORDER BY evidence_version",
                (candidate.candidate_id,),
            )
        )

        dismissed = knowledge.decide_identity_candidate(
            candidate.candidate_id,
            decision=decision,
            expected_candidate_revision=candidate.candidate_revision,
            expected_identity_revision=candidate.identity_revision,
            context=context,
        )

        assert dismissed.status == status
        assert dismissed.person_ids == tuple(sorted((first, second)))
        assert tuple(
            tuple(row) for row in knowledge._store.query("SELECT * FROM contacts ORDER BY id")
        ) == people_before
        assert tuple(
            tuple(row)
            for row in knowledge._store.query(
                "SELECT * FROM knowledge_identifier_bindings ORDER BY binding_id"
            )
        ) == bindings_before
        assert tuple(
            tuple(row)
            for row in knowledge._store.query(
                "SELECT * FROM knowledge_identity_candidate_evidence"
                " WHERE candidate_id = ? ORDER BY evidence_version",
                (candidate.candidate_id,),
            )
        ) == evidence_before
    finally:
        knowledge.close()


def test_empty_support_candidate_cannot_merge(tmp_path: Path) -> None:
    knowledge, source, context = _runtime(tmp_path)
    try:
        first, _, candidate = _empty_support_candidate(
            knowledge, source, context, first_number=811
        )

        with pytest.raises(KnowledgeError) as exc_info:
            knowledge.decide_identity_candidate(
                candidate.candidate_id,
                decision="merge",
                expected_candidate_revision=candidate.candidate_revision,
                expected_identity_revision=candidate.identity_revision,
                context=context,
                target_id=first,
            )

        assert exc_info.value.code == "stale_revision"
        assert knowledge.list_identity_candidates(context=context)[0].status == "pending"
    finally:
        knowledge.close()


def test_empty_support_dismissal_keeps_auth_and_revision_checks(tmp_path: Path) -> None:
    knowledge, source, context = _runtime(tmp_path)
    try:
        _, _, candidate = _empty_support_candidate(
            knowledge, source, context, first_number=821
        )
        forged = TrustedAdminContext("owner:1", 1, "never-issued", owner=True)

        with pytest.raises(KnowledgeError):
            knowledge.decide_identity_candidate(
                candidate.candidate_id,
                decision="not_same",
                expected_candidate_revision=candidate.candidate_revision,
                expected_identity_revision=candidate.identity_revision,
                context=forged,
            )
        for expected_candidate_revision, expected_identity_revision in (
            (candidate.candidate_revision - 1, candidate.identity_revision),
            (candidate.candidate_revision, candidate.identity_revision - 1),
        ):
            with pytest.raises(KnowledgeError) as exc_info:
                knowledge.decide_identity_candidate(
                    candidate.candidate_id,
                    decision="not_same",
                    expected_candidate_revision=expected_candidate_revision,
                    expected_identity_revision=expected_identity_revision,
                    context=context,
                )
            assert exc_info.value.code == "stale_revision"

        dismissed = knowledge.decide_identity_candidate(
            candidate.candidate_id,
            decision="later",
            expected_candidate_revision=candidate.candidate_revision,
            expected_identity_revision=candidate.identity_revision,
            context=context,
        )
        with pytest.raises(KnowledgeError) as exc_info:
            knowledge.decide_identity_candidate(
                candidate.candidate_id,
                decision="not_same",
                expected_candidate_revision=dismissed.candidate_revision,
                expected_identity_revision=dismissed.identity_revision,
                context=context,
            )
        assert exc_info.value.code == "identity_conflict"
    finally:
        knowledge.close()


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
            observed_at = knowledge._store.now_ms() + 1_000
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
                knowledge.add_or_end_binding(
                    person_id=person_id,
                    identifier=identifier,
                    evidence_ref=evidence_ref,
                    mapping_verified=True,
                    context=context,
                    valid_from_ms=observed_at,
                )
            observation = TrustedIdentityObservation(
                identifiers=(phone, lid),
                evidence_ref=f"provider-pair:{namespace}",
                observed_at_ms=observed_at,
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


def test_provider_pair_candidates_bind_sources_to_the_observation_time(tmp_path: Path) -> None:
    import json

    knowledge, source, context = _runtime(tmp_path)
    namespace = "account-reassignment"
    phone = Identifier("whatsapp", "phone_jid", "491511111199@s.whatsapp.net", namespace)
    lid = Identifier("whatsapp", "lid", "123456789199@lid", namespace)
    try:
        old_observation = TrustedIdentityObservation(
            identifiers=(phone,),
            evidence_ref="reassignment:old-phone",
            observed_name="Old Person",
            observed_at_ms=1_000,
            account_namespace=namespace,
        )
        source.issue_observation(old_observation)
        old_person = knowledge.resolve_person(old_observation).person_id
        assert old_person is not None

        paired_observation = TrustedIdentityObservation(
            identifiers=(phone, lid),
            evidence_ref="reassignment:provider-pair",
            observed_name="Old Person",
            observed_at_ms=1_500,
            mapping_verified=True,
            account_namespace=namespace,
        )
        source.issue_observation(paired_observation)
        assert knowledge.resolve_person(paired_observation).person_id == old_person

        new_observation = TrustedIdentityObservation(
            identifiers=(
                Identifier("telegram", "telegram_id", "920199", "telegram-account"),
            ),
            evidence_ref="reassignment:new-owner",
            observed_name="New Person",
            observed_at_ms=3_000,
            channel_hint="telegram",
        )
        source.issue_observation(new_observation)
        new_person = knowledge.resolve_person(new_observation).person_id
        assert new_person is not None
        old_binding = knowledge._identity.binding_for(phone)
        assert old_binding is not None
        source.issue_evidence_ref("reassignment:owner-change")
        knowledge.add_or_end_binding(
            person_id=new_person,
            identifier=phone,
            evidence_ref="reassignment:owner-change",
            context=context,
            valid_from_ms=3_000,
            end_binding_id=old_binding.binding_id,
            end_at_ms=3_000,
        )

        # A pending row can outlive the flawed current-owner join from the prior code.
        # Seed that persisted snapshot to verify refresh removes its obsolete support.
        pair = tuple(sorted((old_person, new_person)))
        candidate_id = knowledge._store.new_id()
        now = knowledge._store.now_ms()
        stale_evidence = json.dumps(
            {
                "signals": [["provider_pair_source_count", 1]],
                "common_names": [],
                "provider_pair_sources": [["whatsapp", namespace, 1]],
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        knowledge._store.execute(
            "INSERT INTO knowledge_identity_candidates"
            " (candidate_id, person_low_id, person_high_id, status, candidate_revision,"
            " evidence_version, identity_revision, created_ms, updated_ms)"
            " VALUES (?, ?, ?, 'pending', 1, 1, ?, ?, ?)",
            (candidate_id, *pair, knowledge._store.identity_revision, now, now),
        )
        knowledge._store.execute(
            "INSERT INTO knowledge_identity_candidate_evidence"
            " (candidate_id, evidence_version, identity_revision, evidence_json, created_ms)"
            " VALUES (?, 1, ?, ?, ?)",
            (candidate_id, knowledge._store.identity_revision, stale_evidence, now),
        )

        refreshed = knowledge.propose_identity_candidates(context=context)

        assert not any(
            item.person_ids == pair and item.provider_pair_sources for item in refreshed
        )
        persisted = next(
            item
            for item in knowledge.list_identity_candidates(context=context)
            if item.candidate_id == candidate_id
        )
        assert persisted.provider_pair_sources == ()
        assert persisted.evidence == ()
        assert persisted.evidence_version == 2
        assert knowledge._store.query_one(
            "SELECT COUNT(*) AS count FROM knowledge_identity_candidate_evidence"
            " WHERE candidate_id = ?",
            (candidate_id,),
        )["count"] == 2
        with pytest.raises(KnowledgeError) as exc_info:
            knowledge.decide_identity_candidate(
                candidate_id,
                decision="merge",
                expected_candidate_revision=persisted.candidate_revision,
                expected_identity_revision=persisted.identity_revision,
                context=context,
                target_id=old_person,
            )
        assert exc_info.value.code == "stale_revision"
    finally:
        knowledge.close()


def test_candidate_list_ids_drive_revision_checked_cli_merge(tmp_path: Path) -> None:
    import json
    import re

    from yeoman_gateway.cli.knowledge_commands import knowledge_app

    db = tmp_path / "knowledge.db"
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(
        json.dumps({"owners": {"telegram": ["999"]}}), encoding="utf-8"
    )
    source = FakeSourceAuthority()
    knowledge = open_knowledge_store(
        db,
        workspace_id=workspace_id_for(tmp_path),
        source_authority=source,
        policy_authority=FakePolicyAuthority(admins={"owner:1"}),
    )
    admin = TrustedAdminContext("owner:1", 1, "admin-ref-1", owner=True)
    first = _person(knowledge, source, 9101, name="Candidate One")
    second = _person(knowledge, source, 9102, name="candidate one")
    candidate = knowledge.propose_identity_candidates(context=admin)[0]
    knowledge.close()

    runner = CliRunner()
    listed = runner.invoke(
        knowledge_app,
        [
            "person-candidates-list",
            "--db",
            str(db),
            "--policy",
            str(policy_path),
        ],
    )
    assert listed.exit_code == 0, listed.output
    assert candidate.candidate_id in listed.output
    assert first in listed.output
    assert second in listed.output
    candidate_line = re.search(
        r"(?m)^(\S+) pending candidate_revision=(\d+) evidence_version=(\d+)"
        r" identity_revision=(\d+)$",
        listed.output,
    )
    people_line = re.search(r"(?m)^  people=([^, ]+),([^ ]+)", listed.output)
    assert candidate_line is not None
    assert people_line is not None
    candidate_id, candidate_revision, _, identity_revision = candidate_line.groups()
    target_id = people_line.group(1)
    arguments = [
        "person-candidate-decide",
        "--db",
        str(db),
        "--policy",
        str(policy_path),
        "--candidate",
        candidate_id,
        "--candidate-revision",
        candidate_revision,
        "--identity-revision",
        identity_revision,
        "--decision",
        "merge",
        "--target",
        target_id,
    ]

    preview = runner.invoke(knowledge_app, arguments)
    assert preview.exit_code == 0, preview.output
    assert "would apply merge" in preview.output
    decided = runner.invoke(knowledge_app, [*arguments, "--apply"])
    assert decided.exit_code == 0, decided.output
    assert "merged" in decided.output
