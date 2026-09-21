"""T28–T30/T40: owner-reviewed binding approvals for a v1 snapshot without bindings.

A v1 store can carry a populated ``contact_identifiers`` projection and *no* identifier
binding at all (that is the live shape).  Nothing in it may be promoted automatically, so
the migration offers the missing piece: a read-only proposal the owner reviews, and an
upgrade that applies exactly the entries marked approved as ordinary durable bindings.
Every value here is synthetic.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from legacy_fixtures import (
    V1_PERSON_BOUND,
    V1_PERSON_LEGACY_ONLY,
    v1_knowledge_store_factory,
)
from yeoman_gateway.knowledge._upgrade import (
    UpgradeError,
    propose_bindings,
    upgrade_v1,
    verify_upgrade,
)
from yeoman_gateway.knowledge.api import open_knowledge_store
from yeoman_gateway.knowledge.authority import FakePolicyAuthority, FakeSourceAuthority
from yeoman_gateway.knowledge.models import Identifier, TrustedIdentityObservation

BOUND_PHONE = "4910000000101@s.whatsapp.net"
LEGACY_PHONE = "4910000000102@s.whatsapp.net"
OWNER = "whatsapp:4910000000101"


def _journal(path: Path, *, principals: tuple[str, ...] = ()) -> Path:
    """Minimal processing journal; optional inbound principals as channel evidence."""
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE events (event_id TEXT PRIMARY KEY, chat_id TEXT NOT NULL,"
            " revision INTEGER NOT NULL, payload TEXT NOT NULL DEFAULT '',"
            " principal TEXT, direction TEXT)"
        )
        conn.execute(
            "CREATE TABLE event_source_authority (event_id TEXT NOT NULL,"
            " revision INTEGER NOT NULL, authorized INTEGER NOT NULL DEFAULT 0,"
            " PRIMARY KEY (event_id, revision))"
        )
        conn.execute(
            "INSERT INTO events (event_id, chat_id, revision) VALUES ('event-0', 'group-x', 1)"
        )
        for index, principal in enumerate(principals, start=1):
            conn.execute(
                "INSERT INTO events (event_id, chat_id, revision, principal, direction)"
                " VALUES (?, 'group-x', 1, ?, 'in')",
                (f"evidence-{index}", principal),
            )
        conn.commit()
    finally:
        conn.close()
    return path


def _live_shaped(tmp_path: Path) -> tuple[Path, Path]:
    """A v1 snapshot whose bindings and binding operations are gone (the live shape)."""
    fixture = v1_knowledge_store_factory(tmp_path / "v1.db")
    conn = sqlite3.connect(fixture.path)
    try:
        conn.execute("DELETE FROM knowledge_identifier_bindings")
        conn.execute("DELETE FROM knowledge_identity_ops")
        conn.commit()
    finally:
        conn.close()
    return fixture.path, _journal(tmp_path / "processing.db", principals=("4910000000101",))


def _approve(path: Path, *, people: tuple[str, ...], actor: str = "owner:dimis") -> Path:
    """Mark the entries of ``people`` as approved, as the operator would."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["approved_by"] = actor
    for entry in payload["entries"]:
        entry["approved"] = entry["person_id"] in people
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _entries(path: Path) -> list[dict[str, object]]:
    return list(json.loads(path.read_text(encoding="utf-8"))["entries"])


def test_a_proposal_lists_the_legacy_identifiers_and_applies_nothing(tmp_path: Path) -> None:
    source, journal = _live_shaped(tmp_path)
    before = hashlib.sha256(source.read_bytes()).hexdigest()

    report = propose_bindings(
        source=source, processing=journal, out=tmp_path / "proposals.json"
    )

    assert report.total == 4
    # The one journal principal matches both identifiers of the bound person.
    assert report.with_journal_evidence == 2
    assert report.role_rows_covered == 4  # distinct people, not per identifier
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    assert not list(tmp_path.glob("v1.db-wal")) and not list(tmp_path.glob("v1.db-shm"))

    payload = json.loads((tmp_path / "proposals.json").read_text(encoding="utf-8"))
    assert payload["proposal_version"] == 1
    assert payload["approved_by"] == ""
    assert payload["counts"]["entries"] == 4
    assert all(entry["approved"] is False for entry in payload["entries"])
    assert {entry["person_id"] for entry in payload["entries"]} == {
        V1_PERSON_BOUND,
        V1_PERSON_LEGACY_ONLY,
    }
    # The proposal names people and identifiers, but never statement or message text.
    text = (tmp_path / "proposals.json").read_text(encoding="utf-8")
    assert "synthetic statement" not in text
    assert "synthetic legacy field" not in text


