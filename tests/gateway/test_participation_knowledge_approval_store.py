"""Delayed approvals against the real schema-3 Knowledge reader, synthetic data only."""
from dataclasses import replace
from types import SimpleNamespace

import pytest
from yeoman_gateway.app.bootstrap import build_participation_decision_validator
from yeoman_gateway.history.context import history_knowledge_scope
from yeoman_gateway.history.queries import HistoryQueries
from yeoman_gateway.knowledge._history_sources import HistoryKnowledgeSources
from yeoman_gateway.knowledge.api import open_knowledge_store
from yeoman_gateway.knowledge.models import StatementCandidate, TrustedCaptureContext
from yeoman_gateway.knowledge.runtime import RuntimeKnowledgePolicy, RuntimeKnowledgeSources
from yeoman_gateway.policy.engine import PolicyEngine
from yeoman_gateway.policy.schema import PolicyConfig
from yeoman_gateway.processing.models import canonical_hash
from yeoman_gateway.processing.participation_knowledge import selection_from_mapping

from tests.gateway.history_test_helpers import ProjectedHistory, prepare_empty_history_knowledge
from tests.gateway.test_participation_knowledge_approval import (
    CHANNEL,
    CHAT,
    _admission,
    _native_evidence,
    _native_fixture,
)


@pytest.mark.parametrize("change", ["unchanged", "text", "revoked", "source_revision"])
async def test_real_knowledge_reader_revalidates_reconstructed_approval(tmp_path, change):
    path = prepare_empty_history_knowledge(tmp_path)
    policy = RuntimeKnowledgePolicy(PolicyEngine(PolicyConfig.model_validate({
        "defaults": {"whoCanTalk": {"mode": "everyone"}},
        "channels": {CHANNEL: {"chats": {CHAT: {}}}},
    }), tmp_path))

    def open_service():
        return open_knowledge_store(path, workspace_id="synthetic", source_authority=RuntimeKnowledgeSources(), policy_authority=policy)

    knowledge = open_service()
    history = ProjectedHistory(tmp_path)
    _native_fixture(history)
    try:
        async with history.turn() as snapshot:
            queries = HistoryQueries(snapshot)
            row = queries.native_message(chat_id=CHAT, native_id="m1")
            sources = HistoryKnowledgeSources(queries, {}, knowledge.history_source_ledger, knowledge._legacy_authority)
            with knowledge.history_scope(queries, sources):
                source = sources.issue(row["message_id"])
                result = knowledge.capture(StatementCandidate("Stammtisch donnerstags.", (source,)), context=TrustedCaptureContext("synthetic", knowledge.policy_revision, "native", (source,)))
                assert result.statement_ids
                evidence = _native_evidence(history, knowledge)
                assert selection_from_mapping({k: v for k, v in evidence.items() if k != "history"}).records
        knowledge.close()
        knowledge = open_service()
        embedding_calls = []

        def embed(text):
            embedding_calls.append(text)
            raise AssertionError("approval validation must never call an embedding provider")

        from yeoman_gateway.knowledge._embedding_index import _EMBEDDING_INDEX_SCHEMA

        for sql in _EMBEDDING_INDEX_SCHEMA:
            knowledge._store.execute(sql)
        knowledge._store.commit_if_idle()
        knowledge._embedder = SimpleNamespace(embed=embed)
        if change == "text":
            knowledge._store.execute("UPDATE memory2_nodes SET content='Stammtisch freitags.' WHERE id=?", (result.statement_ids[0],))
            knowledge._store.commit_if_idle()
        elif change == "revoked":
            knowledge._store.execute("UPDATE knowledge_statement_sources SET status='revoked' WHERE statement_id=?", (result.statement_ids[0],))
            knowledge._store.commit_if_idle()
        elif change == "source_revision":
            knowledge._store.execute("UPDATE knowledge_statement_sources SET revision=revision+1 WHERE statement_id=?", (result.statement_ids[0],))
            knowledge._store.commit_if_idle()
        admission = replace(_admission(payload_hash="exact-text-hash", knowledge_evidence=evidence), knowledge_evidence_digest=canonical_hash(evidence))
        async with history.turn() as snapshot:
            with history_knowledge_scope(snapshot, knowledge):
                allowed, reason = build_participation_decision_validator(knowledge=knowledge)(admission)
        assert allowed == (change == "unchanged"), reason
        assert embedding_calls == []
    finally:
        history.close()
        knowledge.close()


@pytest.mark.parametrize("corruption", ["not_object", "missing", "bad_version", "bad_digest"])
async def test_corrupt_approval_evidence_resolves_hold_and_claim(tmp_path, corruption):
    import json

    from tests.gateway.test_participation_knowledge_approval import (
        FIXED_NOW,
        _build_tools,
        _claim,
        _effect_id,
        _stage,
        _stored_snapshot,
        _Submission,
    )

    tools, log, history, _ = _build_tools(tmp_path)
    knowledge = _native_fixture(history)
    submission = _Submission()
    tools.set_participation_submission(submission)
    tools.set_knowledge_decision_validator(build_participation_decision_validator(knowledge=knowledge))
    try:
        assert await log.reserve_delivery(
            proposal_id="opp-1", effect_id=_effect_id(), channel=CHANNEL, chat_id=CHAT,
            now_ms=int(FIXED_NOW.timestamp() * 1000), limits=(("comment", 3, 86_400_000),), origin="participation",
        )
        async with history.turn():
            evidence = _native_evidence(history, knowledge)
            assert (await _stage(tools, knowledge_evidence=evidence))["status"] == "awaiting_approval"
        snapshot = await _stored_snapshot(log)
        await _claim(log, snapshot)
        admission = snapshot["participation_admission"]
        if corruption == "not_object":
            admission["knowledge_evidence"] = []
        elif corruption == "missing":
            admission["knowledge_evidence"] = None
        elif corruption == "bad_version":
            admission["knowledge_evidence"]["version"] = 99
        else:
            admission["knowledge_evidence_digest"] = "wrong"
        with log._write() as db:
            db.execute("UPDATE speakups SET context_snapshot_json=? WHERE id=?", (json.dumps(snapshot), "opp-1"))
        tools._proposals.clear()
        result = await tools.submit_proposal("opp-1")
        assert result["status"] == "rejected"
        assert submission.calls == []
        row = log._delivery_row_for_effect(_effect_id())
        assert row["delivery_state"] == "failed"
        claim = await log.approval_claim("opp-1")
        assert claim["state"] == "terminal"
        assert claim["resolution"] == "rejected"
        assert (await tools.submit_proposal("opp-1"))["status"] == "rejected"
        assert submission.calls == []
    finally:
        log.close()
        history.close()
