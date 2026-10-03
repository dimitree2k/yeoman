from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner
from yeoman_gateway.cli.commands import app
from yeoman_gateway.knowledge._history import HistoricalJournal
from yeoman_gateway.knowledge._history_rebuild import rebuild_history, verify_history

from tests.gateway.test_history_rebuild import (
    _collection,
    _event,
    _inbound,
    _write_source,
)


def test_two_rebuilds_have_identical_semantic_counts_hashes_and_conflicts(
    tmp_path: Path,
) -> None:
    collection = _collection(
        tmp_path / "collection",
        _write_source(tmp_path / "collection", "copy-a", events=[_event("event-a", "native-a", "one")]),
        _write_source(tmp_path / "collection", "copy-b", events=[_event("event-a", "native-a", "two")]),
    )

    first = rebuild_history(collection=collection, target_home=tmp_path / "replay-a")
    second = rebuild_history(collection=collection, target_home=tmp_path / "replay-b")

    assert first["semantic_receipt"]["counts"] == second["semantic_receipt"]["counts"]
    assert first["semantic_receipt"]["hashes"] == second["semantic_receipt"]["hashes"]
    assert first["semantic_receipt"]["sha256"] == second["semantic_receipt"]["sha256"]
    assert first["conflict_count"] == second["conflict_count"] == 1


def test_semantic_receipt_counts_persisted_collision_rows_and_keeps_locators(
    tmp_path: Path,
) -> None:
    root = tmp_path / "collection"
    collection = _collection(
        root,
        _write_source(
            root,
            "source-a",
            events=[_event("shared-event", None, "same text")],
        ),
        _write_source(
            root,
            "source-b",
            events=[_event("shared-event", None, "same text")],
        ),
    )
    target = tmp_path / "rebuilt"

    built = rebuild_history(collection=collection, target_home=target)
    verified = verify_history(target_home=target)
    connection = sqlite3.connect(target / "data" / "processing.db")
    persisted_event_details = connection.execute(
        "SELECT COUNT(*) FROM history_event_details"
    ).fetchone()[0]
    copies = connection.execute(
        "SELECT source_id,locator_json FROM history_event_copies ORDER BY source_id"
    ).fetchall()
    aliases = connection.execute(
        "SELECT source_id,locator_json FROM history_event_aliases ORDER BY source_id"
    ).fetchall()
    connection.close()

    assert verified["semantic_receipt_valid"] is True
    assert built["semantic_receipt"]["counts"]["event_details"] == persisted_event_details == 1
    assert built["semantic_receipt"]["counts"]["event_copies"] == len(copies) == 2
    assert built["semantic_receipt"]["counts"]["event_aliases"] == len(aliases) == 2
    assert {row[0] for row in copies} == {"source-a", "source-b"}
    assert {row[0] for row in aliases} == {"source-a", "source-b"}
    assert all(row[1] for row in copies + aliases)


def _snapshot(path: Path, references: list[tuple[str, int]]) -> Path:
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE knowledge_statement_sources "
        "(statement_id TEXT,event_id TEXT,revision INTEGER)"
    )
    connection.executemany(
        "INSERT INTO knowledge_statement_sources VALUES (?,?,?)",
        [(f"statement-{index}", event_id, revision) for index, (event_id, revision) in enumerate(references)],
    )
    connection.commit()
    connection.close()
    return path


