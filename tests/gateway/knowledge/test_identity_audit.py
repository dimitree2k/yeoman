from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from typer.testing import CliRunner
from yeoman_gateway.cli.commands import app
from yeoman_gateway.knowledge._identity_audit import IdentityAuditError, audit_person_stores


def _insert(connection: sqlite3.Connection, table: str, row: dict[str, Any]) -> None:
    columns = tuple(row)
    placeholders = ", ".join("?" for _ in columns)
    connection.execute(
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
        tuple(row[column] for column in columns),
    )


def _person(person_id: str, name: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": person_id,
        "display_name": name,
        "phone_number": None,
        "is_owner": 0,
        "created_at": "2026-01-01",
        "updated_at": "2026-01-01",
        **extra,
    }


def _binding(
    binding_id: str,
    person_id: str,
    *,
    value: str = "491555000001@s.whatsapp.net",
    status: str = "active",
    valid_from_ms: int = 100,
    valid_until_ms: int = 0,
) -> dict[str, Any]:
    return {
        "binding_id": binding_id,
        "channel": "whatsapp",
        "kind": "phone_jid",
        "namespace": "wa-account",
        "value": value,
        "person_id": person_id,
        "status": status,
        "valid_from_ms": valid_from_ms,
        "valid_until_ms": valid_until_ms,
        "observed_at_ms": valid_from_ms,
        "evidence_ref": f"synthetic:{binding_id}",
        "mapping_verified": 0,
        "revision": 1,
        "created_ms": valid_from_ms,
        "updated_ms": valid_from_ms,
    }


def _write_db(
    path: Path,
    *,
    knowledge: bool,
    people: tuple[dict[str, Any], ...] = (),
    identifiers: tuple[dict[str, Any], ...] = (),
    aliases: tuple[dict[str, Any], ...] = (),
    fields: tuple[dict[str, Any], ...] = (),
    bindings: tuple[dict[str, Any], ...] = (),
) -> None:
    connection = sqlite3.connect(path)
    columns = (
        "id TEXT PRIMARY KEY, display_name TEXT NOT NULL, phone_number TEXT, "
        "is_owner INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL"
    )
    if knowledge:
        columns += (
            ", revision INTEGER DEFAULT 1, status TEXT DEFAULT 'active', "
            "preferred_name TEXT, preferred_name_source TEXT, "
            "preferred_name_visibility TEXT DEFAULT 'public'"
        )
    connection.execute(f"CREATE TABLE contacts ({columns})")
    connection.execute(
        "CREATE TABLE contact_identifiers (channel TEXT, identifier TEXT, contact_id TEXT, kind TEXT)"
    )
    alias_columns = (
        "id INTEGER, contact_id TEXT, alias TEXT, source TEXT, first_seen TEXT, last_seen TEXT"
    )
    if knowledge:
        alias_columns += (
            ", normalized_alias TEXT DEFAULT '', scope_key TEXT DEFAULT 'global', "
            "status TEXT DEFAULT 'observed', alias_kind TEXT DEFAULT 'other_name', "
            "mapping_retracted INTEGER DEFAULT 0"
        )
    connection.execute(f"CREATE TABLE contact_aliases ({alias_columns})")
    connection.execute(
        "CREATE TABLE contact_fields (id INTEGER, contact_id TEXT, kind TEXT, value TEXT, "
        "label TEXT, created_at TEXT, updated_at TEXT)"
    )
    if knowledge:
        connection.execute(
            "CREATE TABLE knowledge_identifier_bindings (binding_id TEXT, channel TEXT, "
            "kind TEXT, namespace TEXT, value TEXT, person_id TEXT, status TEXT, "
            "valid_from_ms INTEGER, valid_until_ms INTEGER, observed_at_ms INTEGER, "
            "evidence_ref TEXT, mapping_verified INTEGER, revision INTEGER, created_ms INTEGER, "
            "updated_ms INTEGER)"
        )
        connection.execute(
            "CREATE TABLE knowledge_identity_redirects (operation_id TEXT, source_id TEXT, "
            "target_id TEXT, active INTEGER)"
        )
    for person in people:
        _insert(connection, "contacts", person)
    for row in identifiers:
        _insert(connection, "contact_identifiers", row)
    for row in aliases:
        _insert(connection, "contact_aliases", row)
    for row in fields:
        _insert(connection, "contact_fields", row)
    for row in bindings:
        _insert(connection, "knowledge_identifier_bindings", row)
    connection.commit()
    connection.close()


