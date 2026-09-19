"""P1.1: the public boundary owns one storage and one transaction.

These tests use the real public API against a temporary database.  They prove that
identity and statement writes share a transaction, that a failing commit leaves no
partial row behind, and that the public facade does not hand out a second writer.
"""

from __future__ import annotations

import threading

import pytest
from yeoman_gateway.knowledge.authority import FakePolicyAuthority, FakeSourceAuthority
from yeoman_gateway.knowledge.models import (
    Identifier,
    KnowledgeError,
    TrustedAdminContext,
    TrustedIdentityObservation,
)


def test_identity_and_alias_write_roll_back_together(knowledge_harness):
    h = knowledge_harness
    person = h.person("Tom")
    before = h.snapshot_counts()
    before_revision = h.identity_revision()
    h.fail_next_commit()
    with pytest.raises(KnowledgeError) as excinfo:
        with h.service._store.transaction():  # noqa: SLF001 - fault injection harness
            h.service._store.execute(  # noqa: SLF001
                "UPDATE contacts SET display_name = ? WHERE id = ?", ("Rollback", person)
            )
            h.service._store.execute(  # noqa: SLF001
                "INSERT INTO contact_aliases (contact_id, alias, source, first_seen, last_seen)"
                " VALUES (?, ?, 'observed', 'now', 'now')",
                (person, "Rollback"),
            )
            h.service._store.set_meta("identity_revision", "9999")  # noqa: SLF001
    assert excinfo.value.code == "storage_unavailable"
    assert h.snapshot_counts() == before
    assert h.service.display_name(person) == "Tom"
    assert "Rollback" not in h.service.alias_names(person)
    assert h.identity_revision() == before_revision


def test_nested_operation_joins_the_outer_transaction(knowledge_harness):
    """An inner knowledge operation must not commit the outer unit of work."""
    import sqlite3

    h = knowledge_harness
    person = h.person("Tom")
    db = h.service.db_path

    def outside_preferred_name() -> str | None:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT preferred_name FROM contacts WHERE id = ?", (person,)
            ).fetchone()
            return None if row is None else row[0]
        finally:
            conn.close()

    assert outside_preferred_name() is None
    with h.service._store.transaction():  # noqa: SLF001
        h.service._store.execute(  # noqa: SLF001
            "UPDATE contacts SET display_name = 'inner' WHERE id = ?", (person,)
        )
        with h.service._store.transaction():  # noqa: SLF001
            h.service._store.execute(  # noqa: SLF001
                "UPDATE contacts SET preferred_name = 'inner-too' WHERE id = ?", (person,)
            )
        # Still inside the outer transaction: another connection must not see it.
        assert outside_preferred_name() is None
    assert outside_preferred_name() == "inner-too"
    assert h.service.display_name(person) == "inner-too"


def test_identifier_racing_creates_at_most_one_person(knowledge_harness):
    h = knowledge_harness
    value = "49188888888@s.whatsapp.net"
    results: list[str | None] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            resolved = h.observe("whatsapp", "phone_jid", value)
            results.append(resolved.person_id)
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors, errors
    assert len(set(results)) == 1
    assert results[0] is not None
    assert h.snapshot_counts()["contacts"] == 1
    assert h.snapshot_counts()["contact_identifiers"] == 1


def test_second_service_instance_on_one_file_sees_the_same_identity(tmp_path):
    """Two processes may read one file; there is exactly one logical writer path."""
    from yeoman_gateway.knowledge.api import open_knowledge_store

    db = tmp_path / "shared.db"
    authority = FakeSourceAuthority()
    policy = FakePolicyAuthority(admins={"owner:p"}, capture_actors={"owner:p"})
    first = open_knowledge_store(
        db, workspace_id="ws", source_authority=authority, policy_authority=policy
    )
    second = open_knowledge_store(
        db, workspace_id="ws", source_authority=authority, policy_authority=policy
    )
    try:
        observation = TrustedIdentityObservation(
            identifiers=(Identifier("whatsapp", "phone_jid", "49177777777@s.whatsapp.net"),),
            evidence_ref="obs-x",
        )
        authority.issue_observation(observation)
        created = first.resolve_person(observation)
        assert created.person_id
        again = second.resolve_person(observation)
        assert again.person_id == created.person_id
        assert again.reason == "existing_binding"
    finally:
        first.close()
        second.close()


def test_close_is_idempotent_and_stops_writes(knowledge_harness):
    h = knowledge_harness
    h.service.close()
    h.service.close()
    with pytest.raises(KnowledgeError) as excinfo:
        with h.service._store.transaction():  # noqa: SLF001
            pass
    assert excinfo.value.code == "storage_unavailable"


def test_admin_context_requires_owner_authority(knowledge_harness):
    h = knowledge_harness
    person = h.person("Tom")
    context = TrustedAdminContext(
        actor_principal="whatsapp:4910000000001",
        policy_revision=1,
        authorization_ref="ref",
        owner=False,
    )
    with pytest.raises(KnowledgeError) as excinfo:
        h.service.set_preferred_name(person, "Dimi", context=context)
    assert excinfo.value.code == "unauthorized"


def test_public_boundary_exposes_no_store_handle():
    """The facade must not offer a generic store getter to consumers."""
    from yeoman_gateway.knowledge.api import KnowledgeService

    public = {
        name
        for name in dir(KnowledgeService)
        if not name.startswith("_") and callable(getattr(KnowledgeService, name, None))
    }
    assert "execute_sql" not in public
    assert "store" not in public
    assert "connection" not in public
    assert "known_jids" not in public