def test_source_reference_closure_including_purged_live_event(tmp_path: Path) -> None:
    collection = _collection(
        tmp_path / "collection",
        _write_source(
            tmp_path / "collection",
            "canonical",
            events=[_event("evt-purged", "native-purged", None, purged_ms=1_725_000_000_000)],
            inbound=[_inbound("native-purged", "recoverable original")],
            nodes=[
                {
                    "id": "derived-1",
                    "source_message_id": "native-derived",
                    "kind": "derived_summary",
                    "is_deleted": 0,
                    "channel": "whatsapp",
                    "account": "acct-a",
                    "chat_id": "chat-a@g.us",
                    "sender_id": None,
                    "source_role": "summary",
                    "created_at": "2026-09-01T10:00:00+00:00",
                    "content": "summary only",
                }
            ],
        ),
    )
    snapshot = _snapshot(
        tmp_path / "knowledge.db",
        [
            ("evt-purged", 1),
            ("legacy-node:derived-1", 1),
            ("legacy-node:missing", 1),
        ],
    )
    target = tmp_path / "rebuilt"

    rebuild_history(collection=collection, target_home=target)
    verified = verify_history(target_home=target, knowledge_snapshot=snapshot)

    closure = verified["structural_reference_closure"]
    by_id = {row["event_id"]: row for row in closure["references"]}
    assert by_id["evt-purged"]["structural_status"] == "resolved"
    assert by_id["evt-purged"]["original_source_status"] == "valid"
    assert by_id["legacy-node:derived-1"]["structural_status"] == "resolved"
    assert by_id["legacy-node:derived-1"]["original_source_status"] == "invalid"
    assert by_id["legacy-node:missing"]["structural_status"] == "unresolved"
    assert closure["status"] == "incomplete"
    assert verified["original_source_validity"]["status"] == "invalid"
    assert verified["disclosure_permission"]["status"] == "not_assessed"
    assert verified["complete"] is False


def test_input_tampering_invalidates_receipt(tmp_path: Path) -> None:
    collection = _collection(
        tmp_path / "collection",
        _write_source(tmp_path / "collection", "canonical", events=[_event("event-1", "native-1", "before")]),
    )
    target = tmp_path / "rebuilt"
    rebuild_history(collection=collection, target_home=target)
    connection = sqlite3.connect(target / "data" / "processing.db")
    row = connection.execute(
        "SELECT normalized_json FROM history_event_details WHERE event_id='event-1'"
    ).fetchone()
    assert row is not None
    normalized = json.loads(row[0])
    normalized["text_hash"] = "0" * 64
    connection.execute(
        "UPDATE history_event_details SET normalized_json=? WHERE event_id='event-1'",
        (json.dumps(normalized, sort_keys=True, ensure_ascii=False, separators=(",", ":")),),
    )
    connection.commit()
    connection.close()

    verified = verify_history(target_home=target)

    assert verified["semantic_receipt_valid"] is False
    assert "event_details" in verified["semantic_receipt"]["mismatched_tables"]
    assert verified["complete"] is False


def test_semantic_receipt_binds_source_hash_and_canonical_identity(tmp_path: Path) -> None:
    collection = _collection(
        tmp_path / "collection",
        _write_source(
            tmp_path / "collection",
            "canonical",
            events=[_event("event-1", "native-1", "retained")],
        ),
    )
    target = tmp_path / "rebuilt"
    rebuild_history(collection=collection, target_home=target)
    assert verify_history(target_home=target)["complete"] is True

    mutations = (
        ("history_event_copies", "source_hash", "0" * 64, "event_copies"),
        ("events", "account", "tampered-account", "canonical_events"),
        ("events", "direction", "out", "canonical_events"),
        ("events", "revision", 2, "canonical_events"),
        ("events", "audience_ref", "tampered-audience", "canonical_events"),
        ("events", "created_ms", 1, "canonical_events"),
    )
    for index, (table, column, value, expected_table) in enumerate(mutations):
        tampered = tmp_path / f"tampered-{index}"
        shutil.copytree(target, tampered)
        connection = sqlite3.connect(tampered / "data" / "processing.db")
        connection.execute(
            f"UPDATE {table} SET {column}=? WHERE rowid=(SELECT rowid FROM {table} LIMIT 1)",
            (value,),
        )
        connection.commit()
        connection.close()

        verified = verify_history(target_home=tampered)

        assert verified["semantic_receipt_valid"] is False, column
        assert expected_table in verified["semantic_receipt"]["mismatched_tables"], column


