"""Owner CLI for id-based person merges and their undo.

The chat merge tool finds people by name, which is ambiguous exactly for duplicates that
share a name.  The CLI takes exact person ids, is a dry run unless ``--apply`` is given,
and never touches an owner-flagged person: owner records are repaired separately.  All
identifiers are synthetic.
"""

from __future__ import annotations

import json
import sqlite3
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
    with sqlite3.connect(db) as connection:
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
    with sqlite3.connect(db) as connection:
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


@pytest.mark.parametrize("role", ["target", "source"])
def test_an_owner_flagged_person_is_refused(store, role: str) -> None:
    db, policy, people = store
    target, source = (
        (people["owner"], people["lid"]) if role == "target" else (people["phone"], people["owner"])
    )
    before = db.read_bytes()

    result = _merge(db, policy, target, source, "--apply")

    assert result.exit_code != 0
    assert "owner" in result.output
    assert db.read_bytes() == before


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
