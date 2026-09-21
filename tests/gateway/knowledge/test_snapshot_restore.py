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
    """Two *closed* writers, so the offline snapshot premise is literally true."""
    from yeoman_gateway.knowledge.api import open_knowledge_store
    from yeoman_gateway.knowledge.authority import FakePolicyAuthority, FakeSourceAuthority
    from yeoman_gateway.knowledge.models import (
        Identifier,
        SourceRef,
        TrustedIdentityObservation,
    )

    knowledge_path = tmp_path / "knowledge.db"
    authority = FakeSourceAuthority()

    class _SnapshotPolicy(FakePolicyAuthority):
        """The composition root's owner decision, without a real policy engine."""

        def admin_actor(self) -> str:
            return "whatsapp:4910000000001"

    policy = _SnapshotPolicy(
        admins={"whatsapp:4910000000001"}, capture_actors={"whatsapp:4910000000001"}
    )
    service = open_knowledge_store(
        knowledge_path,
        workspace_id="snapshot-tests",
        source_authority=authority,
        policy_authority=policy,
    )
    observation = TrustedIdentityObservation(
        identifiers=(Identifier("whatsapp", "phone_jid", "49183333333@s.whatsapp.net", "acc"),),
        evidence_ref="snapshot-observation-1",
        observed_name="Synthetic Snapshot Person",
    )
    authority.issue_observation(observation)
    person = service.resolve_observation(observation)
    assert person.person_id
    # A manual name correction must survive the round trip.
    service.set_preferred_name(person.person_id, "Corrected Name", context=service.admin_context_for(reason="snapshot-tests"))

    from yeoman_gateway.knowledge.models import (
        AttributeCandidate,
        AttributeValue,
        PersonLinkCandidate,
        StatementCandidate,
    )

    source = SourceRef(
        event_id="snapshot-event-1",
        revision=1,
        channel="whatsapp",
        chat_id="group-a",
        author_principal="whatsapp:49183333333",
        occurred_at_ms=1,
    )
    from yeoman_gateway.knowledge.authority import EvidenceAudience

    authority.issue_source(source, EvidenceAudience.known({"whatsapp:49183333333"}))
    candidate = StatementCandidate(
        content="Synthetic residence.",
        sources=(source,),
        people=(
            PersonLinkCandidate(
                person_id=person.person_id, role="subject", source=source, attribution="extracted"
            ),
        ),
        attributes=(
            AttributeCandidate(
                person_id=person.person_id, attribute_key="residence", value=AttributeValue("Köln")
            ),
        ),
        extractor_version="snapshot-test",
        confidence=0.5,
    )
    from yeoman_gateway.knowledge.models import TrustedCaptureContext

    request_id = "snapshot-capture-1"
    policy.issue_capture(request_id)
    context = TrustedCaptureContext(
        request_id=request_id,
        policy_revision=service.policy_revision,
        capture_basis="user_message",
        authorized_sources=(source,),
        actor_principal="whatsapp:4910000000001",
        authorized=True,
    )
    captured = service.capture(candidate, context=context)
    assert captured.statement_ids
    service.close()

    processing_path = tmp_path / "processing.db"
    connection = sqlite3.connect(processing_path)
    try:
        connection.execute(
            "CREATE TABLE events (event_id TEXT PRIMARY KEY, chat_id TEXT NOT NULL,"
            " revision INTEGER NOT NULL, media_path TEXT)"
        )
        connection.execute(
            "INSERT INTO events (event_id, chat_id, revision, media_path)"
            " VALUES ('snapshot-event-1', 'group-a', 1, '/nonexistent/synthetic.pdf')"
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
    assert payload["revoked_statements"] == 0
    assert "Corrected Name" not in json.dumps(payload)
    assert "49183333333" not in json.dumps(payload)
    # A missing medium is marked, not silently dropped.
    assert payload["media"] and payload["media"][0][1] == -1


def test_restore_rehearsal_preserves_correction_attribute_binding_and_revocation(
    tmp_path: Path,
) -> None:
    """The rehearsal the plan requires: the restored copy still means the same thing."""
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
        name = connection.execute(
            "SELECT preferred_name FROM contacts WHERE preferred_name IS NOT NULL"
        ).fetchone()
        assert name is not None and str(name[0]) == "Corrected Name"
        facet = connection.execute(
            "SELECT attribute_key FROM knowledge_person_attributes"
        ).fetchone()
        assert facet is not None and str(facet[0]) == "residence"
        binding = connection.execute(
            "SELECT COUNT(*) FROM knowledge_identifier_bindings"
            " WHERE status = 'active' AND binding_id IS NOT NULL"
        ).fetchone()
        assert binding is not None and int(binding[0]) == 1
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