def _empty_stores(tmp_path: Path) -> tuple[Path, Path]:
    legacy = tmp_path / "legacy.db"
    knowledge = tmp_path / "knowledge.db"
    _write_db(legacy, knowledge=False, people=(_person("person-1", "Synthetic Person"),))
    _write_db(
        knowledge,
        knowledge=True,
        people=(_person("person-1", "Synthetic Person", preferred_name=None, preferred_name_source=None),),
    )
    return knowledge, legacy


def test_field_reconciliation_includes_legacy_only_contact_fields(tmp_path: Path) -> None:
    knowledge, legacy = _empty_stores(tmp_path)
    connection = sqlite3.connect(legacy)
    connection.execute(
        "INSERT INTO contact_fields VALUES (1, 'person-1', 'test', 'legacy-only', NULL, "
        "'2026-01-01', '2026-01-01')"
    )
    connection.commit()
    connection.close()

    report = audit_person_stores(knowledge, legacy)

    fields = report["aggregate"]["field_reconciliation"]["contact_fields"]
    assert fields["legacy_only_rows"] == 1
    assert fields["knowledge_only_rows"] == 0
    assert report["aggregate"]["field_reconciliation"]["contacts"]["changed_rows"] == 0


def test_same_nickname_for_two_people_is_a_name_conflict_not_a_merge_candidate(
    tmp_path: Path,
) -> None:
    knowledge, legacy = _empty_stores(tmp_path)
    connection = sqlite3.connect(knowledge)
    _insert(connection, "contacts", _person("person-2", "Other Person", preferred_name=None))
    for alias_id, person_id in ((1, "person-1"), (2, "person-2")):
        _insert(
            connection,
            "contact_aliases",
            {
                "id": alias_id,
                "contact_id": person_id,
                "alias": "Ace",
                "source": "chat_nickname",
                "first_seen": "2026-01-01",
                "last_seen": "2026-01-01",
            },
        )
    connection.commit()
    connection.close()

    report = audit_person_stores(knowledge, legacy)

    assert report["aggregate"]["people"]["name_conflict_groups"] == 1
    assert report["aggregate"]["evidence_candidates"]["count"] == 0
    assert report["details"]["name_conflicts"][0]["reason"] == "shared_name_is_not_identity_evidence"


def test_renamed_account_keeps_both_name_variants_on_one_bound_person(tmp_path: Path) -> None:
    knowledge, legacy = _empty_stores(tmp_path)
    connection = sqlite3.connect(knowledge)
    connection.execute(
        "INSERT INTO knowledge_identifier_bindings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        tuple(_binding("binding-1", "person-1").values()),
    )
    connection.commit()
    connection.close()
    observations = [
        {
            "event_id": f"event-{index}",
            "revision": 1,
            "channel": "whatsapp",
            "account": "wa-account",
            "occurred_ms": timestamp,
            "time_certainty": "native",
            "raw_identifier": "491555000001@s.whatsapp.net",
            "name": name,
            "provenance_class": "native",
        }
        for index, (timestamp, name) in enumerate(((200, "Old Display"), (300, "New Display")))
    ]

    report = audit_person_stores(knowledge, legacy, observations=observations)

    assert report["aggregate"]["observations"]["name_variant_groups"] == 1
    assert report["aggregate"]["observations"]["resolved"] == 2
    assert report["details"]["resolved_observations"][0]["person_id"] == "person-1"


