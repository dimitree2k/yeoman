"""Offline snapshot and restore rehearsal.

Everything here is synthetic and offline: two closed fixture databases, one explicit
quiesce reference, and isolated restore copies.  Nothing starts a gateway, a worker, a
job or a send, and no live path is ever opened.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from yeoman_gateway.knowledge._snapshot import (
    SnapshotError,
    create_snapshot,
    verify_snapshot,
)


def _fixture_dbs(tmp_path: Path) -> tuple[Path, Path]:
    """A verified schema-3 copy with synthetic curation and revocation history."""
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from yeoman_gateway.knowledge._history_upgrade import upgrade_history_knowledge
    from yeoman_gateway.knowledge._identity import IdentityEngine
    from yeoman_gateway.knowledge._statements import StatementEngine
    from yeoman_gateway.knowledge._store import KnowledgeStore
    from yeoman_gateway.knowledge.authority import EvidenceAudience
    from yeoman_gateway.knowledge.models import SourceRef, StatementCandidate, TrustedCaptureContext
    from yeoman_gateway.knowledge.runtime import RuntimeKnowledgePolicy, RuntimeKnowledgeSources

    from tests.gateway.test_history_knowledge_upgrade import v2

    source_v2 = v2(tmp_path / "knowledge-v2.db")
    store = KnowledgeStore(source_v2, create=False)
    authority = RuntimeKnowledgeSources()
    policy = RuntimeKnowledgePolicy(engine=None)
    identity = IdentityEngine(store, authority=authority, policy=policy, legacy_history_disabled=True)
    statements = StatementEngine(store, identity=identity, authority=authority, policy=policy,
                                 workspace_id="synthetic")
    revoked_source = SourceRef("revocation-source", 1, "whatsapp", "curated@g.us", "whatsapp:10009", 200)
    authority.register_source(revoked_source, EvidenceAudience.author_only())
    context = TrustedCaptureContext("synthetic", 1, "synthetic_snapshot", (revoked_source,))
    with store.transaction():
        statements.capture(StatementCandidate("Synthetic revoked source", (revoked_source,)), context=context)
    with store.transaction():
        statements.invalidate_source_internal(revoked_source, actor="synthetic", reason="fixture_revocation")
    # The verified snapshot requires the locked search index to contain no entries.
    # Rebuild this synthetic v2 index after curation/revocation, preserving historical
    # statement rows while excluding every statement that is not readable.
    store.execute("DELETE FROM memory2_nodes_fts")
    store.execute(
        "INSERT INTO memory2_nodes_fts (entry_id, content) "
        "SELECT n.id, n.content FROM memory2_nodes n "
        "WHERE n.is_deleted = 0 AND n.content <> '' "
        "AND NOT EXISTS (SELECT 1 FROM knowledge_statements s "
        "WHERE s.statement_id = n.id AND (s.status IN ('superseded','revoked') "
        "OR s.revoked_at_ms IS NOT NULL OR s.superseded_by IS NOT NULL))"
    )
    store.commit_if_idle()
    store.close()

    knowledge = tmp_path / "knowledge-v3.db"
    upgrade_history_knowledge(source=source_v2, target=knowledge)
    processing = tmp_path / "processing.db"
    connection = sqlite3.connect(processing)
    try:
        connection.execute(
            "CREATE TABLE events (event_id TEXT PRIMARY KEY, chat_id TEXT NOT NULL,"
            " revision INTEGER NOT NULL, media_path TEXT)"
        )
        connection.executemany(
            "INSERT INTO events VALUES (?, 'curated@g.us', ?, ?)",
            [("curated-source", 4, "/nonexistent/synthetic.pdf"), ("revocation-source", 1, None)],
        )
        connection.execute(
            "CREATE TABLE event_source_authority (event_id TEXT NOT NULL, revision INTEGER NOT NULL,"
            " authorized INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(event_id, revision))"
        )
        connection.commit()
    finally:
        connection.close()
    return processing, knowledge


def test_snapshot_requires_an_explicit_quiesce_reference(tmp_path: Path) -> None:
    """Without a quiesce reference the command must refuse, not guess."""
    processing, knowledge = _fixture_dbs(tmp_path)
    with pytest.raises(SnapshotError) as excinfo:
        create_snapshot(
            processing=processing,
            knowledge=knowledge,
            target_dir=tmp_path / "out",
            quiesce_ref="   ",
        )
    assert excinfo.value.code == "quiesce_ref_required"
    assert not (tmp_path / "out").exists()


def test_snapshot_refuses_to_overwrite_and_never_writes_the_sources(tmp_path: Path) -> None:
    processing, knowledge = _fixture_dbs(tmp_path)
    before = (processing.read_bytes(), knowledge.read_bytes())
    target = tmp_path / "out"
    report = create_snapshot(
        processing=processing,
        knowledge=knowledge,
        target_dir=target,
        quiesce_ref="quiesce-2026-09-21T00:00Z",
    )
    assert report.manifest_path.exists()
    assert (processing.read_bytes(), knowledge.read_bytes()) == before
    with pytest.raises(SnapshotError) as excinfo:
        create_snapshot(
            processing=processing,
            knowledge=knowledge,
            target_dir=target,
            quiesce_ref="quiesce-2",
        )
    assert excinfo.value.code == "target_not_empty"


def test_snapshot_manifest_is_redacted_and_describes_the_copy(tmp_path: Path) -> None:
    processing, knowledge = _fixture_dbs(tmp_path)
    report = create_snapshot(
        processing=processing,
        knowledge=knowledge,
        target_dir=tmp_path / "out",
        quiesce_ref="quiesce-ref-1",
    )
    payload = json.loads(report.manifest_path.read_text(encoding="utf-8"))
    assert payload["quiesce_ref"] == "quiesce-ref-1"
    # The command never claims a coherent live boundary it cannot prove.
    assert payload["coherent_live_boundary"] is False
    assert payload["identity_revision"]
    assert payload["acl_epoch"]
    assert payload["revoked_statements"] == 1
    assert "Corrected Name" not in json.dumps(payload)
    assert "49183333333" not in json.dumps(payload)
    # A missing medium is marked, not silently dropped.
    assert payload["media"] and payload["media"][0][1] == -1




def test_restore_rehearsal_preserves_correction_attribute_binding_and_revocation(
    tmp_path: Path,
) -> None:
    processing, knowledge = _fixture_dbs(tmp_path)
    create_snapshot(
        processing=processing,
        knowledge=knowledge,
        target_dir=tmp_path / "out",
        quiesce_ref="quiesce-ref-restore",
    )
    verification = verify_snapshot(
        manifest=tmp_path / "out" / "manifest.json", restore_dir=tmp_path / "restore-curated"
    )
    assert verification.verdict == "ok", verification.reason
    assert verification.rehearsal_ok and verification.cross_references_ok
    restored = verification.restored_path
    assert restored is not None
    with sqlite3.connect(f"file:{restored}?mode=ro", uri=True) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM knowledge_statements WHERE status='superseded'"
        ).fetchone()[0] >= 1
        assert connection.execute("SELECT COUNT(*) FROM knowledge_person_attributes").fetchone()[0] >= 1
        assert connection.execute(
            "SELECT COUNT(*) FROM knowledge_identifier_bindings WHERE binding_id IS NOT NULL"
        ).fetchone()[0] >= 1
        assert connection.execute(
            "SELECT COUNT(*) FROM knowledge_statements WHERE status='revoked' AND revoked_at_ms IS NOT NULL"
        ).fetchone()[0] >= 1
        assert connection.execute("SELECT COUNT(*) FROM knowledge_jobs").fetchone()[0] >= 1
        suppression = connection.execute(
            "SELECT value FROM knowledge_meta WHERE key='curated_suppression'"
        ).fetchone()
        assert suppression == ("synthetic-withheld-ref",)


def test_verify_reports_a_damaged_snapshot_instead_of_accepting_it(tmp_path: Path) -> None:
    processing, knowledge = _fixture_dbs(tmp_path)
    create_snapshot(
        processing=processing,
        knowledge=knowledge,
        target_dir=tmp_path / "out",
        quiesce_ref="quiesce-ref-3",
    )
    manifest = tmp_path / "out" / "manifest.json"
    # Drop rows from the snapshot copy: the restore must not keep claiming success.
    connection = sqlite3.connect(tmp_path / "out" / "knowledge.db")
    try:
        connection.execute("DELETE FROM knowledge_statement_people")
        connection.commit()
    finally:
        connection.close()
    verification = verify_snapshot(manifest=manifest, restore_dir=tmp_path / "restore2")
    assert verification.verdict == "failed"
    assert any(
        role == "knowledge" and table == "knowledge_statement_people" and have == 0
        for role, table, _want, have in verification.counts
    )


def test_verify_reports_a_missing_manifest_as_a_stable_error(tmp_path: Path) -> None:
    with pytest.raises(SnapshotError) as excinfo:
        verify_snapshot(manifest=tmp_path / "absent.json")
    assert excinfo.value.code == "manifest_missing"


def test_snapshot_cli_round_trip(tmp_path: Path) -> None:
    from typer.testing import CliRunner
    from yeoman_gateway.cli.knowledge_commands import knowledge_app

    processing, knowledge = _fixture_dbs(tmp_path)
    runner = CliRunner()
    created = runner.invoke(
        knowledge_app,
        [
            "snapshot",
            "create",
            "--processing",
            str(processing),
            "--knowledge",
            str(knowledge),
            "--target-dir",
            str(tmp_path / "out"),
            "--quiesce-ref",
            "quiesce-ref-cli",
        ],
    )
    assert created.exit_code == 0, created.output
    assert "coherent live boundary: no" in created.output

    verified = runner.invoke(
        knowledge_app,
        ["snapshot", "verify", "--manifest", str(tmp_path / "out" / "manifest.json")],
    )
    assert verified.exit_code == 0, verified.output
    assert "verdict: ok" in verified.output


def test_a_pre_migration_v1_snapshot_verifies_as_healthy(tmp_path: Path) -> None:
    """The backup that matters most before the upgrade is schema 1.

    It has no ``knowledge_person_attributes``, no ``binding_id`` and an index that still
    carries locked leftovers from before that rule existed.  None of that is a backup
    defect, so the verification reports it instead of failing: a healthy pre-migration
    backup that reads as "broken" would be the worst possible signal.
    """
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from legacy_fixtures import v1_knowledge_store_factory

    fixture = v1_knowledge_store_factory(tmp_path / "v1.db")
    journal = tmp_path / "processing.db"
    connection = sqlite3.connect(journal)
    try:
        connection.execute(
            "CREATE TABLE events (event_id TEXT PRIMARY KEY, chat_id TEXT NOT NULL,"
            " revision INTEGER NOT NULL, payload TEXT NOT NULL DEFAULT '')"
        )
        connection.execute(
            "CREATE TABLE event_source_authority (event_id TEXT NOT NULL,"
            " revision INTEGER NOT NULL, authorized INTEGER NOT NULL DEFAULT 0,"
            " PRIMARY KEY (event_id, revision))"
        )
        connection.commit()
    finally:
        connection.close()

    report = create_snapshot(
        processing=journal,
        knowledge=fixture.path,
        target_dir=tmp_path / "snapshot",
        quiesce_ref="v1-backup-test",
    )
    verification = verify_snapshot(manifest=report.manifest_path)

    assert verification.verdict == "ok", verification.reason
    assert verification.schema_version == "1"
    assert verification.integrity_ok
    assert verification.cross_references_ok
    assert verification.rehearsal_ok
    # The leftovers are reported, and they do not fail a v1 backup.
    assert not verification.locked_index_enforced
