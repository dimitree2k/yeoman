"""Offline speaker attribution plan stays inside source, account and time evidence."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from yeoman_gateway.knowledge._identity_attribution import plan_speaker_attribution
from yeoman_gateway.knowledge._identity_attribution_basis import resolve_attribution_binding
from yeoman_gateway.knowledge._identity_audit import IdentityAuditError
from yeoman_gateway.knowledge.models import Identifier


def _write_inputs(
    root: Path,
    *,
    time_certainty: str = "native",
    occurred_ms: int | None = 100,
    proof_occurred_ms: int | None = None,
    valid_from_ms: int = 1,
    valid_until_ms: int = 0,
    mapping_verified: int = 1,
    event_account: str | None = "test-account",
    event_channel: str | None = "whatsapp",
    sender_id_raw: str | None = "15550000001@s.whatsapp.net",
    sender_raw: str | None = None,
    participant_jid_raw: str | None = None,
    name_observations: tuple[dict[str, Any], ...] = (),
    copy_account: str | None = "test-account",
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
        "participant_jid_raw": participant_jid_raw,
        "name_observations": list(name_observations),
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


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _without_explicit_account(history_db: Path) -> None:
    with sqlite3.connect(history_db) as connection:
        raw = connection.execute(
            "SELECT normalized_json FROM history_event_details"
        ).fetchone()[0]
        event = json.loads(raw)
        event["account"] = None
        connection.execute(
            "UPDATE history_event_details SET normalized_json=?", (json.dumps(event),)
        )
        connection.execute("UPDATE history_event_copies SET account=NULL")


def _single_account_evidence(
    history_db: Path,
    knowledge_db: Path,
    *,
    config_accounts: tuple[str, ...] = ("test-account",),
    journal_complete: bool = True,
) -> dict[str, Any]:
    with sqlite3.connect(history_db) as connection:
        if connection.execute("SELECT 1 FROM history_event_copies WHERE copy_id=2").fetchone() is None:
            connection.execute(
                "INSERT INTO history_event_copies VALUES (2,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("event-1", "1", "inventory-source", "b" * 64, '{"line":2}',
                 "inbound_archive_copy", "native", "inbound_archive_copy", "whatsapp",
                 "test-account", "test-chat", "logical_copy"),
            )
    history_hash = _sha256(history_db)
    knowledge_hash = _sha256(knowledge_db)
    period = {"start_ms": 1, "end_ms": 101}
    config_path = history_db.parent / "account-inventory.json"
    config_path.write_text(json.dumps({
        "channel": "whatsapp", "accounts": list(config_accounts),
        "covered_period": period, "reviewed": True, "coverage_complete": True,
    }))
    config_hash = _sha256(config_path)
    with sqlite3.connect(history_db) as connection:
        copies = connection.execute(
            "SELECT copy_id, source_id FROM history_event_copies WHERE event_id='event-1' ORDER BY copy_id"
        ).fetchall()
    journal_refs = [
        {"input": "history", "source_id": source_id, "sha256": history_hash,
         "locator": {"table": "history_event_copies", "copy_id": copy_id, "source_id": source_id},
         "time_ms": 100}
        for copy_id, source_id in copies
    ]
    return {
        "schema_version": 1,
        "input_hashes": {
            "history": history_hash,
            "knowledge": knowledge_hash,
            "config": config_hash,
        },
        "covered_period": period,
        "account_inventories": [
            {
                "channel": "whatsapp",
                "covered_period": period,
                "sources": {
                    "config": {
                        "complete": True,
                        "reviewed": True,
                        "coverage_complete": True,
                        "accounts": list(config_accounts),
                        "source_refs": [
                            {
                                "input": "config",
                                "source_id": "config-whatsapp",
                                "sha256": config_hash,
                                "locator": {"path": str(config_path), "key": "accounts"},
                                "time_ms": 100,
                            }
                        ],
                    },
                    "journal": {
                        "complete": journal_complete,
                        "accounts": ["test-account"],
                        "source_refs": journal_refs,
                    },
                    "bindings": {
                        "complete": True,
                        "accounts": ["test-account"],
                        "source_refs": [
                            {
                                "input": "knowledge",
                                "source_id": "binding-a",
                                "sha256": knowledge_hash,
                                "locator": {
                                    "table": "knowledge_identifier_bindings",
                                    "binding_id": "binding-a",
                                },
                                "time_ms": 100,
                            }
                        ],
                    },
                },
            }
        ],
        "observations": [],
        "continuity_spans": [],
    }


def test_single_account_inference_requires_complete_period(tmp_path: Path) -> None:
    history_db, knowledge_db = _write_inputs(tmp_path, valid_from_ms=1)
    _without_explicit_account(history_db)

    complete = plan_speaker_attribution(
        history_db,
        knowledge_db,
        include_details=True,
        attribution_evidence=_single_account_evidence(history_db, knowledge_db),
    )["details"][0]
    assert complete["decision"]["status"] == "candidate"
    assert complete["attribution_basis"] == "single_account_inferred"
    assert complete["resolution_proposal"]["canonical_person_id"] == "person-a"
    assert complete["supporting_sources"][0]["account"] is None

    multiple_accounts = _single_account_evidence(
        history_db, knowledge_db, config_accounts=("test-account", "other-account")
    )
    multiple = plan_speaker_attribution(
        history_db,
        knowledge_db,
        include_details=True,
        attribution_evidence=multiple_accounts,
    )["details"][0]
    assert multiple["attribution_basis"] is None
    assert multiple["decision"]["status"] == "unresolved"

    incomplete = _single_account_evidence(
        history_db, knowledge_db, journal_complete=False
    )
    refused = plan_speaker_attribution(
        history_db,
        knowledge_db,
        include_details=True,
        attribution_evidence=incomplete,
    )["details"][0]
    assert refused["attribution_basis"] is None
    assert refused["decision"]["status"] == "unresolved"


def _typed(kind: str, value: str, account: str = "test-account") -> Identifier:
    return Identifier(channel="whatsapp", kind=kind, namespace=account, value=value)


def _binding(identifier: Identifier, *, start: int = 200, status: str = "active", person: str = "person-a") -> dict[str, Any]:
    return {
        "binding_id": f"binding-{person}",
        "channel": identifier.channel,
        "kind": identifier.kind,
        "namespace": identifier.namespace,
        "value": identifier.value,
        "person_id": person,
        "status": status,
        "valid_from_ms": start,
        "valid_until_ms": 0,
        "mapping_verified": 1,
        "evidence_ref": "owner-correction:test",
    }


def _observation_ref(
    at_ms: int, source_id: str = "source-1", copy_id: int = 1
) -> dict[str, Any]:
    return {
        "input": "history",
        "source_id": source_id,
        "sha256": "a" * 64,
        "locator": {"table": "history_event_copies", "copy_id": copy_id, "source_id": source_id},
        "time_ms": at_ms,
    }


def _planner_observation_ref(history_db: Path, at_ms: int, copy_id: int = 1) -> dict[str, Any]:
    with sqlite3.connect(history_db) as connection:
        row = connection.execute(
            "SELECT source_id,source_hash,locator_json,provenance_class,source_authority "
            "FROM history_event_copies WHERE copy_id=?", (copy_id,)
        ).fetchone()
    return {
        **_observation_ref(at_ms, row[0], copy_id),
        "sha256": _sha256(history_db),
        "source_hash": row[1],
        "source_locator": json.loads(row[2]),
        "provenance_class": row[3],
        "source_authority": row[4],
    }


def _append_planner_event(
    history_db: Path,
    *,
    event_id: str,
    copy_id: int,
    occurred_ms: int,
    phone_value: str,
    lid_value: str,
    name: str,
) -> None:
    source_id = f"source-{copy_id}"
    event = {
        "event_id": event_id,
        "revision": "1",
        "channel": "whatsapp",
        "account": "test-account",
        "chat_id": "test-chat",
        "sender_id_raw": phone_value,
        "participant_jid_raw": lid_value,
        "name_observations": [{
            "name": name,
            "raw_identifier": phone_value,
            "occurred_ms": occurred_ms,
            "observed_ms": occurred_ms,
            "time_certainty": "native",
            "provenance_class": "native",
        }],
        "occurred_ms": occurred_ms,
        "time_certainty": "native",
    }
    locator = json.dumps({"line": copy_id})
    source_hash = chr(ord("a") + copy_id) * 64
    with sqlite3.connect(history_db) as connection:
        connection.execute(
            "INSERT INTO history_event_details VALUES (?,?,?,?,?,?,?,?,?)",
            (event_id, "1", json.dumps(event), "message", "in", "native", "retained", None, 0),
        )
        connection.execute(
            "INSERT INTO history_event_copies VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (copy_id, event_id, "1", source_id, source_hash, locator,
             "inbound_archive_copy", "native", "inbound_archive_copy", "whatsapp",
             "test-account", "test-chat", "logical_copy"),
        )
        connection.execute(
            "INSERT INTO history_source_proofs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (event_id, "1", source_id, locator, None, "whatsapp", "test-chat",
             occurred_ms, "direct", "[]", "snapshot-1", "revision-1", None, None, 1, None),
        )


def _planner_evidence(
    history_db: Path,
    knowledge_db: Path,
    observations: list[dict[str, Any]],
    spans: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "input_hashes": {
            "history": _sha256(history_db),
            "knowledge": _sha256(knowledge_db),
        },
        "covered_period": {"start_ms": 1, "end_ms": 200},
        "account_inventories": [],
        "observations": observations,
        "continuity_spans": spans,
    }


def _add_lid_binding(knowledge_db: Path, lid_value: str, person_id: str) -> None:
    with sqlite3.connect(knowledge_db) as connection:
        connection.execute(
            "INSERT OR IGNORE INTO contacts VALUES (?, ?, NULL)",
            (person_id, f"Synthetic {person_id}"),
        )
        connection.execute(
            "INSERT INTO knowledge_identifier_bindings VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (f"binding-lid-{person_id}", "whatsapp", "lid", "test-account", lid_value,
             person_id, "active", 1, 0, 1, "owner-correction:test"),
        )


def test_unreviewed_or_out_of_period_span_cannot_hide_sender_conflict(
    tmp_path: Path,
) -> None:
    phone_value, lid_value = "15550000001@s.whatsapp.net", "abc123@lid"
    cases = ("unreviewed", "incomplete", "out_of_period", "conflicting", "compatible")
    for case in cases:
        case_root = tmp_path / case
        case_root.mkdir()
        history_db, knowledge_db = _write_inputs(
            case_root,
            valid_from_ms=1,
            participant_jid_raw=lid_value,
            name_observations=({
                "name": "Ada", "raw_identifier": phone_value,
                "occurred_ms": 100, "observed_ms": 100,
                "time_certainty": "native", "provenance_class": "native",
            },),
        )
        lid_person = "person-a" if case == "compatible" else "person-b"
        _add_lid_binding(knowledge_db, lid_value, lid_person)
        if case == "out_of_period":
            _append_planner_event(
                history_db, event_id="event-0", copy_id=2, occurred_ms=60,
                phone_value=phone_value, lid_value=lid_value, name="Ada",
            )
            ref = _planner_observation_ref(history_db, 60, copy_id=2)
            start_ms, end_ms = 50, 90
        else:
            ref = _planner_observation_ref(history_db, 100)
            start_ms, end_ms = 50, 150
        phone = _typed("phone_jid", phone_value)
        lid = _typed("lid", lid_value)
        observation = {
            "identifier": phone,
            "paired_lid": lid,
            "name": "Ada",
            "observed_ms": ref["time_ms"],
            "time_certainty": "native",
            "provenance_class": "native",
            "source_ref": ref,
        }
        span = {
            "identifier": phone,
            "lid_identifier": lid,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "reviewed": case != "unreviewed",
            "coverage_complete": case != "incomplete",
            "pair_name": "Ada",
            "canonical_person_id": "person-a",
            "binding_id": "binding-a",
            "evidence_refs": [ref],
        }
        result = plan_speaker_attribution(
            history_db,
            knowledge_db,
            include_details=True,
            attribution_evidence=_planner_evidence(
                history_db, knowledge_db, [observation], [span]
            ),
        )
        detail = next(row for row in result["details"] if row["event"]["event_id"] == "event-1")
        if case in {"unreviewed", "incomplete", "out_of_period"}:
            assert detail["decision"]["status"] != "confirmed"
            assert detail["attribution_basis"] is None
        elif case == "conflicting":
            assert detail["decision"]["status"] == "conflict"
            assert detail["attribution_basis"] is None
        else:
            assert detail["decision"]["status"] == "confirmed"
            assert detail["attribution_basis"] == "proven"


def test_phone_span_cannot_omit_pinned_name_or_pair_break(tmp_path: Path) -> None:
    phone_value, lid_value = "15550000001@s.whatsapp.net", "abc123@lid"
    for break_kind in ("name", "pair", "valid"):
        case_root = tmp_path / break_kind
        case_root.mkdir()
        history_db, knowledge_db = _write_inputs(
            case_root,
            valid_from_ms=0,
            participant_jid_raw=lid_value,
            name_observations=({
                "name": "Ada", "raw_identifier": phone_value,
                "occurred_ms": 100, "observed_ms": 100,
                "time_certainty": "native", "provenance_class": "native",
            },),
        )
        phone = _typed("phone_jid", phone_value)
        lid = _typed("lid", lid_value)
        if break_kind != "valid":
            _append_planner_event(
                history_db,
                event_id="event-2",
                copy_id=2,
                occurred_ms=110,
                phone_value=phone_value,
                lid_value=("def456@lid" if break_kind == "pair" else lid_value),
                name=("Bea" if break_kind == "name" else "Ada"),
            )
        ref = _planner_observation_ref(history_db, 100)
        observation = {
            "identifier": phone,
            "paired_lid": lid,
            "name": "Ada",
            "observed_ms": 100,
            "time_certainty": "native",
            "provenance_class": "native",
            "source_ref": ref,
        }
        span = {
            "identifier": phone,
            "lid_identifier": lid,
            "start_ms": 50,
            "end_ms": 150,
            "reviewed": True,
            "coverage_complete": True,
            "pair_name": "Ada",
            "canonical_person_id": "person-a",
            "binding_id": "binding-a",
            "evidence_refs": [ref],
        }
        details = plan_speaker_attribution(
            history_db,
            knowledge_db,
            include_details=True,
            attribution_evidence=_planner_evidence(
                history_db, knowledge_db, [observation], [span]
            ),
        )["details"]
        if break_kind == "valid":
            assert details[0]["attribution_basis"] == "observed_continuity"
        else:
            prior_detail = next(row for row in details if row["event"]["event_id"] == "event-1")
            break_detail = next(row for row in details if row["event"]["event_id"] == "event-2")
            assert prior_detail["attribution_basis"] != "observed_continuity"
            assert break_detail["attribution_basis"] != "observed_continuity"
            assert prior_detail["resolution_proposal"] is None
            assert break_detail["resolution_proposal"] is None


def test_explicit_source_account_stays_proven_with_supplementary_evidence(
    tmp_path: Path,
) -> None:
    history_db, knowledge_db = _write_inputs(
        tmp_path, event_account=None, copy_account="test-account", valid_from_ms=1
    )
    detail = plan_speaker_attribution(
        history_db,
        knowledge_db,
        include_details=True,
        attribution_evidence=_single_account_evidence(history_db, knowledge_db),
    )["details"][0]
    assert detail["decision"]["status"] == "confirmed"
    assert detail["attribution_basis"] == "proven"
    assert detail["basis_components"] == ["proven"]
    assert "inferred_scope" not in detail


def test_lid_continuity_covers_prebinding_events() -> None:
    identifier = _typed("lid", "abc123@lid")
    observations = [
        {"identifier": identifier, "observed_ms": at_ms, "time_certainty": "native",
         "provenance_class": "native", "source_ref": _observation_ref(at_ms)}
        for at_ms in (50, 150)
    ]
    result = resolve_attribution_binding(
        identifier, 100, bindings=[_binding(identifier, start=0)], canonical_ids={},
        observations=observations, continuity_spans=[]
    )
    assert result["status"] == "candidate"
    assert result["basis"] == "observed_continuity"
    assert result["canonical_person_id"] == "person-a"

    future = resolve_attribution_binding(
        identifier, 100, bindings=[_binding(identifier, start=200)], canonical_ids={},
        observations=observations, continuity_spans=[]
    )
    assert future["status"] == "unresolved"
    assert future["basis"] is None
    ended = _binding(identifier, start=1, status="ended")
    ended["valid_until_ms"] = 90
    after_end = resolve_attribution_binding(
        identifier, 100, bindings=[ended], canonical_ids={}, observations=observations,
        continuity_spans=[]
    )
    assert after_end["status"] == "unresolved"
    assert after_end["basis"] is None

    unknown_start_ended = _binding(identifier, start=0, status="ended")
    unknown_start_ended["valid_until_ms"] = 90
    after_unknown_start_end = resolve_attribution_binding(
        identifier, 100, bindings=[unknown_start_ended], canonical_ids={}, observations=observations,
        continuity_spans=[]
    )
    assert after_unknown_start_end["status"] == "unresolved"
    assert after_unknown_start_end["basis"] is None
    unknown_start_ended["valid_until_ms"] = 0
    unknown_end = resolve_attribution_binding(
        identifier, 100, bindings=[unknown_start_ended], canonical_ids={}, observations=observations,
        continuity_spans=[]
    )
    assert unknown_end["status"] == "unresolved"
    assert unknown_end["basis"] is None
    unknown_start_ended["valid_until_ms"] = 120
    within_unknown_start_end = resolve_attribution_binding(
        identifier, 100, bindings=[unknown_start_ended], canonical_ids={},
        observations=[observations[0], {
            **observations[1], "observed_ms": 110, "source_ref": _observation_ref(110)
        }], continuity_spans=[]
    )
    assert within_unknown_start_end["status"] == "candidate"
    assert within_unknown_start_end["basis"] == "observed_continuity"

    competing = resolve_attribution_binding(
        identifier, 100, bindings=[_binding(identifier, start=0), _binding(identifier, start=0, person="person-b")],
        canonical_ids={}, observations=observations, continuity_spans=[]
    )
    assert competing["canonical_person_id"] is None
    assert competing["reason"] == "binding_start_conflict"


def test_phone_continuity_stops_at_pair_name_or_coverage_break() -> None:
    phone = _typed("phone_jid", "15550000001@s.whatsapp.net")
    lid = _typed("lid", "abc123@lid")
    ref = _observation_ref(100)
    observation = {
        "identifier": phone, "paired_lid": lid, "name": "Ada", "observed_ms": 100,
        "time_certainty": "native", "provenance_class": "native", "source_ref": ref,
    }
    span = {
        "identifier": phone, "lid_identifier": lid, "start_ms": 50, "end_ms": 150,
        "reviewed": True, "coverage_complete": True, "pair_name": "Ada",
        "canonical_person_id": "person-a", "binding_id": "binding-person-a",
        "evidence_refs": [ref],
    }
    args = dict(identifier=phone, at_ms=100, bindings=[_binding(phone, start=0)], canonical_ids={},
                observations=[observation], continuity_spans=[span])
    assert resolve_attribution_binding(**args)["basis"] == "observed_continuity"
    name_break = {**observation, "name": "Bea"}
    assert resolve_attribution_binding(**{**args, "observations": [name_break]})["reason"] == "phone_continuity_pair_break"
    coverage_break = {**span, "evidence_refs": []}
    # Empty evidence is malformed at the schema boundary; a nonmatching locator is a known gap.
    coverage_break["evidence_refs"] = [{**ref, "locator": {"line": 9}}]
    assert resolve_attribution_binding(**{**args, "continuity_spans": [coverage_break]})["reason"] == "phone_continuity_coverage_break"
    weak_provenance = {**observation, "provenance_class": "recovered_text"}
    assert resolve_attribution_binding(**{**args, "observations": [weak_provenance]})["reason"] == "phone_continuity_pair_break"
    no_source = {**observation, "source_ref": {}}
    assert resolve_attribution_binding(**{**args, "observations": [no_source]})["reason"] == "phone_continuity_pair_break"
    ended_stub = _binding(phone, start=50, status="ended")
    assert resolve_attribution_binding(**{**args, "bindings": [ended_stub]})["reason"] == "binding_end_unknown"

    unknown_start_ended = _binding(phone, start=0, status="ended")
    unknown_start_ended["valid_until_ms"] = 90
    after_unknown_start_end = resolve_attribution_binding(
        **{**args, "bindings": [unknown_start_ended]}
    )
    assert after_unknown_start_end["basis"] is None
    unknown_start_ended["valid_until_ms"] = 0
    unknown_end = resolve_attribution_binding(
        **{**args, "bindings": [unknown_start_ended]}
    )
    assert unknown_end["basis"] is None
    unknown_start_ended["valid_until_ms"] = 120
    within_unknown_start_end = resolve_attribution_binding(
        **{**args, "bindings": [unknown_start_ended]}
    )
    assert within_unknown_start_end["basis"] == "observed_continuity"


def test_name_free_observation_time_certainty_matches_pinned_event(tmp_path: Path) -> None:
    history_db, knowledge_db = _write_inputs(
        tmp_path, time_certainty="capture_time_approx", valid_from_ms=0
    )
    evidence = _single_account_evidence(history_db, knowledge_db)
    phone = _typed("phone_jid", "15550000001@s.whatsapp.net")
    evidence["observations"] = [{
        "identifier": phone, "observed_ms": 100, "time_certainty": "capture_time_approx",
        "provenance_class": "native", "source_ref": _planner_observation_ref(history_db, 100),
    }]
    plan_speaker_attribution(
        history_db, knowledge_db, include_details=True, attribution_evidence=evidence
    )

    evidence["observations"][0]["time_certainty"] = "native"
    with pytest.raises(IdentityAuditError, match="time certainty"):
        plan_speaker_attribution(
            history_db, knowledge_db, include_details=True, attribution_evidence=evidence
        )


def test_missing_event_time_with_evidence_stays_unresolved_and_unchanged(tmp_path: Path) -> None:
    history_db, knowledge_db = _write_inputs(
        tmp_path, occurred_ms=None, event_account=None, copy_account=None, valid_from_ms=0
    )
    evidence = {
        "schema_version": 1,
        "input_hashes": {"history": _sha256(history_db), "knowledge": _sha256(knowledge_db)},
        "covered_period": {"start_ms": 1, "end_ms": 200},
        "account_inventories": [],
        "observations": [],
        "continuity_spans": [],
    }
    history_before = _sha256(history_db)
    knowledge_before = _sha256(knowledge_db)

    result = plan_speaker_attribution(
        history_db, knowledge_db, include_details=True, attribution_evidence=evidence
    )

    detail = result["details"][0]
    assert result["aggregate"]["decision_counts"]["unresolved"] == 1
    assert result["aggregate"]["reason_counts"]["unresolved"] == {"source_unproven": 1}
    assert result["aggregate"]["source_unproven_diagnostics"] == {"event_time_unknown": 1}
    assert detail["decision"] == {"status": "unresolved", "reason": "source_unproven"}
    assert detail["event"]["occurred_ms"] is None
    assert detail["resolution_proposal"] is None
    assert "inferred_scope" not in detail
    assert _sha256(history_db) == history_before
    assert _sha256(knowledge_db) == knowledge_before


def test_owner_correction_beats_continuity() -> None:
    phone = _typed("phone_jid", "15550000001@s.whatsapp.net")
    lid = _typed("lid", "abc123@lid")
    ref = _observation_ref(100)
    contradictory_span = {
        "identifier": phone, "lid_identifier": lid, "start_ms": 50, "end_ms": 150,
        "reviewed": True, "coverage_complete": True, "pair_name": "Someone else",
        "canonical_person_id": "person-a", "binding_id": "binding-person-a",
        "evidence_refs": [ref],
    }
    result = resolve_attribution_binding(
        phone, 100, bindings=[_binding(phone, start=1)], canonical_ids={},
        observations=[], continuity_spans=[contradictory_span]
    )
    assert result["status"] == "resolved"
    assert result["basis"] == "proven"
    assert result["binding_id"] == "binding-person-a"


def test_combined_inference_uses_weakest_required_basis(tmp_path: Path) -> None:
    history_db, knowledge_db = _write_inputs(
        tmp_path, valid_from_ms=0, participant_jid_raw="abc123@lid",
        name_observations=({
            "name": "Ada", "raw_identifier": "15550000001@s.whatsapp.net",
            "occurred_ms": 100, "observed_ms": 100, "time_certainty": "native",
            "provenance_class": "native",
        },),
    )
    _without_explicit_account(history_db)
    evidence = _single_account_evidence(history_db, knowledge_db)
    phone = _typed("phone_jid", "15550000001@s.whatsapp.net")
    lid = _typed("lid", "abc123@lid")
    ref = _planner_observation_ref(history_db, 100)
    evidence["observations"] = [{
        "identifier": phone, "paired_lid": lid, "name": "Ada", "observed_ms": 100,
        "time_certainty": "native", "provenance_class": "native", "source_ref": ref,
    }]
    evidence["continuity_spans"] = [{
        "identifier": phone, "lid_identifier": lid, "start_ms": 50, "end_ms": 150,
        "reviewed": True, "coverage_complete": True, "pair_name": "Ada",
        "canonical_person_id": "person-a", "binding_id": "binding-a", "evidence_refs": [ref],
    }]
    detail = plan_speaker_attribution(
        history_db, knowledge_db, include_details=True, attribution_evidence=evidence
    )["details"][0]
    assert detail["decision"]["status"] == "candidate"
    assert detail["attribution_basis"] == "single_account_inferred"
    assert detail["basis_components"] == ["single_account_inferred", "observed_continuity"]


def test_phone_observation_requires_exact_source_time_and_pair_metadata(tmp_path: Path) -> None:
    history_db, knowledge_db = _write_inputs(tmp_path, valid_from_ms=0)
    _without_explicit_account(history_db)
    evidence = _single_account_evidence(history_db, knowledge_db)
    phone = _typed("phone_jid", "15550000001@s.whatsapp.net")
    lid = _typed("lid", "abc123@lid")
    ref = {**_planner_observation_ref(history_db, 99)}
    evidence["observations"] = [{
        "identifier": phone, "paired_lid": lid, "name": "Invented name", "observed_ms": 99,
        "time_certainty": "native", "provenance_class": "native", "source_ref": ref,
    }]
    evidence["continuity_spans"] = [{
        "identifier": phone, "lid_identifier": lid, "start_ms": 50, "end_ms": 101,
        "reviewed": True, "coverage_complete": True, "pair_name": "Invented name",
        "canonical_person_id": "person-a", "binding_id": "binding-a", "evidence_refs": [ref],
    }]
    with pytest.raises(IdentityAuditError):
        plan_speaker_attribution(
            history_db, knowledge_db, include_details=True, attribution_evidence=evidence
        )


def test_account_inventory_cannot_omit_a_pinned_second_account(tmp_path: Path) -> None:
    history_db, knowledge_db = _write_inputs(tmp_path, valid_from_ms=1)
    _without_explicit_account(history_db)
    with sqlite3.connect(history_db) as connection:
        connection.execute(
            "INSERT INTO history_event_copies VALUES (3,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("event-1", "1", "other-account-source", "d" * 64, '{"line":3}',
             "inbound_archive_copy", "native", "inbound_archive_copy", "whatsapp",
             "other-account", "test-chat", "logical_copy"),
        )
    evidence = _single_account_evidence(history_db, knowledge_db)
    with pytest.raises(IdentityAuditError):
        plan_speaker_attribution(
            history_db, knowledge_db, include_details=True, attribution_evidence=evidence
        )


def test_weaker_attribution_preserves_denials_and_input_bytes(tmp_path: Path) -> None:
    denied_root = tmp_path / "denied"
    denied_root.mkdir()
    history_db, knowledge_db = _write_inputs(denied_root, denied=1)
    _without_explicit_account(history_db)
    evidence = _single_account_evidence(history_db, knowledge_db)
    before = (_sha256(history_db), _sha256(knowledge_db))
    result = plan_speaker_attribution(history_db, knowledge_db, include_details=True,
                                      attribution_evidence=evidence)
    assert result["aggregate"]["denominator"] == 0
    assert result["aggregate"]["decision_counts"]["confirmed"] == 0
    assert (_sha256(history_db), _sha256(knowledge_db)) == before
    assert not Path(f"{history_db}-wal").exists()
    assert not Path(f"{history_db}-shm").exists()

    audience_root = tmp_path / "unknown-audience"
    audience_root.mkdir()
    live_history, live_knowledge = _write_inputs(audience_root)
    _without_explicit_account(live_history)
    with sqlite3.connect(live_history) as connection:
        connection.execute("UPDATE history_source_proofs SET audience_status='unknown'")
    evidence = _single_account_evidence(live_history, live_knowledge)
    before = (_sha256(live_history), _sha256(live_knowledge))
    result = plan_speaker_attribution(live_history, live_knowledge, include_details=True,
                                      attribution_evidence=evidence)
    assert result["aggregate"]["unknown_audience_proposal_counts"] == {"candidate": 1}
    assert result["details"][0]["supporting_sources"][0]["audience_status"] == "unknown"
    assert (_sha256(live_history), _sha256(live_knowledge)) == before
    assert not Path(f"{live_history}-wal").exists()
    assert not Path(f"{live_history}-shm").exists()