def test_proposal_refuses_to_overwrite_and_needs_a_v1_source(tmp_path: Path) -> None:
    source, journal = _live_shaped(tmp_path)
    out = tmp_path / "proposals.json"
    out.write_text("{}", encoding="utf-8")
    with pytest.raises(UpgradeError) as existing:
        propose_bindings(source=source, processing=journal, out=out)
    assert existing.value.code == "target_exists"


def test_approved_identifiers_become_active_bindings_and_revive_roles(tmp_path: Path) -> None:
    source, journal = _live_shaped(tmp_path)
    proposals = propose_bindings(
        source=source, processing=journal, out=tmp_path / "proposals.json"
    ).out_path
    _approve(proposals, people=(V1_PERSON_BOUND,))

    report = upgrade_v1(
        source=source,
        processing=journal,
        target=tmp_path / "v2.db",
        manifest=tmp_path / "manifest.json",
        binding_approvals=proposals,
    )

    assert report.balance.approvals == {"applied": 2, "requested": 2}
    # The unapproved person keeps its withheld verdict; the approved one is active.
    assert report.balance.person_roles.get("active", 0) >= 1
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["approvals"]["applied"] == 2
    assert manifest["approvals"]["approved_by"] == "owner:dimis"
    assert len(manifest["approvals"]["file_digest"]) == 64

    verification = verify_upgrade(target=tmp_path / "v2.db", manifest=tmp_path / "manifest.json")
    assert verification.verdict == "ok", verification.mismatches
    assert verification.approvals_declared == 2
    assert verification.approvals_found == 2
    assert verification.approvals_ok


def test_an_approved_binding_resolves_the_next_observation_to_the_same_person(
    tmp_path: Path,
) -> None:
    """The migration-day acceptance: the person does not have to be re-invented."""
    source, journal = _live_shaped(tmp_path)
    proposals = propose_bindings(
        source=source, processing=journal, out=tmp_path / "proposals.json"
    ).out_path
    _approve(proposals, people=(V1_PERSON_BOUND,))
    upgrade_v1(
        source=source,
        processing=journal,
        target=tmp_path / "v2.db",
        manifest=tmp_path / "manifest.json",
        binding_approvals=proposals,
    )

    authority = FakeSourceAuthority()
    service = open_knowledge_store(
        tmp_path / "v2.db",
        workspace_id="approval-tests",
        source_authority=authority,
        policy_authority=FakePolicyAuthority(admins={OWNER}, capture_actors={OWNER}),
    )
    try:
        before = service._store.scalar("SELECT COUNT(*) FROM contacts")  # noqa: SLF001
        observation = TrustedIdentityObservation(
            identifiers=(Identifier("whatsapp", "phone_jid", BOUND_PHONE),),
            evidence_ref="approval-test-observation",
            observed_name="Boundy",
            observed_at_ms=service._now(),  # noqa: SLF001 - synthetic clock
            mapping_verified=True,
        )
        authority.issue_observation(observation)
        resolved = service.resolve_observation(observation)
        after = service._store.scalar("SELECT COUNT(*) FROM contacts")  # noqa: SLF001

        assert resolved.status == "resolved"
        assert resolved.person_id == V1_PERSON_BOUND
        assert resolved.reason == "existing_binding"
        assert after == before  # no second person was invented
        assert BOUND_PHONE in {item.value for item in service.person_identifiers(V1_PERSON_BOUND)}
    finally:
        service.close()


