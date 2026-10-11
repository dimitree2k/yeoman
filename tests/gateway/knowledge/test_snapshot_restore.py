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
    """Closed, synthetic schema-3 stores for the offline snapshot rehearsal."""
    from dataclasses import replace

    from yeoman_gateway.knowledge.models import AttributeCandidate, AttributeValue

    from tests.gateway.knowledge.conftest import KnowledgeHarness

    harness = KnowledgeHarness(tmp_path / "harness")
    person = harness.person("Snapshot Person")
    source = harness.source(person)
    original = harness.capture_text("Synthetic residence evidence.", source).statement_ids[0]
    replacement = replace(
        harness.candidate("Corrected synthetic residence evidence.", source, subjects=(person,)),
        attributes=(
            AttributeCandidate(
                person_id=person,
                attribute_key="residence",
                value=AttributeValue("Köln"),
            ),
        ),
    )
    harness.service.correct_statement(
        original,
        replacement,
        expected_source=source,
        context=harness.capture_context(source),
    )
    revoked_source = harness.source(person, chat="group-revoked")
    harness.capture_text("Synthetic revoked evidence.", revoked_source)
    harness.service.invalidate_source(
        revoked_source, context=harness.capture_context(revoked_source)
    )
    harness.close()
    # A corrected statement retains its old FTS row; snapshot rehearsal requires
    # locked statements to have no searchable index entry, so model that cleanup.
    knowledge_path = tmp_path / "harness" / "knowledge-v3.db"
    connection = sqlite3.connect(knowledge_path)
    try:
        connection.execute(
            "DELETE FROM memory2_nodes_fts WHERE entry_id = ?", (original,)
        )
        connection.commit()
    finally:
        connection.close()

    processing_path = tmp_path / "processing.db"
    connection = sqlite3.connect(processing_path)
    try:
        connection.execute(
            "CREATE TABLE events (event_id TEXT PRIMARY KEY, chat_id TEXT NOT NULL,"
            " revision INTEGER NOT NULL, media_path TEXT)"
        )
        connection.execute(
            "INSERT INTO events (event_id, chat_id, revision, media_path)"
            " VALUES (?, 'group-a', 1, '/nonexistent/synthetic.pdf')",
            (source.event_id,),
        )
        connection.commit()
    finally:
        connection.close()
    return processing_path, knowledge_path


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


def test_restore_rehearsal_preserves_correction_attribute_and_revocation(
    tmp_path: Path,
) -> None:
    """The restore keeps the live statement correction, facet, and revocation state."""
    processing, knowledge = _fixture_dbs(tmp_path)
    create_snapshot(
        processing=processing,
        knowledge=knowledge,
        target_dir=tmp_path / "out",
        quiesce_ref="quiesce-ref-2",
    )
    verification = verify_snapshot(
        manifest=tmp_path / "out" / "manifest.json", restore_dir=tmp_path / "restore"
    )
    assert verification.verdict == "ok", verification.reason
    assert verification.integrity_ok
    assert verification.cross_references_ok
    assert verification.rehearsal_ok
    assert verification.fts_locked_ok
    assert verification.restored_path is not None and verification.restored_path.exists()
    # The rehearsal never touches the live snapshot copy.
    assert verification.restored_path != knowledge

    connection = sqlite3.connect(f"file:{verification.restored_path}?mode=ro", uri=True)
    try:
        statement = connection.execute(
            "SELECT n.content, s.status FROM knowledge_statements s"
            " JOIN memory2_nodes n ON n.id = s.statement_id"
            " WHERE n.content = 'Corrected synthetic residence evidence.'"
        ).fetchone()
        assert statement is not None and str(statement[1]) == "assertion"
        facet = connection.execute(
            "SELECT attribute_key FROM knowledge_person_attributes"
        ).fetchone()
        assert facet is not None and str(facet[0]) == "residence"
        revoked = connection.execute(
            "SELECT COUNT(*) FROM knowledge_statements WHERE status = 'revoked'"
        ).fetchone()
        assert revoked is not None and int(revoked[0]) == 1
        # Person identity is History-backed; the frozen Knowledge projection stays empty.
        bindings = connection.execute(
            "SELECT COUNT(*) FROM knowledge_identifier_bindings"
        ).fetchone()
        assert bindings is not None and int(bindings[0]) == 0
    finally:
        connection.close()


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