@pytest.mark.parametrize("pin", ("input_sha256", "source_head", "software_source_head"))
def test_semantic_receipt_binds_input_and_software_pins(tmp_path: Path, pin: str) -> None:
    collection = _collection(
        tmp_path / "collection",
        _write_source(
            tmp_path / "collection",
            "canonical",
            events=[_event("event-1", "native-1", "retained")],
        ),
    )
    target = tmp_path / "rebuilt"
    rebuild_history(collection=collection, target_home=target)
    assert verify_history(target_home=target)["complete"] is True
    tampered = tmp_path / f"tampered-{pin}"
    shutil.copytree(target, tampered)
    connection = sqlite3.connect(tampered / "data" / "processing.db")
    raw = connection.execute(
        "SELECT value FROM history_meta WHERE key='build_report_json'"
    ).fetchone()
    assert raw is not None
    report = json.loads(raw[0])
    if pin == "input_sha256":
        report["input_receipt"]["sha256"] = "0" * 64
    elif pin == "source_head":
        report["semantic_receipt"]["normalization"]["source_head"] = "0" * 40
    else:
        report["software_receipt"]["source_head"] = "0" * 40
    connection.execute(
        "UPDATE history_meta SET value=? WHERE key='build_report_json'",
        (json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")),),
    )
    connection.commit()
    connection.close()

    verified = verify_history(target_home=tampered)

    assert verified["semantic_receipt_valid"] is False


def test_incomplete_coverage_never_reported_complete(tmp_path: Path) -> None:
    collection = _collection(
        tmp_path / "collection",
        _write_source(tmp_path / "collection", "canonical", events=[_event("event-1", "native-1", "retained")]),
    )
    manifest_path = collection / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["complete"] = False
    manifest["sources"].extend(
        {"source_id": f"missing-{index}", "status": "missing", "reason_code": "source_missing"}
        for index in range(5)
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    target = tmp_path / "rebuilt"

    built = rebuild_history(collection=collection, target_home=target)
    verified = verify_history(target_home=target)

    assert built["build_complete"] is True
    assert built["complete"] is True
    assert built["input_complete"] is False
    assert len(built["input_receipt"]["missing_source_ids"]) == 5
    assert verified["build_complete"] is True
    assert verified["input_complete"] is False
    assert verified["input_receipt_valid"] is True
    assert verified["complete"] is False


def test_restore_and_reopen_verification_is_path_independent(tmp_path: Path) -> None:
    collection = _collection(
        tmp_path / "collection",
        _write_source(tmp_path / "collection", "canonical", events=[_event("event-1", "native-1", "kept")]),
    )
    original = tmp_path / "original"
    restored = tmp_path / "restored"
    rebuild_history(collection=collection, target_home=original)
    shutil.copytree(original, restored)

    original_report = verify_history(target_home=original)
    restored_report = verify_history(target_home=restored)
    with HistoricalJournal(restored, create=False) as journal:
        assert journal.rebuild_state()["build_complete"] is True

    assert original_report["semantic_receipt"]["sha256"] == restored_report["semantic_receipt"]["sha256"]
    assert restored_report["semantic_receipt_valid"] is True
    assert restored_report["complete"] is True
    assert restored_report["disclosure_permission"]["status"] == "not_assessed"


def test_verify_cli_requires_explicit_owned_target_and_copies_snapshot_first(
    tmp_path: Path,
) -> None:
    collection = _collection(
        tmp_path / "collection",
        _write_source(tmp_path / "collection", "canonical", events=[_event("event-1", "native-1", "private body")]),
    )
    target = tmp_path / "owned-history"
    rebuild_history(collection=collection, target_home=target)
    snapshot = _snapshot(tmp_path / "knowledge.db", [("event-1", 1)])
    before = hashlib.sha256(snapshot.read_bytes()).hexdigest()

    missing_target = CliRunner().invoke(app, ["knowledge", "history", "verify"])
    result = CliRunner().invoke(
        app,
        [
            "knowledge",
            "history",
            "verify",
            "--target-home",
            str(target),
            "--knowledge-snapshot",
            str(snapshot),
        ],
    )

    assert missing_target.exit_code != 0
    assert result.exit_code == 0, result.output
    receipt = json.loads(result.output)
    assert receipt["structural_reference_closure"]["status"] == "closed"
    assert receipt["complete"] is True
    assert "private body" not in result.output
    assert hashlib.sha256(snapshot.read_bytes()).hexdigest() == before