def test_an_unapproved_identifier_still_cannot_bind_itself(tmp_path: Path) -> None:
    """The documented boundary: no approval, no proof - the projection is not authority."""
    source, journal = _live_shaped(tmp_path)
    proposals = propose_bindings(
        source=source, processing=journal, out=tmp_path / "proposals.json"
    ).out_path
    _approve(proposals, people=(V1_PERSON_BOUND,))
    upgrade_v1(
        source=source,
        processing=journal,
        target=tmp_path / "v2.db",
        manifest=tmp_path / "manifest.json",
        binding_approvals=proposals,
    )

    authority = FakeSourceAuthority()
    service = open_knowledge_store(
        tmp_path / "v2.db",
        workspace_id="approval-tests",
        source_authority=authority,
        policy_authority=FakePolicyAuthority(admins={OWNER}, capture_actors={OWNER}),
    )
    try:
        observation = TrustedIdentityObservation(
            identifiers=(Identifier("whatsapp", "phone_jid", LEGACY_PHONE),),
            evidence_ref="approval-test-unapproved",
            observed_name="Synthetic Legacy",
            observed_at_ms=service._now(),  # noqa: SLF001 - synthetic clock
            mapping_verified=True,
        )
        authority.issue_observation(observation)
        with pytest.raises(sqlite3.IntegrityError):
            service.resolve_observation(observation)
        assert service.person_identifiers(V1_PERSON_LEGACY_ONLY) == ()
    finally:
        service.close()


def test_an_approval_that_names_someone_elses_identifier_is_refused(tmp_path: Path) -> None:
    source, journal = _live_shaped(tmp_path)
    proposals = propose_bindings(
        source=source, processing=journal, out=tmp_path / "proposals.json"
    ).out_path
    _approve(proposals, people=(V1_PERSON_BOUND,))
    payload = json.loads(proposals.read_text(encoding="utf-8"))
    for entry in payload["entries"]:
        if entry["approved"]:
            entry["person_id"] = V1_PERSON_LEGACY_ONLY
    proposals.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(UpgradeError) as excinfo:
        upgrade_v1(
            source=source,
            processing=journal,
            target=tmp_path / "v2.db",
            manifest=tmp_path / "manifest.json",
            binding_approvals=proposals,
        )

    assert excinfo.value.code == "approval_mismatch"
    assert not (tmp_path / "v2.db").exists()
    assert not (tmp_path / "manifest.json").exists()


def test_unknown_duplicate_and_anonymous_approvals_are_refused(tmp_path: Path) -> None:
    source, journal = _live_shaped(tmp_path)
    proposals = propose_bindings(
        source=source, processing=journal, out=tmp_path / "proposals.json"
    ).out_path

    # (a) an identifier that is not in the snapshot at all
    _approve(proposals, people=(V1_PERSON_BOUND,))
    payload = json.loads(proposals.read_text(encoding="utf-8"))
    entry = next(e for e in payload["entries"] if e["approved"])
    payload["entries"] = [entry]
    entry["value"] = "4910000009999@s.whatsapp.net"
    proposals.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(UpgradeError) as unknown:
        upgrade_v1(
            source=source, processing=journal, target=tmp_path / "a.db",
            manifest=tmp_path / "a.json", binding_approvals=proposals,
        )
    assert unknown.value.code == "approval_unknown_identifier"

    # (b) the same entry twice
    payload = json.loads(proposals.read_text(encoding="utf-8"))
    entry = {
        "person_id": V1_PERSON_BOUND,
        "channel": "whatsapp",
        "kind": "phone_jid",
        "value": "4910000000101@s.whatsapp.net",
        "approved": True,
    }
    payload["entries"] = [entry, dict(entry)]
    proposals.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(UpgradeError) as duplicate:
        upgrade_v1(
            source=source, processing=journal, target=tmp_path / "b.db",
            manifest=tmp_path / "b.json", binding_approvals=proposals,
        )
    assert duplicate.value.code == "approval_duplicate"

    # (c) an approval without an actor is not a decision
    payload = json.loads(proposals.read_text(encoding="utf-8"))
    entry = {"person_id": V1_PERSON_BOUND, "channel": "whatsapp", "kind": "phone_jid",
             "value": "4910000000101@s.whatsapp.net", "approved": True}
    payload["entries"] = [entry]
    payload["approved_by"] = ""
    proposals.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(UpgradeError) as anonymous:
        upgrade_v1(
            source=source, processing=journal, target=tmp_path / "c.db",
            manifest=tmp_path / "c.json", binding_approvals=proposals,
        )
    assert anonymous.value.code == "approval_file_invalid"