def test_recycled_identifier_resolves_only_inside_its_proven_period(tmp_path: Path) -> None:
    knowledge, legacy = _empty_stores(tmp_path)
    connection = sqlite3.connect(knowledge)
    _insert(connection, "contacts", _person("person-2", "Replacement", preferred_name=None))
    _insert(
        connection,
        "knowledge_identifier_bindings",
        _binding("binding-old", "person-1", status="ended", valid_from_ms=100, valid_until_ms=200),
    )
    _insert(
        connection,
        "knowledge_identifier_bindings",
        _binding("binding-new", "person-2", valid_from_ms=200),
    )
    connection.commit()
    connection.close()
    observations = [
        {
            "event_id": "before-recycle",
            "revision": 1,
            "channel": "whatsapp",
            "account": "wa-account",
            "occurred_ms": 150,
            "time_certainty": "native",
            "raw_identifier": "491555000001@s.whatsapp.net",
        },
        {
            "event_id": "after-recycle",
            "revision": 1,
            "channel": "whatsapp",
            "account": "wa-account",
            "occurred_ms": 250,
            "time_certainty": "native",
            "raw_identifier": "491555000001@s.whatsapp.net",
        },
    ]

    report = audit_person_stores(knowledge, legacy, observations=observations)

    resolved = {item["event_id"]: item["person_id"] for item in report["details"]["resolved_observations"]}
    assert resolved == {"before-recycle": "person-1", "after-recycle": "person-2"}
    assert report["aggregate"]["bindings"]["reused_identifier_groups"] == 1


def test_ended_binding_with_unknown_end_does_not_resolve_historically(tmp_path: Path) -> None:
    knowledge, legacy = _empty_stores(tmp_path)
    connection = sqlite3.connect(knowledge)
    _insert(
        connection,
        "knowledge_identifier_bindings",
        _binding("binding-ended-unknown", "person-1", status="ended", valid_until_ms=0),
    )
    connection.commit()
    connection.close()

    report = audit_person_stores(
        knowledge,
        legacy,
        observations=[
            {
                "event_id": "inside-unknown-end",
                "revision": 1,
                "channel": "whatsapp",
                "account": "wa-account",
                "occurred_ms": 150,
                "time_certainty": "native",
                "raw_identifier": "491555000001@s.whatsapp.net",
            }
        ],
    )

    assert report["aggregate"]["observations"]["unresolved_by_reason"] == {
        "binding_end_unknown": 1
    }


def test_primary_and_preserved_copy_share_one_event_revision_denominator(tmp_path: Path) -> None:
    knowledge, legacy = _empty_stores(tmp_path)
    connection = sqlite3.connect(knowledge)
    _insert(connection, "knowledge_identifier_bindings", _binding("binding-1", "person-1"))
    connection.commit()
    connection.close()
    observations = [
        {
            "event_id": "same-event",
            "revision": 1,
            "channel": "whatsapp",
            "account": "wa-account",
            "occurred_ms": 150,
            "time_certainty": "native",
            "raw_identifier": "491555000001@s.whatsapp.net",
        },
        {
            "event_id": "same-event",
            "revision": 1,
            "channel": "whatsapp",
            "account": "wa-account",
            "occurred_ms": 150,
            "time_certainty": "native",
            "sender_raw": "491555999999@s.whatsapp.net",
            "missing_copy_fields": [],
        },
    ]

    report = audit_person_stores(knowledge, legacy, observations=observations)

    assert report["aggregate"]["observations"]["records"] == 2
    assert report["aggregate"]["observations"]["resolution_by_record_type"] == {
        "normalized_primary": {"resolved": 1, "unresolved": 0},
        "preserved_copy": {"resolved": 0, "unresolved": 1},
    }
    assert report["aggregate"]["observations"]["unique_event_revisions"] == 1
    assert report["aggregate"]["observations"]["unassigned_event_revisions"] == 0


def test_two_people_resolving_for_one_event_are_reported_as_conflict(tmp_path: Path) -> None:
    knowledge, legacy = _empty_stores(tmp_path)
    connection = sqlite3.connect(knowledge)
    _insert(connection, "contacts", _person("person-2", "Other Person", preferred_name=None))
    _insert(connection, "knowledge_identifier_bindings", _binding("binding-1", "person-1"))
    _insert(
        connection,
        "knowledge_identifier_bindings",
        _binding("binding-2", "person-2", value="491555000002@s.whatsapp.net"),
    )
    connection.commit()
    connection.close()
    observations = [
        {
            "event_id": "conflicted-event",
            "revision": 1,
            "channel": "whatsapp",
            "account": "wa-account",
            "occurred_ms": 150,
            "time_certainty": "native",
            "raw_identifier": identifier,
        }
        for identifier in (
            "491555000001@s.whatsapp.net",
            "491555000002@s.whatsapp.net",
        )
    ]

    report = audit_person_stores(knowledge, legacy, observations=observations)

    assert report["aggregate"]["observations"]["unique_event_revisions"] == 1
    assert report["aggregate"]["observations"]["unassigned_event_revisions"] == 0
    assert report["aggregate"]["observations"]["conflicting_event_revisions"] == 1
    assert {
        row["person_id"] for row in report["details"]["resolved_observations"]
    } == {"person-1", "person-2"}


