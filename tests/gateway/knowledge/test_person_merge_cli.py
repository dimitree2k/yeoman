"""Owner CLI for id-based person merges and their undo.

The chat merge tool finds people by name, which is ambiguous exactly for duplicates that
share a name.  The CLI takes exact person ids, is a dry run unless ``--apply`` is given,
and never touches an owner-flagged person: owner records are repaired separately.  All
identifiers are synthetic.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from typer.testing import CliRunner
from yeoman_gateway.cli.knowledge_commands import knowledge_app
from yeoman_gateway.knowledge.api import open_knowledge_store
from yeoman_gateway.knowledge.authority import FakePolicyAuthority, FakeSourceAuthority
from yeoman_gateway.knowledge.models import Identifier, TrustedIdentityObservation

PHONE_JID = "491520000001@s.whatsapp.net"
LID = "100000000000001@lid"
OWNER_PHONE = "491520000009"


@contextmanager
def _sql(db: Path) -> Iterator[sqlite3.Connection]:
    """Commit and *close* deterministically.

    ``with sqlite3.connect(...)`` commits but leaves closing to the garbage collector;
    the last close on a WAL database checkpoints it and changes the file bytes at an
    arbitrary moment, which breaks the "a dry run writes nothing" byte comparisons.
    """
    connection = sqlite3.connect(db)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def _observation(ref: str, *identifiers: Identifier, verified: bool) -> TrustedIdentityObservation:
    return TrustedIdentityObservation(
        identifiers=identifiers, evidence_ref=ref, mapping_verified=verified
    )


def _phone() -> Identifier:
    return Identifier("whatsapp", "phone_jid", PHONE_JID, "default")


def _lid() -> Identifier:
    return Identifier("whatsapp", "lid", LID, "default")


def _open(db: Path, authority: FakeSourceAuthority):
    return open_knowledge_store(
        db,
        workspace_id="person-merge-cli-tests",
        source_authority=authority,
        policy_authority=FakePolicyAuthority(admins={f"whatsapp:{OWNER_PHONE}"}),
    )


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A phone person and a LID person for one account, plus an owner-flagged person."""
    monkeypatch.setenv("YEOMAN_HOME", str(tmp_path))
    db = tmp_path / "knowledge.db"
    authority = FakeSourceAuthority()
    service = _open(db, authority)
    people = {}
    for name, identifier in (("phone", _phone()), ("lid", _lid())):
        observation = _observation(f"seed-{name}", identifier, verified=False)
        authority.issue_observation(observation)
        people[name] = service.resolve_observation(observation).person_id
    owner_obs = _observation(
        "seed-owner",
        Identifier("whatsapp", "lid", "100000000000009@lid", "default"),
        verified=False,
    )
    authority.issue_observation(owner_obs)
    people["owner"] = service.resolve_observation(owner_obs).person_id
    service.close()
    with _sql(db) as connection:
        connection.execute("UPDATE contacts SET is_owner = 1 WHERE id = ?", (people["owner"],))
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"owners": {"whatsapp": [OWNER_PHONE]}}))
    return db, policy, people


def _invoke(*args: str):
    return CliRunner().invoke(knowledge_app, list(args))


def _merge(db: Path, policy: Path, target: str, source: str, *extra: str):
    return _invoke(
        "person-merge", "--db", str(db), "--policy", str(policy),
        "--target", target, "--source", source, *extra,
    )


def _resolve_pair(db: Path) -> str:
    """How a bridge-proven phone+LID observation of this account resolves now."""
    authority = FakeSourceAuthority()
    service = _open(db, authority)
    try:
        observation = _observation("probe", _phone(), _lid(), verified=True)
        authority.issue_observation(observation)
        result = service.resolve_observation(observation)
        return f"{result.status}:{result.person_id or ''}"
    finally:
        service.close()


def _redirect_count(db: Path) -> int:
    with _sql(db) as connection:
        return connection.execute(
            "SELECT COUNT(*) FROM knowledge_identity_redirects WHERE active = 1"
        ).fetchone()[0]


def test_the_split_account_is_a_conflict_before_any_merge(store) -> None:
    db, _policy, _people = store
    assert _resolve_pair(db).startswith("conflict:")


