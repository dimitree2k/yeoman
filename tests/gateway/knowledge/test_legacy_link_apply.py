"""Focused tests for the reversible, text-free legacy-link apply ledger."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import stat
from pathlib import Path

import pytest
import typer
from legacy_fixtures import (
    LINKED_NODE_ID,
    OWNER_CONTACT_ID,
    legacy_snapshot_factory,
)
from typer.testing import CliRunner
from yeoman_gateway.cli.knowledge_commands import knowledge_app
from yeoman_gateway.knowledge._migration import (
    MigrationSourceError,
    apply_legacy_link_decisions,
    inspect_legacy_nodes,
    migrate_sources,
    propose_legacy_links,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _cli_app() -> typer.Typer:
    parent = typer.Typer()
    parent.add_typer(knowledge_app, name="knowledge")
    return parent


def _rows(path: Path, sql: str, params: tuple[object, ...] = ()) -> list[tuple[object, ...]]:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return [tuple(row) for row in connection.execute(sql, params).fetchall()]
    finally:
        connection.close()


def _insert_binding(target: Path) -> None:
    connection = sqlite3.connect(target)
    try:
        connection.execute(
            "INSERT INTO knowledge_identifier_bindings (binding_id, channel, kind,"
            " namespace, value, person_id, status, valid_from_ms, valid_until_ms,"
            " observed_at_ms, evidence_ref, mapping_verified, revision, created_ms,"
            " updated_ms) VALUES (?, 'whatsapp', 'phone_jid', 'default', ?, ?,"
            " 'active', 0, 0, 1, 'synthetic-binding-evidence', 1, 1, 1, 1)",
            ("binding-verified-sender", "4910000000001@s.whatsapp.net", OWNER_CONTACT_ID),
        )
        connection.commit()
    finally:
        connection.close()


def _fixture(tmp_path: Path) -> tuple[Path, Path]:
    sources = legacy_snapshot_factory(tmp_path)
    target = tmp_path / "knowledge.db"
    manifest = tmp_path / "migration.json"
    migrate_sources(
        contacts_path=sources.contacts,
        memory_path=sources.memory,
        target=target,
        manifest=manifest,
    )
    _insert_binding(target)
    audit = tmp_path / "audit.json"
    audit.write_text(inspect_legacy_nodes(target).to_json() + "\n", encoding="utf-8")
    candidates = tmp_path / "candidates.json"
    candidates.write_text(
        propose_legacy_links(audit, target).to_json() + "\n", encoding="utf-8"
    )
    return target, candidates


def test_apply_marks_one_verified_transport_decision_without_roles_or_text(
    tmp_path: Path,
) -> None:
    target, candidates = _fixture(tmp_path)
    before_roles = _rows(target, "SELECT * FROM knowledge_statement_people")
    before_fingerprint = _sha256(target)

    report = apply_legacy_link_decisions(
        candidates,
        target,
        approval_ref="owner-batch-test",
    )

    assert report.applied == 3
    assert report.linked_candidates == 3
    assert report.speaker_roles_changed == 0
    assert report.statement_rows_changed == 0
    assert report.before_target_fingerprint == before_fingerprint
    assert report.after_target_fingerprint != before_fingerprint
    rows = _rows(
        target,
        "SELECT reason, detail_json FROM knowledge_quarantine"
        " WHERE source_table = 'memory2_nodes' AND source_pk = ?"
        " AND reason = 'legacy-node-without-fact-shell'"
        " ORDER BY reason",
        (LINKED_NODE_ID,),
    )
    assert len(rows) == 1
    detail = json.loads(str(rows[0][1]))
    assert detail["legacy_reconciliation"]["status"] == "applied"
    assert detail["legacy_reconciliation"]["binding_id"] == "binding-verified-sender"
    assert "content" not in detail
    assert "meta_json" not in detail
    assert "subjects" not in json.dumps(detail)
    assert _rows(target, "SELECT * FROM knowledge_statement_people") == before_roles


def test_apply_rejects_target_fingerprint_mismatch_without_write(tmp_path: Path) -> None:
    target, candidates = _fixture(tmp_path)
    before = target.read_bytes()
    payload = json.loads(candidates.read_text(encoding="utf-8"))
    payload["target_fingerprint"] = "0" * 64
    candidates.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(MigrationSourceError) as error:
        apply_legacy_link_decisions(
            candidates,
            target,
            approval_ref="owner-batch-test",
        )

    assert error.value.reason == "manifest_mismatch"
    assert target.read_bytes() == before


def test_apply_requires_every_linked_candidate_to_have_a_quarantine_row(
    tmp_path: Path,
) -> None:
    target, candidates = _fixture(tmp_path)
    payload = json.loads(candidates.read_text(encoding="utf-8"))
    payload["candidates"][0]["proposed_disposition"] = "linked"
    payload["candidates"][0]["candidate_state"] = "deterministic"
    payload["candidates"][0]["speaker_candidates"] = [
        {
            "person_id": OWNER_CONTACT_ID,
            "role": "speaker",
            "attribution": "transport",
            "evidence_kind": "verified_identifier_binding",
            "binding_id": "binding-verified-sender",
        }
    ]
    payload["candidates"][0]["legacy_node_id"] = "not-a-quarantine-row"
    candidates.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")

    before = _sha256(target)
    with pytest.raises(MigrationSourceError) as error:
        apply_legacy_link_decisions(
            candidates,
            target,
            approval_ref="owner-batch-test",
        )

    assert error.value.reason == "apply_target_missing"
    assert _sha256(target) == before


def test_apply_with_fresh_manifest_is_idempotent(tmp_path: Path) -> None:
    target, candidates = _fixture(tmp_path)
    first = apply_legacy_link_decisions(
        candidates,
        target,
        approval_ref="owner-batch-test",
    )
    audit = tmp_path / "audit-after.json"
    audit.write_text(inspect_legacy_nodes(target).to_json() + "\n", encoding="utf-8")
    refreshed = tmp_path / "candidates-after.json"
    refreshed.write_text(
        propose_legacy_links(audit, target).to_json() + "\n", encoding="utf-8"
    )
    before = _sha256(target)

    second = apply_legacy_link_decisions(
        refreshed,
        target,
        approval_ref="owner-batch-test",
    )

    assert first.applied == 3
    assert second.applied == 0
    assert second.already_applied == 3
    assert second.before_target_fingerprint == before
    assert second.after_target_fingerprint == before


def test_apply_cli_writes_private_audit_and_rejects_existing_output(tmp_path: Path) -> None:
    target, candidates = _fixture(tmp_path)
    output = tmp_path / "apply.json"
    runner = CliRunner()

    result = runner.invoke(
        _cli_app(),
        [
            "knowledge",
            "migration",
            "apply-legacy-link-decisions",
            "--candidates",
            str(candidates),
            "--target",
            str(target),
            "--out",
            str(output),
            "--approval-ref",
            "owner-batch-test",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "applied ledger decisions: 3" in result.output
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["applied"] == 3
    assert payload["speaker_roles_changed"] == 0
    assert "content" not in output.read_text(encoding="utf-8")

    target_after_first = _sha256(target)
    repeated = runner.invoke(
        _cli_app(),
        [
            "knowledge",
            "migration",
            "apply-legacy-link-decisions",
            "--candidates",
            str(candidates),
            "--target",
            str(target),
            "--out",
            str(output),
            "--approval-ref",
            "owner-batch-test",
        ],
    )
    assert repeated.exit_code != 0
    assert "target_exists" in (repeated.output or "")
    assert _sha256(target) == target_after_first