def test_redirected_pair_resolves_and_owner_inventory_includes_merged_members(tmp_path: Path) -> None:
    knowledge, legacy = _empty_stores(tmp_path)
    connection = sqlite3.connect(knowledge)
    connection.execute("UPDATE contacts SET is_owner = 1 WHERE id = 'person-1'")
    _insert(connection, "contacts", _person("person-2", "Canonical Person", preferred_name=None))
    lid = _binding("binding-lid", "person-1", value="12345678901234@lid")
    lid["kind"] = "lid"
    _insert(connection, "knowledge_identifier_bindings", lid)
    _insert(connection, "knowledge_identifier_bindings", _binding("binding-phone", "person-2"))
    _insert(
        connection,
        "knowledge_identity_redirects",
        {"operation_id": "merge-1", "source_id": "person-1", "target_id": "person-2", "active": 1},
    )
    connection.commit()
    connection.close()

    report = audit_person_stores(
        knowledge,
        legacy,
        observations=[
            {
                "event_id": "merged-native-pair",
                "revision": 1,
                "channel": "whatsapp",
                "account": "wa-account",
                "occurred_ms": 250,
                "time_certainty": "native",
                "sender_id_raw": "12345678901234@lid",
                "participant_jid_raw": "491555000001@s.whatsapp.net",
                "missing_copy_fields": [],
                "provenance_class": "native",
                "source_hash": "b" * 64,
                "source_id": "synthetic-source",
                "locator": {"row": 2},
            }
        ],
    )

    assert report["aggregate"]["observations"]["resolved"] == 1
    assert report["aggregate"]["observations"]["conflicting_event_revisions"] == 0
    assert report["aggregate"]["evidence_candidates"]["count"] == 0
    owner = report["details"]["owner_records"][0]
    assert {row["person_id"] for row in owner["active_bindings"]} == {"person-1", "person-2"}


def test_unknown_start_competing_binding_blocks_known_period_assignment(tmp_path: Path) -> None:
    knowledge, legacy = _empty_stores(tmp_path)
    connection = sqlite3.connect(knowledge)
    _insert(connection, "contacts", _person("person-2", "Other Person", preferred_name=None))
    _insert(
        connection,
        "knowledge_identifier_bindings",
        _binding("binding-unknown", "person-1", valid_from_ms=0),
    )
    _insert(
        connection,
        "knowledge_identifier_bindings",
        _binding("binding-known", "person-2", valid_from_ms=100),
    )
    connection.commit()
    connection.close()

    report = audit_person_stores(
        knowledge,
        legacy,
        observations=[
            {
                "event_id": "competing-periods",
                "revision": 1,
                "channel": "whatsapp",
                "account": "wa-account",
                "occurred_ms": 250,
                "time_certainty": "native",
                "raw_identifier": "491555000001@s.whatsapp.net",
            }
        ],
    )

    assert report["aggregate"]["observations"]["resolved"] == 0
    assert report["aggregate"]["observations"]["unresolved_by_reason"] == {
        "binding_start_conflict": 1
    }