def test_a_dry_run_reports_the_plan_and_writes_nothing(store) -> None:
    db, policy, people = store
    before = db.read_bytes()

    result = _merge(db, policy, people["phone"], people["lid"])

    assert result.exit_code == 0, result.output
    assert "dry run" in result.output
    assert "would merge" in result.output
    assert db.read_bytes() == before
    assert _redirect_count(db) == 0


def test_apply_redirects_the_source_so_the_account_resolves_to_one_person(store) -> None:
    db, policy, people = store

    result = _merge(db, policy, people["phone"], people["lid"], "--apply")

    assert result.exit_code == 0, result.output
    assert "merged" in result.output and "operation" in result.output
    assert _redirect_count(db) == 1
    assert _resolve_pair(db) == f"resolved:{people['phone']}"


def _set_owner(db: Path, person_id: str) -> None:
    with _sql(db) as connection:
        connection.execute("UPDATE contacts SET is_owner = 1 WHERE id = ?", (person_id,))


def test_an_owner_flagged_source_is_refused_when_the_target_is_not(store) -> None:
    """The flag never moves with a merge, so the canonical person would lose it."""
    db, policy, people = store
    before = db.read_bytes()

    result = _merge(db, policy, people["phone"], people["owner"], "--apply")

    assert result.exit_code != 0
    assert "owner" in result.output
    assert db.read_bytes() == before


def test_a_person_can_be_merged_into_an_owner_flagged_target(store) -> None:
    db, policy, people = store

    result = _merge(db, policy, people["owner"], people["lid"], "--apply")

    assert result.exit_code == 0, result.output
    assert _redirect_count(db) == 1


def test_two_owner_flagged_people_can_be_merged(store) -> None:
    db, policy, people = store
    _set_owner(db, people["phone"])

    result = _merge(db, policy, people["phone"], people["owner"], "--apply")

    assert result.exit_code == 0, result.output
    assert _redirect_count(db) == 1


def _person_row(db: Path, person_id: str) -> sqlite3.Row:
    with _sql(db) as connection:
        return connection.execute("SELECT * FROM contacts WHERE id = ?", (person_id,)).fetchone()


def _alias_rows(db: Path, person_id: str) -> list[sqlite3.Row]:
    with _sql(db) as connection:
        return connection.execute(
            "SELECT * FROM contact_aliases WHERE contact_id = ? ORDER BY id", (person_id,)
        ).fetchall()


def _add_alias(db: Path, person_id: str, alias: str, source: str = "push_name") -> None:
    with _sql(db) as connection:
        connection.execute(
            "INSERT INTO contact_aliases (contact_id, alias, source, first_seen, last_seen)"
            " VALUES (?, ?, ?, '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')",
            (person_id, alias, source),
        )


def _name(db: Path, policy: Path, person: str, name: str, *extra: str):
    return _invoke("person-name", "--db", str(db), "--policy", str(policy),
                   "--person", person, "--name", name, *extra)


def _retire(db: Path, policy: Path, person: str, alias: str, *extra: str):
    return _invoke("person-alias-retire", "--db", str(db), "--policy", str(policy),
                   "--person", person, "--alias", alias, *extra)


def test_person_name_is_a_dry_run_until_applied(store) -> None:
    db, policy, people = store
    before = db.read_bytes()

    dry = _name(db, policy, people["phone"], "Synthetic Name")
    assert dry.exit_code == 0, dry.output
    assert "dry run" in dry.output
    assert db.read_bytes() == before

    applied = _name(db, policy, people["phone"], "Synthetic Name", "--apply")
    assert applied.exit_code == 0, applied.output
    row = _person_row(db, people["phone"])
    assert row["preferred_name"] == "Synthetic Name"
    assert row["preferred_name_source"] == "owner_confirmed"


def test_person_name_refuses_an_unknown_person(store) -> None:
    db, policy, _people = store

    assert _name(db, policy, "no-such-person", "Synthetic Name", "--apply").exit_code != 0


def test_alias_retire_retracts_the_mapping_only_when_applied(store) -> None:
    db, policy, people = store
    _add_alias(db, people["phone"], "Wrong Name")
    _add_alias(db, people["phone"], "Kept Name")
    before = db.read_bytes()

    dry = _retire(db, policy, people["phone"], "Wrong Name")
    assert dry.exit_code == 0, dry.output
    assert "dry run" in dry.output
    assert db.read_bytes() == before

    applied = _retire(db, policy, people["phone"], "Wrong Name", "--apply")
    assert applied.exit_code == 0, applied.output
    by_alias = {row["alias"]: row for row in _alias_rows(db, people["phone"])}
    assert by_alias["Wrong Name"]["status"] == "retired"
    assert by_alias["Wrong Name"]["mapping_retracted"] == 1
    assert by_alias["Kept Name"]["status"] != "retired"


