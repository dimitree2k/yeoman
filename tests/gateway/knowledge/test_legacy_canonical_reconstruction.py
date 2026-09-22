"""Focused tests for canonical, speaker-only legacy reconstruction."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import stat
from pathlib import Path

import pytest
from legacy_fixtures import LINKED_NODE_ID, OWNER_CONTACT_ID, legacy_snapshot_factory
from typer.testing import CliRunner
from yeoman_gateway.cli.commands import app
from yeoman_gateway.knowledge._migration import (
    MigrationSourceError,
    inspect_legacy_nodes,
    migrate_sources,
    propose_legacy_links,
    reconstruct_legacy_statements,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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
    migrate_sources(
        contacts_path=sources.contacts,
        memory_path=sources.memory,
        target=target,
        manifest=tmp_path / "migration.json",
    )
    _insert_binding(target)
    audit = tmp_path / "audit.json"
    audit.write_text(inspect_legacy_nodes(target).to_json() + "\n", encoding="utf-8")
    candidates = tmp_path / "candidates.json"
    candidates.write_text(
        propose_legacy_links(audit, target).to_json() + "\n", encoding="utf-8"
    )
    return target, candidates


def _table_digest(path: Path, table: str) -> str:
    digest = hashlib.sha256()
    for row in _rows(path, f'SELECT * FROM "{table}"'):
        digest.update(repr(row).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _forbidden_key_count(value: object) -> int:
    if isinstance(value, dict):
        return sum(key in {"content", "meta_json", "subjects"} for key in value) + sum(
            _forbidden_key_count(item) for item in value.values()
        )
    if isinstance(value, list):
        return sum(_forbidden_key_count(item) for item in value)
    return 0


def test_reconstructs_existing_nodes_with_fact_source_and_one_speaker_only(
    tmp_path: Path,
) -> None:
    target, candidates = _fixture(tmp_path)
    before_target = _sha256(target)
    before_nodes = _table_digest(target, "memory2_nodes")
    before_fts = _table_digest(target, "memory2_nodes_fts_data")
    before_embeddings = _table_digest(target, "memory2_embeddings")
    before_facts = _rows(target, "SELECT COUNT(*) FROM memory2_facts")[0][0]
    before_fact_sources = _rows(target, "SELECT COUNT(*) FROM memory2_fact_sources")[0][0]
    before_statement_sources = _rows(
        target, "SELECT COUNT(*) FROM knowledge_statement_sources"
    )[0][0]

    report = reconstruct_legacy_statements(
        candidates,
        target,
        approval_ref="owner-canonical-test",
    )

    assert report.reconstructed == 3
    assert report.statement_rows_created == 3
    assert report.fact_rows_created == 3
    assert report.speaker_roles_created == 3
    assert report.other_roles_created == 0
    assert _sha256(target) != before_target
    assert _table_digest(target, "memory2_nodes") == before_nodes
    assert _table_digest(target, "memory2_nodes_fts_data") == before_fts
    assert _table_digest(target, "memory2_embeddings") == before_embeddings
    assert _rows(target, "SELECT COUNT(*) FROM knowledge_statements") == [(3,)]
    assert _rows(target, "SELECT COUNT(*) FROM memory2_facts") == [(before_facts + 3,)]
    assert _rows(target, "SELECT COUNT(*) FROM knowledge_statement_sources") == [
        (before_statement_sources + 3,)
    ]
    assert _rows(target, "SELECT COUNT(*) FROM memory2_fact_sources") == [
        (before_fact_sources + 3,)
    ]
    assert _rows(
        target,
        "SELECT role, attribution, status, binding_id FROM knowledge_statement_people",
    ) == [("speaker", "transport", "active", "binding-verified-sender")] * 3
    assert _rows(
        target,
        "SELECT DISTINCT visibility_scope, group_rule FROM knowledge_statements",
    ) == [("author_only", "author_only")]
    assert _rows(
        target,
        "SELECT DISTINCT status FROM knowledge_statement_sources",
    ) == [("unknown",)]
    assert _rows(
        target,
        "SELECT DISTINCT role FROM knowledge_statement_people",
    ) == [("speaker",)]
    assert _forbidden_key_count(json.loads(report.to_json())) == 0


def test_reconstruct_rejects_target_fingerprint_mismatch_without_write(tmp_path: Path) -> None:
    target, candidates = _fixture(tmp_path)
    payload = json.loads(candidates.read_text(encoding="utf-8"))
    payload["target_fingerprint"] = "0" * 64
    candidates.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    before = target.read_bytes()

    with pytest.raises(MigrationSourceError) as error:
        reconstruct_legacy_statements(
            candidates,
            target,
            approval_ref="owner-canonical-test",
        )

    assert error.value.reason == "manifest_mismatch"
    assert target.read_bytes() == before


def test_reconstruct_leaves_nonlinked_candidate_quarantined(tmp_path: Path) -> None:
    target, candidates = _fixture(tmp_path)
    payload = json.loads(candidates.read_text(encoding="utf-8"))
    held = next(
        row
        for row in payload["candidates"]
        if row["candidate_state"] == "deterministic"
        and row["proposed_disposition"] == "linked"
    )
    held["candidate_state"] = "review"
    held["proposed_disposition"] = "quarantined"
    held["speaker_candidates"] = []
    payload["counts"]["speaker_candidates"] = 2
    candidates.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")

    report = reconstruct_legacy_statements(
        candidates,
        target,
        approval_ref="owner-canonical-test",
    )

    assert report.reconstructed == 2
    assert report.untouched_candidates == 1
    assert _rows(
        target,
        "SELECT COUNT(*) FROM knowledge_statements WHERE statement_id = ?",
        (held["legacy_node_id"],),
    ) == [(0,)]
    assert _rows(
        target,
        "SELECT detail_json FROM knowledge_quarantine"
        " WHERE source_table = 'memory2_nodes' AND source_pk = ?"
        " AND reason = 'legacy-node-without-fact-shell'",
        (held["legacy_node_id"],),
    ) == [("{}",)]


def test_reconstruct_rejects_existing_canonical_row_without_write(tmp_path: Path) -> None:
    target, candidates = _fixture(tmp_path)
    connection = sqlite3.connect(target)
    try:
        connection.execute(
            "INSERT INTO knowledge_statements (statement_id, workspace_id, scope_key,"
            " author_principal, status, visibility_scope, group_rule, source_chat_id,"
            " source_channel, valid_from_ms, extractor_version, content_hash,"
            " unresolved_mentions_json, dedupe_key, created_ms, updated_ms)"
            " SELECT id, workspace_id, scope_key, 'legacy:test', 'assertion',"
            " 'author_only', 'author_only', chat_id, channel, 1, 'test', content_hash,"
            " '[]', 'test:' || id, 1, 1 FROM memory2_nodes WHERE id = ?",
            (LINKED_NODE_ID,),
        )
        connection.commit()
    finally:
        connection.close()
    audit = tmp_path / "audit-conflict.json"
    audit.write_text(inspect_legacy_nodes(target).to_json() + "\n", encoding="utf-8")
    conflict_candidates = tmp_path / "candidates-conflict.json"
    conflict_candidates.write_text(
        propose_legacy_links(audit, target).to_json() + "\n", encoding="utf-8"
    )
    before = _sha256(target)

    with pytest.raises(MigrationSourceError) as error:
        reconstruct_legacy_statements(
            conflict_candidates,
            target,
            approval_ref="owner-canonical-test",
        )

    assert error.value.reason == "canonical_conflict"
    assert _sha256(target) == before


def test_reconstruct_is_idempotent_with_refreshed_manifest(tmp_path: Path) -> None:
    target, candidates = _fixture(tmp_path)
    first = reconstruct_legacy_statements(
        candidates,
        target,
        approval_ref="owner-canonical-test",
    )
    audit = tmp_path / "audit-after.json"
    audit.write_text(inspect_legacy_nodes(target).to_json() + "\n", encoding="utf-8")
    refreshed = tmp_path / "candidates-after.json"
    refreshed.write_text(
        propose_legacy_links(audit, target).to_json() + "\n", encoding="utf-8"
    )
    before = _sha256(target)

    second = reconstruct_legacy_statements(
        refreshed,
        target,
        approval_ref="owner-canonical-test",
    )

    assert first.reconstructed == 3
    assert second.reconstructed == 0
    assert second.already_reconstructed == 3
    assert second.before_target_fingerprint == before
    assert second.after_target_fingerprint == before


def test_reconstruct_cli_writes_private_audit_and_rejects_existing_output(
    tmp_path: Path,
) -> None:
    target, candidates = _fixture(tmp_path)
    output = tmp_path / "canonical-audit.json"
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "knowledge",
            "migration",
            "reconstruct-legacy-statements",
            "--candidates",
            str(candidates),
            "--target",
            str(target),
            "--out",
            str(output),
            "--approval-ref",
            "owner-canonical-cli-test",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "reconstructed statements: 3" in result.output
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert json.loads(output.read_text(encoding="utf-8"))["reconstructed"] == 3
    before = _sha256(target)

    repeated = runner.invoke(
        app,
        [
            "knowledge",
            "migration",
            "reconstruct-legacy-statements",
            "--candidates",
            str(candidates),
            "--target",
            str(target),
            "--out",
            str(output),
            "--approval-ref",
            "owner-canonical-cli-test",
        ],
    )

    assert repeated.exit_code != 0
    assert "target_exists" in repeated.output
    assert _sha256(target) == before
