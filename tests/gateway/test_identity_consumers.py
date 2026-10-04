from __future__ import annotations

from pathlib import Path

from yeoman_gateway.knowledge.api import open_knowledge_store
from yeoman_gateway.knowledge.authority import FakePolicyAuthority, FakeSourceAuthority
from yeoman_gateway.knowledge.models import Identifier, TrustedIdentityObservation

OWNER = "whatsapp:4910000000001"
IDENTIFIER = "4917000000000@s.whatsapp.net"


class _OwnerPolicy(FakePolicyAuthority):
    def admin_actor(self) -> str:
        return OWNER


def test_value_resolution_fails_closed_for_conflicting_account_owners(
    tmp_path: Path,
) -> None:
    source = FakeSourceAuthority()
    policy = _OwnerPolicy(admins={OWNER}, capture_actors={OWNER})
    knowledge = open_knowledge_store(
        tmp_path / "knowledge.db",
        workspace_id="identity-consumer-tests",
        source_authority=source,
        policy_authority=policy,
    )

    def observe(namespace: str, name: str) -> str:
        observation = TrustedIdentityObservation(
            identifiers=(Identifier("whatsapp", "phone_jid", IDENTIFIER, namespace),),
            evidence_ref=f"consumer-observation-{namespace}",
            observed_name=name,
            observed_at_ms=1_700_000_000_000,
        )
        source.issue_observation(observation)
        result = knowledge.resolve_observation(observation)
        assert result.person_id is not None
        return result.person_id

    try:
        first = observe("account-one", "First Person")
        second = observe("account-two", "Second Person")
        assert first != second
        assert set(knowledge.owners_of_identifier_value(IDENTIFIER)) == {first, second}

        assert knowledge.person_id_for_value(IDENTIFIER) is None
        assert knowledge.name_for_identifier(IDENTIFIER) is None
        assert knowledge.person_id_for_value("unknown-value") is None

        merged = knowledge.merge_people_with_policy(first, second, reason="test")
        assert knowledge.person_id_for_value(IDENTIFIER) == first
        assert knowledge.name_for_identifier(IDENTIFIER) is not None

        knowledge.undo_merge_with_policy(merged.operation_id, reason="test")
        assert knowledge.person_id_for_value(IDENTIFIER) is None
    finally:
        knowledge.close()
