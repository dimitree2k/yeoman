"""The legacy mutable identity-candidate queue is retired after native-history cutover."""

from __future__ import annotations

from typer.testing import CliRunner


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