def test_alias_retire_refuses_an_alias_the_person_does_not_have(store) -> None:
    db, policy, people = store

    result = _retire(db, policy, people["phone"], "Never Seen", "--apply")

    assert result.exit_code != 0


def test_unknown_or_identical_people_are_refused(store) -> None:
    db, policy, people = store

    assert _merge(db, policy, people["phone"], people["phone"], "--apply").exit_code != 0
    assert _merge(db, policy, people["phone"], "no-such-person", "--apply").exit_code != 0
    assert _redirect_count(db) == 0


def test_undo_is_a_dry_run_until_applied_and_restores_the_split(store) -> None:
    db, policy, people = store
    merged = _merge(db, policy, people["phone"], people["lid"], "--apply")
    operation = merged.output.split("operation", 1)[1].split()[0].strip(":()")

    dry = _invoke("person-merge-undo", "--db", str(db), "--policy", str(policy),
                  "--operation", operation)
    assert dry.exit_code == 0, dry.output
    assert "dry run" in dry.output
    assert _redirect_count(db) == 1

    undone = _invoke("person-merge-undo", "--db", str(db), "--policy", str(policy),
                     "--operation", operation, "--apply")
    assert undone.exit_code == 0, undone.output
    assert _redirect_count(db) == 0
    assert _resolve_pair(db).startswith("conflict:")


def test_output_never_prints_identifier_values(store) -> None:
    db, policy, people = store

    result = _merge(db, policy, people["phone"], people["lid"], "--apply")

    for secret in ("491520000001", "100000000000001", OWNER_PHONE):
        assert secret not in result.output


def _retire_operation(output: str) -> str:
    return output.split("operation", 1)[1].split()[0].strip(":()")


def _restore(db: Path, policy: Path, operation: str, *extra: str):
    return _invoke("person-alias-restore", "--db", str(db), "--policy", str(policy),
                   "--operation", operation, *extra)


def _set_alias_state(db: Path, person_id: str, alias: str, **fields: object) -> None:
    columns = ", ".join(f"{name} = ?" for name in fields)
    with _sql(db) as connection:
        connection.execute(
            f"UPDATE contact_aliases SET {columns} WHERE contact_id = ? AND alias = ?",
            (*fields.values(), person_id, alias),
        )


def test_alias_retire_prints_an_operation_that_restores_the_exact_previous_state(
    store,
) -> None:
    db, policy, people = store
    _add_alias(db, people["phone"], "Confirmed Name")
    _set_alias_state(
        db, people["phone"], "Confirmed Name", status="confirmed", address_allowed=1
    )
    retired = _retire(db, policy, people["phone"], "Confirmed Name", "--apply")
    assert retired.exit_code == 0, retired.output
    operation = _retire_operation(retired.output)
    before = db.read_bytes()

    dry = _restore(db, policy, operation)
    assert dry.exit_code == 0, dry.output
    assert "dry run" in dry.output
    assert db.read_bytes() == before

    restored = _restore(db, policy, operation, "--apply")
    assert restored.exit_code == 0, restored.output
    row = {r["alias"]: r for r in _alias_rows(db, people["phone"])}["Confirmed Name"]
    assert row["status"] == "confirmed"
    assert row["address_allowed"] == 1
    assert row["mapping_retracted"] == 0
    assert row["valid_until_ms"] is None


def test_a_retirement_without_recorded_state_restores_to_observed_without_addressing(
    store,
) -> None:
    db, policy, people = store
    _add_alias(db, people["phone"], "Legacy Name")
    retired = _retire(db, policy, people["phone"], "Legacy Name", "--apply")
    operation = _retire_operation(retired.output)
    with _sql(db) as connection:  # the shape of an older retirement record
        connection.execute(
            "UPDATE knowledge_identity_ops SET payload_json = json_remove(payload_json,"
            " '$.previous') WHERE operation_id = ?",
            (operation,),
        )

    restored = _restore(db, policy, operation, "--apply")

    assert restored.exit_code == 0, restored.output
    row = {r["alias"]: r for r in _alias_rows(db, people["phone"])}["Legacy Name"]
    assert row["status"] == "observed"
    assert row["address_allowed"] == 0
    assert row["mapping_retracted"] == 0


