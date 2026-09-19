"""P5.2: startup states and degraded operation.

A fresh installation may initialise an empty knowledge schema.  Everything else - legacy
data without a verified target, an occupied path, an incompatible version, an
unavailable file - is reported as its own state, and none of them silently presents an
empty store as if the old data had been migrated.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from legacy_fixtures import legacy_snapshot_factory
from yeoman_gateway.knowledge.api import (
    KnowledgeStartupError,
    open_knowledge_store,
    workspace_id_for,
)
from yeoman_gateway.knowledge.authority import FakePolicyAuthority, FakeSourceAuthority
from yeoman_gateway.knowledge.models import KnowledgeError, TrustedReadContext
from yeoman_gateway.knowledge.runtime import (
    EvidenceAudience,
    RuntimeKnowledgePolicy,
    RuntimeKnowledgeSources,
)


def _open(path: Path, *, legacy: tuple[Path, ...] = (), workspace: Path | None = None):
    policy = FakePolicyAuthority(
        admins={"whatsapp:4910000000001"}, capture_actors={"whatsapp:4910000000001"}
    )
    return open_knowledge_store(
        path,
        workspace_id=workspace_id_for(workspace or path.parent),
        source_authority=FakeSourceAuthority(),
        policy_authority=policy,
        legacy_sources=legacy,
    )


def test_fresh_installation_initialises_an_empty_knowledge_store(tmp_path: Path):
    service = _open(tmp_path / "knowledge" / "knowledge.db")
    try:
        stats = service.stats(context=_admin(service))
        assert stats.state == "ready"
        assert stats.people_count == 0
        assert stats.statement_count == 0
        assert stats.schema_version >= 1
    finally:
        service.close()


def _admin(service):
    from yeoman_gateway.knowledge.models import TrustedAdminContext

    return TrustedAdminContext(
        actor_principal="whatsapp:4910000000001",
        policy_revision=service.policy_revision,
        authorization_ref="policy:startup-test",
        owner=True,
    )


def test_legacy_data_without_a_knowledge_store_reports_migration_required(tmp_path: Path):
    sources = legacy_snapshot_factory(tmp_path)
    with pytest.raises(KnowledgeStartupError) as excinfo:
        _open(
            tmp_path / "knowledge" / "knowledge.db",
            legacy=(sources.contacts, sources.memory),
        )
    assert excinfo.value.code == "migration_required"
    # Nothing was created to paper over the missing migration.
    assert not (tmp_path / "knowledge" / "knowledge.db").exists()


def test_legacy_memory_file_used_as_target_is_refused(tmp_path: Path):
    """Configuring a legacy file as the store must not silently 'upgrade' it."""
    sources = legacy_snapshot_factory(tmp_path)
    before = sources.memory.read_bytes()
    with pytest.raises(KnowledgeStartupError) as excinfo:
        _open(sources.memory)
    assert excinfo.value.code == "migration_required"
    assert sources.memory.read_bytes() == before


def test_incompatible_schema_version_is_reported_without_writes(tmp_path: Path):
    path = tmp_path / "knowledge.db"
    service = _open(path)
    service.close()
    conn = sqlite3.connect(path)
    try:
        conn.execute("UPDATE knowledge_meta SET value = '999' WHERE key = 'schema_version'")
        conn.commit()
    finally:
        conn.close()
    before = path.read_bytes()
    with pytest.raises(KnowledgeStartupError) as excinfo:
        _open(path)
    assert excinfo.value.code == "schema_incompatible"
    assert path.read_bytes() == before


def test_knowledge_store_without_complete_marker_reports_migration_required(tmp_path: Path):
    path = tmp_path / "knowledge.db"
    service = _open(path)
    service.close()
    conn = sqlite3.connect(path)
    try:
        conn.execute("UPDATE knowledge_meta SET value = '0' WHERE key = 'migration_complete'")
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(KnowledgeStartupError) as excinfo:
        _open(path)
    assert excinfo.value.code == "migration_required"


def test_missing_parent_directory_is_created_for_a_fresh_store(tmp_path: Path):
    target = tmp_path / "deep" / "nested" / "knowledge.db"
    service = _open(target)
    try:
        assert target.exists()
    finally:
        service.close()


def test_degraded_read_returns_neutral_result_without_personal_recall(tmp_path: Path):
    """With unknown membership the read is empty, not a fallback to legacy search."""
    service = _open(tmp_path / "knowledge.db")
    try:
        context = TrustedReadContext(
            principal_id="whatsapp:4910000000002",
            channel="whatsapp",
            chat_id="group-a",
            recipient_principals=None,  # membership not proven
            membership_revision=None,
            policy_revision=service.policy_revision,
            purpose="reply",
            now_ms=1,
        )
        from yeoman_gateway.knowledge.models import RecallQuery

        result = service.recall(RecallQuery(text="anything"), context=context)
        assert result.statement_ids == ()
        assert result.text == ""
        assert result.reason == "membership_unknown"
    finally:
        service.close()


def test_failed_capture_is_not_reported_as_success(tmp_path: Path):
    """A storage failure surfaces as an error, never as an empty successful capture."""
    from yeoman_gateway.knowledge.models import (
        SourceRef,
        StatementCandidate,
        TrustedCaptureContext,
    )

    authority = FakeSourceAuthority()
    policy = FakePolicyAuthority(capture_actors={"whatsapp:4910000000001"})
    policy.issue_capture("cap-1")
    service = open_knowledge_store(
        tmp_path / "knowledge.db",
        workspace_id="ws",
        source_authority=authority,
        policy_authority=policy,
    )
    try:
        source = SourceRef(
            event_id="evt-1",
            revision=1,
            channel="whatsapp",
            chat_id="group-a",
            author_principal="whatsapp:4910000000001",
            occurred_at_ms=1,
        )
        authority.issue_source(source, EvidenceAudience.known({"whatsapp:4910000000001"}))
        context = TrustedCaptureContext(
            request_id="cap-1",
            policy_revision=service.policy_revision,
            capture_basis="user_message",
            authorized_sources=(source,),
            actor_principal="whatsapp:4910000000001",
            authorized=True,
        )
        candidate = StatementCandidate(
            content="Alex reist.", sources=(source,), extractor_version="v", confidence=0.5
        )
        service._store.fail_next_commit = True  # noqa: SLF001 - injected storage failure
        with pytest.raises(KnowledgeError) as excinfo:
            service.capture(candidate, context=context)
        assert excinfo.value.code == "storage_unavailable"
        # The failure left no partial statement behind.
        assert service.active_statement_ids_for_source(source) == ()
    finally:
        service.close()


def test_close_drains_and_leaves_no_writer(tmp_path: Path):
    service = _open(tmp_path / "knowledge.db")
    service.close()
    service.close()  # idempotent
    with pytest.raises(KnowledgeError) as excinfo:
        with service._store.transaction():  # noqa: SLF001 - the contract under test
            pass
    assert excinfo.value.code == "storage_unavailable"


def test_runtime_policy_reads_membership_from_the_chat_registry(tmp_path: Path):
    """The runtime adapter is the only bridge to the registry; unknown means unknown."""
    from yeoman_gateway.storage.chat_registry import ChatRegistry

    registry = ChatRegistry(db_path=tmp_path / "registry.db")
    registry.register_chat(
        channel="whatsapp",
        chat_id="group-a",
        chat_type="group",
        readable_name="synthetic",
        metadata={"participants": [{"id": "whatsapp:4910000000002"}]},
    )
    policy = RuntimeKnowledgePolicy(engine=None, chat_registry=registry)
    context = TrustedReadContext(
        principal_id="whatsapp:4910000000002",
        channel="whatsapp",
        chat_id="group-a",
        recipient_principals=frozenset({"whatsapp:4910000000002"}),
        membership_revision="r1",
        policy_revision=1,
        purpose="reply",
        now_ms=1,
    )
    membership = policy.membership(context)
    assert membership is not None
    assert membership.members == frozenset({"whatsapp:4910000000002"})

    unknown = TrustedReadContext(
        principal_id="whatsapp:4910000000002",
        channel="whatsapp",
        chat_id="no-such-chat",
        recipient_principals=frozenset({"whatsapp:4910000000002"}),
        membership_revision=None,
        policy_revision=1,
        purpose="reply",
        now_ms=1,
    )
    assert policy.membership(unknown) is None
    registry.close()


def test_runtime_source_adapter_refuses_unregistered_evidence():
    sources = RuntimeKnowledgeSources()
    assert sources.evidence_audience(_source_ref(), basis="user_message") is None
    assert sources.verify_source(_source_ref()) is False


def _source_ref():
    from yeoman_gateway.knowledge.models import SourceRef

    return SourceRef(
        event_id="evt-x",
        revision=1,
        channel="whatsapp",
        chat_id="group-a",
        author_principal="whatsapp:4910000000002",
        occurred_at_ms=1,
    )
