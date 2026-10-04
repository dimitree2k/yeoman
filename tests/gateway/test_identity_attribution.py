"""Offline speaker attribution plan stays inside source, account and time evidence."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from yeoman_gateway.knowledge._identity_attribution import plan_speaker_attribution


def _write_inputs(
    root: Path,
    *,
    time_certainty: str = "native",
    occurred_ms: int = 100,
    proof_occurred_ms: int | None = None,
    valid_from_ms: int = 1,
    valid_until_ms: int = 0,
    mapping_verified: int = 1,
    event_account: str | None = "test-account",
    event_channel: str | None = "whatsapp",
    sender_id_raw: str | None = "15550000001@s.whatsapp.net",
    sender_raw: str | None = None,
    copy_account: str = "test-account",
    unproven_extra_copy: bool = False,
    extra_bindings: tuple[tuple[str, int, int], ...] = (),
    denied: int = 0,
) -> tuple[Path, Path]:
    history_db = root / "processing.db"
    connection = sqlite3.connect(history_db)
    connection.executescript(
        "CREATE TABLE history_event_details ("
        "event_id TEXT, revision TEXT, normalized_json TEXT, semantic_kind TEXT, "
        "semantic_direction TEXT, provenance_class TEXT, retention_status TEXT, "
        "text_hash TEXT, denied INTEGER, PRIMARY KEY(event_id, revision));"
        "CREATE TABLE history_event_copies ("
        "copy_id INTEGER PRIMARY KEY, event_id TEXT, revision TEXT, source_id TEXT, "
        "source_hash TEXT, locator_json TEXT, source_kind TEXT, provenance_class TEXT, "
        "source_authority TEXT, channel TEXT, account TEXT, chat_id TEXT, "
        "disposition TEXT);"
        "CREATE TABLE history_source_proofs ("
        "event_id TEXT, revision TEXT, source_id TEXT, locator_json TEXT, "
        "author_principal TEXT, channel TEXT, chat_id TEXT, occurred_ms INTEGER, "
        "audience_status TEXT, audience_members_json TEXT, snapshot_id TEXT, "
        "policy_revision TEXT, revoked_at_ms INTEGER, revoking_event_id TEXT, "
        "eligible INTEGER, denial_reason TEXT);"
    )
    event = {
        "event_id": "event-1",
        "revision": "1",
        "channel": event_channel,
        "account": event_account,
        "chat_id": "test-chat",
        "sender_id_raw": sender_id_raw,
        "sender_raw": sender_raw,
        "occurred_ms": occurred_ms,
        "time_certainty": time_certainty,
    }
    connection.execute(
        "INSERT INTO history_event_details VALUES (?,?,?,?,?,?,?,?,?)",
        ("event-1", "1", json.dumps(event), "message", "in", "native", "retained", None, denied),
    )
    connection.execute(
        "INSERT INTO history_event_copies VALUES (1,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "event-1", "1", "source-1", "a" * 64, '{"line":1}', "inbound_archive_copy",
            "native", "inbound_archive_copy", "whatsapp", copy_account, "test-chat", "logical_copy",
        ),
    )
    connection.execute(
        "INSERT INTO history_source_proofs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "event-1", "1", "source-1", '{"line":1}', None, "whatsapp", "test-chat",
            occurred_ms if proof_occurred_ms is None else proof_occurred_ms,
            "direct", "[]", "snapshot-1", "revision-1", None, None, 1, None,
        ),
    )
    if unproven_extra_copy:
        connection.execute(
            "INSERT INTO history_event_copies VALUES (2,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "event-1", "1", "source-unproven", "b" * 64, '{"line":2}',
                "inbound_archive_copy", "native", "inbound_archive_copy", "whatsapp",
                copy_account, "test-chat", "logical_copy",
            ),
        )
    connection.commit()
    connection.close()

    knowledge_db = root / "knowledge.db"
    connection = sqlite3.connect(knowledge_db)
    connection.executescript(
        "CREATE TABLE contacts (id TEXT PRIMARY KEY, display_name TEXT, preferred_name TEXT);"
        "CREATE TABLE knowledge_identifier_bindings ("
        "binding_id TEXT, channel TEXT, kind TEXT, namespace TEXT, value TEXT, "
        "person_id TEXT, status TEXT, valid_from_ms INTEGER, valid_until_ms INTEGER, "
        "mapping_verified INTEGER, evidence_ref TEXT);"
        "CREATE TABLE knowledge_identity_redirects ("
        "operation_id TEXT, seq INTEGER, source_id TEXT, target_id TEXT, active INTEGER);"
        "CREATE TABLE knowledge_statement_people ("
        "statement_id TEXT, person_id TEXT, role TEXT, evidence_source_id TEXT, "
        "evidence_revision INTEGER, attribution TEXT, created_ms INTEGER, status TEXT, "
        "binding_id TEXT, resolution_reason TEXT);"
    )
    connection.execute("INSERT INTO contacts VALUES ('person-a', 'Synthetic Person A', NULL)")
    connection.execute(
        "INSERT INTO knowledge_identifier_bindings VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            "binding-a", "whatsapp", "phone_jid", "test-account",
            "15550000001@s.whatsapp.net", "person-a", "active", valid_from_ms,
            valid_until_ms, mapping_verified, "owner-correction:test",
        ),
    )
    for index, (person_id, start, until) in enumerate(extra_bindings, start=2):
        connection.execute(
            "INSERT INTO contacts VALUES (?, ?, NULL)", (person_id, f"Synthetic {person_id}")
        )
        connection.execute(
            "INSERT INTO knowledge_identifier_bindings VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"binding-{index}", "whatsapp", "phone_jid", "test-account",
                "15550000001@s.whatsapp.net", person_id, "ended", start, until, 1,
                f"owner-correction:{index}",
            ),
        )
    connection.execute(
        "INSERT INTO knowledge_statement_people VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("statement-1", "person-a", "speaker", "source-1", 1, "transport", 1, "active", "binding-a", None),
    )
    connection.commit()
    connection.close()
    return history_db, knowledge_db


def _roles(database: Path) -> list[tuple[Any, ...]]:
    with sqlite3.connect(database) as connection:
        return connection.execute(
            "SELECT statement_id, person_id, role, evidence_source_id, evidence_revision, "
            "attribution, status, binding_id FROM knowledge_statement_people ORDER BY 1"
        ).fetchall()


def test_unknown_binding_epoch_remains_candidate_without_role_rewrites(
    tmp_path: Path,
) -> None:
    history_db, knowledge_db = _write_inputs(
        tmp_path, time_certainty="native", valid_from_ms=0
    )
    before = _roles(knowledge_db)

    result = plan_speaker_attribution(history_db, knowledge_db)

    aggregate = result["aggregate"]
    assert aggregate["denominator"] == 1
    assert aggregate["decision_counts"] == {
        "candidate": 1,
        "confirmed": 0,
        "conflict": 0,
        "unresolved": 0,
    }
    assert aggregate["confirmed_fraction"] == {"numerator": 0, "denominator": 1}
    assert aggregate["reason_counts"]["candidate"] == {"binding_start_unknown": 1}
    assert "details" not in plan_speaker_attribution(history_db, knowledge_db)
    assert _roles(knowledge_db) == before


def test_opt_in_details_include_confirmed_canonical_binding_and_source_evidence(
    tmp_path: Path,
) -> None:
    history_db, knowledge_db = _write_inputs(tmp_path)
    history_before = hashlib.sha256(history_db.read_bytes()).hexdigest()
    knowledge_before = hashlib.sha256(knowledge_db.read_bytes()).hexdigest()
    roles_before = _roles(knowledge_db)

    result = plan_speaker_attribution(history_db, knowledge_db, include_details=True)

    detail = result["details"][0]
    assert len(result["details"]) == result["aggregate"]["denominator"] == 1
    assert detail["event"] == {
        "event_id": "event-1",
        "revision": "1",
        "occurred_ms": 100,
        "time_certainty": "native",
    }
    assert detail["decision"] == {"status": "confirmed", "reason": None}
    assert detail["canonical_candidate_person_ids"] == ["person-a"]
    assert detail["canonical_people"] == [
        {"canonical_person_id": "person-a", "display_only_label": "Synthetic Person A"}
    ]
    assert detail["resolved_person_id"] == "person-a"
    assert detail["binding_evidence"] == [
        {
            "binding_id": "binding-a",
            "person_id": "person-a",
            "canonical_person_id": "person-a",
            "status": "active",
            "valid_from_ms": 1,
            "valid_until_ms": 0,
            "mapping_verified": True,
            "evidence_ref": "owner-correction:test",
        }
    ]
    source = detail["supporting_sources"][0]
    assert source["source_id"] == "source-1"
    assert source["source_hash"] == "a" * 64
    assert source["locator"] == {"line": 1}
    assert source["account"] == "test-account"
    assert source["event_occurred_ms"] == source["proof_occurred_ms"] == 100
    serialized = json.dumps(detail)
    assert "15550000001" not in serialized
    assert "test-chat" not in serialized
    assert hashlib.sha256(history_db.read_bytes()).hexdigest() == history_before
    assert hashlib.sha256(knowledge_db.read_bytes()).hexdigest() == knowledge_before
    assert _roles(knowledge_db) == roles_before


def test_opt_in_details_keep_unknown_start_as_candidate_with_period_evidence(
    tmp_path: Path,
) -> None:
    history_db, knowledge_db = _write_inputs(tmp_path, valid_from_ms=0)

    result = plan_speaker_attribution(history_db, knowledge_db, include_details=True)

    detail = result["details"][0]
    assert detail["decision"] == {
        "status": "candidate",
        "reason": "binding_start_unknown",
    }
    assert detail["canonical_candidate_person_ids"] == ["person-a"]
    assert detail["canonical_people"][0]["display_only_label"] == "Synthetic Person A"
    assert detail["resolved_person_id"] is None
    assert detail["binding_evidence"][0]["valid_from_ms"] == 0


def test_approximate_event_time_is_not_confirmed_by_a_known_binding_period(
    tmp_path: Path,
) -> None:
    history_db, knowledge_db = _write_inputs(
        tmp_path, time_certainty="capture_time_approx", valid_from_ms=1
    )

    aggregate = plan_speaker_attribution(history_db, knowledge_db)["aggregate"]

    assert aggregate["decision_counts"] == {
        "candidate": 1,
        "confirmed": 0,
        "conflict": 0,
        "unresolved": 0,
    }
    assert aggregate["reason_counts"]["candidate"] == {"event_time_not_native": 1}


def test_approximate_event_time_does_not_turn_proof_time_difference_into_conflict(
    tmp_path: Path,
) -> None:
    history_db, knowledge_db = _write_inputs(
        tmp_path,
        time_certainty="capture_time_approx",
        occurred_ms=100,
        proof_occurred_ms=101,
    )

    aggregate = plan_speaker_attribution(history_db, knowledge_db)["aggregate"]

    assert aggregate["decision_counts"] == {
        "candidate": 1,
        "confirmed": 0,
        "conflict": 0,
        "unresolved": 0,
    }
    assert aggregate["reason_counts"]["candidate"] == {"event_time_not_native": 1}


def test_exact_native_source_account_and_binding_period_can_be_confirmed(
    tmp_path: Path,
) -> None:
    history_db, knowledge_db = _write_inputs(tmp_path)

    aggregate = plan_speaker_attribution(history_db, knowledge_db)["aggregate"]

    assert aggregate["decision_counts"] == {
        "candidate": 0,
        "confirmed": 1,
        "conflict": 0,
        "unresolved": 0,
    }


def test_sender_raw_typed_jid_is_used_when_optional_sender_id_is_absent(
    tmp_path: Path,
) -> None:
    history_db, knowledge_db = _write_inputs(
        tmp_path, sender_id_raw=None, sender_raw="15550000001@s.whatsapp.net"
    )

    aggregate = plan_speaker_attribution(history_db, knowledge_db)["aggregate"]

    assert aggregate["decision_counts"] == {
        "candidate": 0,
        "confirmed": 1,
        "conflict": 0,
        "unresolved": 0,
    }


def test_disjoint_recycled_binding_periods_resolve_for_event_time(
    tmp_path: Path,
) -> None:
    history_db, knowledge_db = _write_inputs(
        tmp_path,
        valid_from_ms=51,
        extra_bindings=(("person-b", 1, 51),),
    )

    aggregate = plan_speaker_attribution(history_db, knowledge_db)["aggregate"]

    assert aggregate["decision_counts"] == {
        "candidate": 0,
        "confirmed": 1,
        "conflict": 0,
        "unresolved": 0,
    }


def test_unproven_extra_copy_does_not_veto_exact_linked_source_proof(
    tmp_path: Path,
) -> None:
    history_db, knowledge_db = _write_inputs(tmp_path, unproven_extra_copy=True)

    aggregate = plan_speaker_attribution(history_db, knowledge_db)["aggregate"]

    assert aggregate["decision_counts"] == {
        "candidate": 0,
        "confirmed": 1,
        "conflict": 0,
        "unresolved": 0,
    }


def test_unknown_overlapping_binding_periods_remain_unresolved(tmp_path: Path) -> None:
    history_db, knowledge_db = _write_inputs(
        tmp_path,
        valid_from_ms=0,
        extra_bindings=(("person-b", 50, 0),),
    )

    aggregate = plan_speaker_attribution(history_db, knowledge_db)["aggregate"]

    assert aggregate["decision_counts"] == {
        "candidate": 0,
        "confirmed": 0,
        "conflict": 0,
        "unresolved": 1,
    }
    assert aggregate["reason_counts"]["unresolved"] == {"binding_end_unknown": 1}


def test_cross_account_copy_mismatch_is_unresolved(tmp_path: Path) -> None:
    history_db, knowledge_db = _write_inputs(tmp_path, copy_account="other-account")

    aggregate = plan_speaker_attribution(history_db, knowledge_db)["aggregate"]

    assert aggregate["decision_counts"] == {
        "candidate": 0,
        "confirmed": 0,
        "conflict": 0,
        "unresolved": 1,
    }


def test_verified_source_copy_supplies_missing_normalized_scope_only_as_candidate(
    tmp_path: Path,
) -> None:
    history_db, knowledge_db = _write_inputs(
        tmp_path,
        event_account=None,
        event_channel=None,
        valid_from_ms=0,
    )

    aggregate = plan_speaker_attribution(history_db, knowledge_db)["aggregate"]

    assert aggregate["decision_counts"] == {
        "candidate": 1,
        "confirmed": 0,
        "conflict": 0,
        "unresolved": 0,
    }
    assert aggregate["reason_counts"]["candidate"] == {"binding_start_unknown": 1}


def test_denied_message_is_excluded_before_identifier_resolution(tmp_path: Path) -> None:
    history_db, knowledge_db = _write_inputs(tmp_path, denied=1)

    aggregate = plan_speaker_attribution(history_db, knowledge_db)["aggregate"]

    assert aggregate["denominator"] == 0
    assert aggregate["excluded_counts"] == {"denied": 1}
    assert aggregate["confirmed_fraction"] == {"numerator": 0, "denominator": 0}