def test_a_restore_is_applied_at_most_once(store) -> None:
    db, policy, people = store
    _add_alias(db, people["phone"], "Once Name")
    operation = _retire_operation(
        _retire(db, policy, people["phone"], "Once Name", "--apply").output
    )
    assert _restore(db, policy, operation, "--apply").exit_code == 0

    assert _restore(db, policy, operation, "--apply").exit_code != 0


def test_restore_refuses_an_operation_that_is_not_an_alias_retirement(store) -> None:
    db, policy, people = store
    merged = _merge(db, policy, people["phone"], people["lid"], "--apply")
    operation = merged.output.split("operation", 1)[1].split()[0].strip(":()")

    result = _restore(db, policy, operation, "--apply")

    assert result.exit_code != 0
    assert _redirect_count(db) == 1


SECOND_PHONE = "491520000002@s.whatsapp.net"


def _move(db: Path, policy: Path, kind: str, value: str, to: str, *extra: str):
    return _invoke("person-binding-move", "--db", str(db), "--policy", str(policy),
                   "--kind", kind, "--value", value, "--to", to,
                   "--evidence", "owner-confirmed-test", *extra)


def _bind_new(db: Path, policy: Path, kind: str, value: str, to: str, *extra: str):
    return _invoke("person-binding-add", "--db", str(db), "--policy", str(policy),
                   "--kind", kind, "--value", value, "--to", to,
                   "--evidence", "owner-confirmed-test", *extra)


def _bindings(db: Path, value: str) -> list[sqlite3.Row]:
    with _sql(db) as connection:
        return connection.execute(
            "SELECT * FROM knowledge_identifier_bindings WHERE value = ? ORDER BY created_ms",
            (value,),
        ).fetchall()


def _legacy_owner(db: Path, value: str) -> str | None:
    with _sql(db) as connection:
        row = connection.execute(
            "SELECT contact_id FROM contact_identifiers WHERE identifier = ?", (value,)
        ).fetchone()
    return None if row is None else str(row[0])


def test_a_wrong_binding_is_moved_as_a_correction_only_when_applied(store) -> None:
    db, policy, people = store
    before = db.read_bytes()

    dry = _move(db, policy, "lid", LID, people["phone"])
    assert dry.exit_code == 0, dry.output
    assert "dry run" in dry.output
    assert db.read_bytes() == before

    moved = _move(db, policy, "lid", LID, people["phone"], "--apply")
    assert moved.exit_code == 0, moved.output
    rows = _bindings(db, LID)
    active = [row for row in rows if row["status"] == "active"]
    assert [row["person_id"] for row in active] == [people["phone"]]
    assert any(row["status"] == "ended" and row["person_id"] == people["lid"] for row in rows)
    # The legacy projection that the owner marking reads follows the correction.
    assert _legacy_owner(db, LID) == people["phone"]
    assert _resolve_pair(db) == f"resolved:{people['phone']}"


def test_moving_a_binding_refuses_an_unbound_or_already_moved_identifier(store) -> None:
    db, policy, people = store

    assert _move(db, policy, "phone_jid", SECOND_PHONE, people["phone"], "--apply").exit_code != 0
    assert _move(db, policy, "phone_jid", PHONE_JID, people["phone"], "--apply").exit_code != 0


def test_an_owner_asserted_binding_attaches_an_unseen_number(store) -> None:
    db, policy, people = store
    before = db.read_bytes()

    dry = _bind_new(db, policy, "phone_jid", SECOND_PHONE, people["phone"])
    assert dry.exit_code == 0, dry.output
    assert db.read_bytes() == before

    added = _bind_new(db, policy, "phone_jid", SECOND_PHONE, people["phone"], "--apply")
    assert added.exit_code == 0, added.output
    rows = _bindings(db, SECOND_PHONE)
    assert [(row["person_id"], row["status"]) for row in rows] == [(people["phone"], "active")]
    assert rows[0]["mapping_verified"] == 0


def test_adding_a_binding_never_takes_an_identifier_from_another_person(store) -> None:
    db, policy, people = store

    result = _bind_new(db, policy, "lid", LID, people["phone"], "--apply")

    assert result.exit_code != 0
    assert [row["person_id"] for row in _bindings(db, LID)] == [people["lid"]]

