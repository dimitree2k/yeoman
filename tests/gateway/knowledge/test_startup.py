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
from legacy_fixtures import legacy_snapshot_factory, v1_knowledge_store_factory
from yeoman_gateway.knowledge.api import (
    KnowledgeStartupError,
    open_knowledge_store,
    workspace_id_for,
)
from yeoman_gateway.knowledge.authority import FakePolicyAuthority, FakeSourceAuthority
from yeoman_gateway.knowledge.models import KnowledgeError, TrustedReadContext
from yeoman_gateway.knowledge.runtime import (
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
    from tests.gateway.history_test_helpers import prepare_history_retirement_knowledge

    path = prepare_history_retirement_knowledge(tmp_path, "incompatible")
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


def test_v1_store_is_refused_without_changing_a_single_byte(tmp_path: Path):
    """A v2 binary must never touch a v1 store: no tables, no meta, no mtime, no WAL.

    This is the fail-closed contract of the whole migration story.  A start that
    half-upgraded a v1 file would make the explicit snapshot upgrade unverifiable.
    """
    fixture = v1_knowledge_store_factory(tmp_path / "v1-knowledge.db")
    before_hash = fixture.sha256()
    before_mtime = fixture.mtime_ns()
    before_tables = fixture.table_names()
    before_meta = fixture.meta()

    with pytest.raises(KnowledgeStartupError) as excinfo:
        _open(fixture.path)
    assert excinfo.value.code == "migration_required"
    assert "explicit snapshot upgrade" in str(excinfo.value)

    assert fixture.sha256() == before_hash
    assert fixture.mtime_ns() == before_mtime
    assert fixture.table_names() == before_tables
    assert fixture.meta() == before_meta
    # The probe used a read-only connection, so it left no WAL/journal sidecar behind.
    assert fixture.dump_sidecars() == {}


def test_v1_store_with_unknown_schema_version_is_refused_without_writes(tmp_path: Path):
    fixture = v1_knowledge_store_factory(tmp_path / "v1-knowledge.db")
    conn = sqlite3.connect(fixture.path)
    try:
        conn.execute("UPDATE knowledge_meta SET value = '7' WHERE key = 'schema_version'")
        conn.commit()
    finally:
        conn.close()
    before = fixture.path.read_bytes()
    with pytest.raises(KnowledgeStartupError) as excinfo:
        _open(fixture.path)
    assert excinfo.value.code == "schema_incompatible"
    assert fixture.path.read_bytes() == before


def test_v1_store_without_complete_marker_is_refused_without_writes(tmp_path: Path):
    fixture = v1_knowledge_store_factory(tmp_path / "v1-knowledge.db")
    conn = sqlite3.connect(fixture.path)
    try:
        conn.execute("UPDATE knowledge_meta SET value = '0' WHERE key = 'migration_complete'")
        conn.commit()
    finally:
        conn.close()
    before = fixture.path.read_bytes()
    with pytest.raises(KnowledgeStartupError) as excinfo:
        _open(fixture.path)
    assert excinfo.value.code == "migration_required"
    assert fixture.path.read_bytes() == before


def test_knowledge_store_without_complete_marker_reports_migration_required(tmp_path: Path):
    from tests.gateway.history_test_helpers import prepare_history_retirement_knowledge

    path = prepare_history_retirement_knowledge(tmp_path, "incomplete")
    conn = sqlite3.connect(path)
    try:
        conn.execute("UPDATE knowledge_meta SET value = '0' WHERE key = 'migration_complete'")
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(KnowledgeStartupError) as excinfo:
        _open(path)
    assert excinfo.value.code == "migration_required"


def test_degraded_read_returns_neutral_result_without_personal_recall(tmp_path: Path):
    """With unknown membership the read is empty, not a fallback to legacy search."""
    from tests.gateway.history_test_helpers import prepare_history_retirement_knowledge

    service = _open(prepare_history_retirement_knowledge(tmp_path, "degraded"))
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
        from yeoman_gateway.history.live import HistoryPaused
        from yeoman_gateway.knowledge.models import RecallQuery

        with pytest.raises(HistoryPaused, match="knowledge_history_scope_required"):
            service.recall(RecallQuery(text="anything"), context=context)
    finally:
        service.close()


def test_failed_capture_is_not_reported_as_success(projected_knowledge_harness):
    """A storage failure surfaces as an error, never as an empty successful capture."""
    h = projected_knowledge_harness
    person = h.person("Alex")
    source = h.source(person)
    candidate = h.candidate("Alex reist.", source)
    h.fail_next_commit()
    with pytest.raises(KnowledgeError) as excinfo:
        h.service.capture(candidate, context=h.capture_context(source))
    assert excinfo.value.code == "storage_unavailable"
    assert h.active_statements_for_source(source) == ()


def test_close_drains_and_leaves_no_writer(tmp_path: Path):
    from tests.gateway.history_test_helpers import prepare_history_retirement_knowledge

    service = _open(prepare_history_retirement_knowledge(tmp_path, "close"))
    service.close()
    service.close()  # idempotent
    with pytest.raises(KnowledgeError) as excinfo:
        with service._store.transaction():  # noqa: SLF001 - the contract under test
            pass
    assert excinfo.value.code == "storage_unavailable"


def test_runtime_policy_reads_membership_from_projected_history(tmp_path: Path):
    from yeoman_gateway.history.context import history_knowledge_scope
    from yeoman_gateway.knowledge.api import open_knowledge_store
    from yeoman_gateway.knowledge.authority import FakeSourceAuthority

    from tests.gateway.history_test_helpers import (
        ProjectedHistory,
        prepare_history_retirement_knowledge,
    )

    policy = RuntimeKnowledgePolicy(engine=None)
    service = open_knowledge_store(
        prepare_history_retirement_knowledge(tmp_path, "runtime-policy"),
        workspace_id="runtime-policy-tests",
        source_authority=FakeSourceAuthority(),
        policy_authority=policy,
            )
    history = ProjectedHistory(tmp_path / "history")
    history.add_group(
        chat_id="group-a", participants=["4910000000002@s.whatsapp.net"], timestamp=1_700_000_000_000
    )
    snapshot = history.snapshot()
    context = TrustedReadContext(
        principal_id="whatsapp:4910000000002",
        channel="whatsapp",
        chat_id="group-a",
        recipient_principals=frozenset({"whatsapp:4910000000002"}),
        membership_revision="history:1",
        policy_revision=service.policy_revision,
        purpose="reply",
        now_ms=1_700_000_001_000,
    )
    with history_knowledge_scope(snapshot, service):
        membership = policy.membership(context)
        assert membership is not None
        assert membership.members == frozenset({"whatsapp:4910000000002"})

    unknown = TrustedReadContext(
        principal_id="whatsapp:4910000000002",
        channel="whatsapp",
        chat_id="no-such-chat",
        recipient_principals=frozenset({"whatsapp:4910000000002"}),
        membership_revision=None,
        policy_revision=service.policy_revision,
        purpose="reply",
        now_ms=1_700_000_001_000,
    )
    with history_knowledge_scope(snapshot, service):
        assert policy.membership(unknown) is None
    snapshot.close()
    history.close()
    service.close()


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