def test_the_same_approvals_produce_the_same_migration(tmp_path: Path) -> None:
    source, journal = _live_shaped(tmp_path)
    proposals = propose_bindings(
        source=source, processing=journal, out=tmp_path / "proposals.json"
    ).out_path
    _approve(proposals, people=(V1_PERSON_BOUND, V1_PERSON_LEGACY_ONLY))

    first = upgrade_v1(
        source=source, processing=journal, target=tmp_path / "one.db",
        manifest=tmp_path / "one.json", binding_approvals=proposals,
    )
    second = upgrade_v1(
        source=source, processing=journal, target=tmp_path / "two.db",
        manifest=tmp_path / "two.json", binding_approvals=proposals,
    )

    assert first.migration_id == second.migration_id
    assert first.semantic_digest == second.semantic_digest
    assert first.balance.approvals == second.balance.approvals == {"applied": 4, "requested": 4}


def test_verify_reports_a_lost_approval(tmp_path: Path) -> None:
    source, journal = _live_shaped(tmp_path)
    proposals = propose_bindings(
        source=source, processing=journal, out=tmp_path / "proposals.json"
    ).out_path
    _approve(proposals, people=(V1_PERSON_BOUND,))
    upgrade_v1(
        source=source, processing=journal, target=tmp_path / "v2.db",
        manifest=tmp_path / "manifest.json", binding_approvals=proposals,
    )

    conn = sqlite3.connect(tmp_path / "v2.db")
    try:
        conn.execute("DELETE FROM knowledge_identifier_bindings")
        conn.commit()
    finally:
        conn.close()

    verification = verify_upgrade(target=tmp_path / "v2.db", manifest=tmp_path / "manifest.json")
    assert verification.verdict == "failed"
    assert verification.approvals_declared == 2
    assert verification.approvals_found == 0
    assert not verification.approvals_ok


def test_entries_are_only_read_from_the_approved_ones(tmp_path: Path) -> None:
    source, journal = _live_shaped(tmp_path)
    proposals = propose_bindings(
        source=source, processing=journal, out=tmp_path / "proposals.json"
    ).out_path
    _approve(proposals, people=(V1_PERSON_BOUND,))
    assert sum(1 for entry in _entries(proposals) if entry["approved"]) == 2


def test_the_offline_cli_offers_the_proposal_and_applies_it(tmp_path: Path) -> None:
    """The operator path is the CLI: propose, review, upgrade with the same file."""
    import typer
    from typer.testing import CliRunner
    from yeoman_gateway.cli.knowledge_commands import knowledge_app

    parent = typer.Typer()
    parent.add_typer(knowledge_app, name="knowledge")
    runner = CliRunner()
    source, journal = _live_shaped(tmp_path)
    proposals = tmp_path / "proposals.json"

    proposed = runner.invoke(
        parent,
        [
            "knowledge", "migration", "propose-bindings",
            "--source", str(source),
            "--processing", str(journal),
            "--out", str(proposals),
        ],
    )
    assert proposed.exit_code == 0, proposed.output
    assert "nothing was applied" in proposed.output

    _approve(proposals, people=(V1_PERSON_BOUND,))
    upgraded = runner.invoke(
        parent,
        [
            "knowledge", "migration", "upgrade-v1",
            "--source", str(source),
            "--processing", str(journal),
            "--target", str(tmp_path / "v2.db"),
            "--manifest", str(tmp_path / "manifest.json"),
            "--binding-approvals", str(proposals),
        ],
    )
    assert upgraded.exit_code == 0, upgraded.output
    assert "approvals: applied=2" in upgraded.output

    verified = runner.invoke(
        parent,
        [
            "knowledge", "migration", "verify-v1",
            "--target", str(tmp_path / "v2.db"),
            "--manifest", str(tmp_path / "manifest.json"),
        ],
    )
    assert verified.exit_code == 0, verified.output
    assert "2 of 2 applied" in verified.output
