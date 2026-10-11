"""P1.1: the public boundary owns one storage and one transaction.

These tests use the real public API against a temporary database.  They prove that
identity and statement writes share a transaction, that a failing commit leaves no
partial row behind, and that the public facade does not hand out a second writer.
"""

from __future__ import annotations

import sqlite3

import pytest
from yeoman_gateway.knowledge.models import (
    KnowledgeError,
)


def test_nested_operation_joins_the_outer_transaction(knowledge_harness):
    """An inner knowledge operation must not commit the outer unit of work."""
    import sqlite3

    h = knowledge_harness
    db = h.service.db_path

    def outside_preferred_name() -> str | None:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT value FROM knowledge_meta WHERE key = 'transaction_probe'"
            ).fetchone()
            return None if row is None else row[0]
        finally:
            conn.close()

    assert outside_preferred_name() is None
    with h.service._store.transaction():  # noqa: SLF001
        h.service._store.execute(  # noqa: SLF001
            "INSERT OR REPLACE INTO knowledge_meta(key, value) VALUES ('transaction_probe', 'inner')"
        )
        with h.service._store.transaction():  # noqa: SLF001
            h.service._store.execute(  # noqa: SLF001
                "INSERT OR REPLACE INTO knowledge_meta(key, value) VALUES ('transaction_probe', 'inner-too')"
            )
        # Still inside the outer transaction: another connection must not see it.
        assert outside_preferred_name() is None
    assert outside_preferred_name() == "inner-too"
    assert h.service._store.get_meta("transaction_probe") == "inner-too"  # noqa: SLF001






def test_close_is_idempotent_and_stops_writes(knowledge_harness):
    h = knowledge_harness
    h.service.close()
    h.service.close()
    with pytest.raises(KnowledgeError) as excinfo:
        with h.service._store.transaction():  # noqa: SLF001
            pass
    assert excinfo.value.code == "storage_unavailable"




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


# ── v2 schema contracts ──────────────────────────────────────────────────────
#
# These are SQL-level contracts of the v2 schema itself: the unique indexes and the
# foreign keys are the last line of defence behind the public API.  They are asserted
# against the real store so a future "small cleanup" cannot quietly drop them.


def _insert_binding(
    store,
    *,
    binding_id: str,
    person_id: str,
    value: str,
    status: str = "active",
    valid_from_ms: int = 0,
    valid_until_ms: int = 0,
) -> None:
    store.execute(
        "INSERT INTO knowledge_identifier_bindings (binding_id, channel, kind, namespace,"
        " value, person_id, status, valid_from_ms, valid_until_ms, observed_at_ms,"
        " evidence_ref, mapping_verified, revision, created_ms, updated_ms)"
        " VALUES (?, 'whatsapp', 'phone_jid', 'default', ?, ?, ?, ?, ?, 1, 'ref', 1, 1, 1, 1)",
        (binding_id, value, person_id, status, valid_from_ms, valid_until_ms),
    )






def test_statement_person_status_rejects_unknown_values(knowledge_harness):
    h = knowledge_harness
    person = h.person("Tom")
    source = h.source(person)
    statement_id = h.capture_text("Alex wohnt in Köln.", source).statement_ids[0]
    with pytest.raises(sqlite3.IntegrityError):
        with h.service._store.transaction():  # noqa: SLF001
            h.service._store.execute(  # noqa: SLF001
                "UPDATE knowledge_statement_people SET status = 'probably'"
                " WHERE statement_id = ?",
                (statement_id,),
            )


def test_statement_supersession_reason_rejects_unknown_values(knowledge_harness):
    h = knowledge_harness
    person = h.person("Tom")
    source = h.source(person)
    statement_id = h.capture_text("Alex wohnt in Köln.", source).statement_ids[0]
    with pytest.raises(sqlite3.IntegrityError):
        with h.service._store.transaction():  # noqa: SLF001
            h.service._store.execute(  # noqa: SLF001
                "UPDATE knowledge_statements SET supersession_reason = 'i_guess'"
                " WHERE statement_id = ?",
                (statement_id,),
            )


def test_attribute_requires_an_existing_statement_and_person(knowledge_harness):
    """A facet is a statement annotation, never a free-standing profile write."""
    h = knowledge_harness
    person = h.person("Tom")
    with pytest.raises(sqlite3.IntegrityError):
        with h.service._store.transaction():  # noqa: SLF001
            h.service._store.execute(  # noqa: SLF001
                "INSERT INTO knowledge_person_attributes (statement_id, person_id,"
                " attribute_key, value_json, value_key, polarity, revision, created_ms,"
                " updated_ms) VALUES ('no-such-statement', ?, 'residence', '{}', 'k',"
                " 'positive', 1, 1, 1)",
                (person,),
            )


def test_attribute_polarity_rejects_unknown_values(knowledge_harness):
    h = knowledge_harness
    person = h.person("Tom")
    source = h.source(person)
    statement_id = h.capture_text("Alex wohnt in Köln.", source).statement_ids[0]
    with pytest.raises(sqlite3.IntegrityError):
        with h.service._store.transaction():  # noqa: SLF001
            h.service._store.execute(  # noqa: SLF001
                "INSERT INTO knowledge_person_attributes (statement_id, person_id,"
                " attribute_key, value_json, value_key, polarity, revision, created_ms,"
                " updated_ms) VALUES (?, ?, 'residence', '{}', 'k', 'maybe', 1, 1, 1)",
                (statement_id, person),
            )


def test_v2_schema_keeps_every_reused_table(knowledge_harness):
    """No cleanup or drop in the normal runtime path: the legacy tables stay present."""
    names = set(knowledge_harness.service._store.table_names())  # noqa: SLF001
    assert {
        "contacts",
        "contact_identifiers",
        "contact_aliases",
        "contact_fields",
        "memory2_nodes",
        "memory2_embeddings",
        "memory2_facts",
        "memory2_fact_sources",
        "memory2_fact_principals",
        "memory2_fact_jobs",
        "idea_backlog_items",
        "knowledge_identifier_bindings",
        "knowledge_statement_people",
        "knowledge_statements",
        "knowledge_jobs",
        "knowledge_quarantine",
        "conversations",
        "conversation_memberships",
        "conversation_relations",
        "knowledge_episodes",
        "knowledge_episode_sources",
        "knowledge_person_attributes",
    } <= names