def test_wal_database_is_refused_without_changing_its_sidecar_bundle(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    writer = sqlite3.connect(source)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("CREATE TABLE example (id INTEGER)")
    writer.execute("INSERT INTO example VALUES (1)")
    writer.commit()
    frozen = tmp_path / "frozen.db"
    frozen.write_bytes(source.read_bytes())
    frozen_wal = Path(f"{source}-wal")
    assert frozen_wal.exists()
    Path(f"{frozen}-wal").write_bytes(frozen_wal.read_bytes())
    before = {path.name: path.read_bytes() for path in tmp_path.glob("frozen.db*")}

    try:
        audit_person_stores(frozen)
    except IdentityAuditError:
        pass
    else:
        raise AssertionError("WAL-backed input must be refused rather than partially read")
    finally:
        writer.close()

    after = {path.name: path.read_bytes() for path in tmp_path.glob("frozen.db*")}
    assert after == before


def test_native_history_time_labels_resolve_but_approximate_time_stays_unresolved(
    tmp_path: Path,
) -> None:
    knowledge, legacy = _empty_stores(tmp_path)
    connection = sqlite3.connect(knowledge)
    _insert(connection, "knowledge_identifier_bindings", _binding("binding-1", "person-1"))
    connection.commit()
    connection.close()
    observations = [
        {
            "event_id": f"time-{certainty}",
            "revision": 1,
            "channel": "whatsapp",
            "account": "wa-account",
            "occurred_ms": 150,
            "time_certainty": certainty,
            "sender_raw": "491555000001@s.whatsapp.net",
            "principal": "not_identity_authority",
        }
        for certainty in ("native", "provider_timestamp", "capture_time_approx")
    ]

    report = audit_person_stores(knowledge, legacy, observations=observations)

    assert {row["event_id"] for row in report["details"]["resolved_observations"]} == {
        "time-native",
        "time-provider_timestamp",
    }
    assert report["aggregate"]["observations"]["unresolved_by_reason"] == {
        "time_unknown": 1
    }


def test_native_same_message_pair_is_a_candidate_only_when_bindings_disagree(tmp_path: Path) -> None:
    knowledge, legacy = _empty_stores(tmp_path)
    connection = sqlite3.connect(knowledge)
    _insert(connection, "contacts", _person("person-2", "Replacement", preferred_name=None))
    phone = _binding("binding-phone", "person-2")
    lid = _binding("binding-lid", "person-1", value="12345678901234@lid")
    lid["kind"] = "lid"
    _insert(connection, "knowledge_identifier_bindings", phone)
    _insert(connection, "knowledge_identifier_bindings", lid)
    connection.commit()
    connection.close()

    report = audit_person_stores(
        knowledge,
        legacy,
        observations=[
            {
                "event_id": "native-pair-event",
                "revision": 1,
                "channel": "whatsapp",
                "account": "wa-account",
                "occurred_ms": 250,
                "time_certainty": "native",
                "sender_id_raw": "12345678901234@lid",
                "participant_jid_raw": "491555000001@s.whatsapp.net",
                "missing_copy_fields": [],
                "provenance_class": "native",
                "source_hash": "a" * 64,
                "source_id": "synthetic-source",
                "locator": {"row": 1},
            }
        ],
    )

    assert report["aggregate"]["evidence_candidates"]["count"] == 1
    candidate = report["details"]["evidence_candidates"][0]
    assert candidate["candidate_type"] == "coobserved_identifiers_bound_to_different_people"
    assert candidate["decision"].startswith("owner_review_required")


def test_unknown_sender_and_newsletter_address_stay_unresolved(tmp_path: Path) -> None:
    knowledge, legacy = _empty_stores(tmp_path)
    connection = sqlite3.connect(knowledge)
    _insert(
        connection,
        "contact_identifiers",
        {
            "channel": "whatsapp",
            "identifier": "120363000000000000@newsletter",
            "contact_id": "person-1",
            "kind": "phone_jid",
        },
    )
    connection.commit()
    connection.close()
    observations = [
        {
            "event_id": "unknown-sender",
            "revision": 1,
            "channel": "whatsapp",
            "account": "wa-account",
            "occurred_ms": 200,
            "time_certainty": "native",
            "raw_identifier": "491555999999@s.whatsapp.net",
        }
    ]

    report = audit_person_stores(knowledge, legacy, observations=observations)

    assert report["aggregate"]["observations"]["unresolved_by_reason"]["no_binding"] == 1
    assert report["aggregate"]["identifier_without_binding"]["knowledge"]["categories"][
        "non_person_newsletter"
    ] == 1


def test_person_audit_cli_prints_only_aggregate_and_does_not_change_inputs(tmp_path: Path) -> None:
    knowledge, legacy = _empty_stores(tmp_path)
    before = (knowledge.read_bytes(), legacy.read_bytes())

    result = CliRunner().invoke(
        app,
        [
            "knowledge",
            "person-audit",
            "--knowledge-db",
            str(knowledge),
            "--legacy-db",
            str(legacy),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Synthetic Person" not in result.output
    assert "person-1" not in result.output
    assert result.output.lstrip().startswith("{")
    assert (knowledge.read_bytes(), legacy.read_bytes()) == before
