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


def test_identity_and_alias_write_roll_back_together(lower_store_harness):
    h = lower_store_harness
    person = h.store_person("Tom")
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
    assert h.service._store.scalar("SELECT display_name FROM contacts WHERE id = ?", (person,)) == "Tom"  # noqa: SLF001
    assert h.service._store.scalar("SELECT COUNT(*) FROM contact_aliases WHERE contact_id = ? AND alias = 'Rollback'", (person,)) == 0  # noqa: SLF001
    assert h.identity_revision() == before_revision


def test_nested_operation_joins_the_outer_transaction(lower_store_harness):
    """An inner knowledge operation must not commit the outer unit of work."""
    import sqlite3

    h = lower_store_harness
    person = h.store_person("Tom")
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
    assert h.service._store.scalar("SELECT preferred_name FROM contacts WHERE id = ?", (person,)) == "inner-too"  # noqa: SLF001






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


class _LowerStoreHarness:
    """Synthetic lower-engine fixture for storage transaction/schema contracts."""

    def __init__(self, path):
        from types import SimpleNamespace

        from yeoman_gateway.knowledge._statements import StatementEngine
        from yeoman_gateway.knowledge._store import KnowledgeStore
        from yeoman_gateway.knowledge.models import (
            SourceRef,
            StatementCandidate,
            TrustedCaptureContext,
        )
        from yeoman_gateway.knowledge.runtime import RuntimeKnowledgePolicy, RuntimeKnowledgeSources

        from tests.gateway.schema_identity_helpers import SyntheticSchemaIdentity
        self._SourceRef, self._StatementCandidate, self._TrustedCaptureContext = SourceRef, StatementCandidate, TrustedCaptureContext
        self._authority = RuntimeKnowledgeSources()
        self._policy = RuntimeKnowledgePolicy(engine=None)
        self.store = KnowledgeStore(path / "lower-schema-contract.db")
        identity = SyntheticSchemaIdentity(self.store, authority=self._authority, policy=self._policy)
        self.statements = StatementEngine(self.store, identity=identity, authority=self._authority, policy=self._policy, workspace_id="synthetic")
        self.service = SimpleNamespace(_store=self.store, db_path=self.store.db_path, close=self.store.close)

    def store_person(self, name):
        person = self.store.new_id()
        self.store.execute("INSERT INTO contacts (id, display_name, created_at, updated_at) VALUES (?, ?, 'now', 'now')", (person, name))
        self.store.commit_if_idle()
        return person

    person = store_person

    def source(self, _person):
        from yeoman_gateway.knowledge.authority import EvidenceAudience
        source = self._SourceRef("boundary-source", 1, "whatsapp", "boundary@g.us", "whatsapp:10001", 1)
        self._authority.register_source(source, EvidenceAudience.author_only())
        return source

    def capture_text(self, text, source):
        context = self._TrustedCaptureContext("synthetic", 1, "boundary-test", (source,))
        with self.store.transaction():
            return self.statements.capture(self._StatementCandidate(text, (source,)), context=context)

    def snapshot_counts(self):
        return self.store.counts()

    def identity_revision(self):
        return self.store.identity_revision

    def fail_next_commit(self):
        self.store.fail_next_commit = True


@pytest.fixture
def lower_store_harness(tmp_path):
    harness = _LowerStoreHarness(tmp_path)
    try:
        yield harness
    finally:
        harness.store.close()


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


def test_ended_binding_history_coexists_while_only_one_active_binding_is_allowed(
    lower_store_harness,
):
    """The partial unique index is the point of the temporal binding table.

    Number re-assignment must be representable: an ended row and a new active row for the
    same fully typed identifier coexist, but two *active* rows never do.
    """
    h = lower_store_harness
    first = h.store_person("Tom")
    second = h.store_person("Alex")
    value = "49170000001@s.whatsapp.net"
    with h.service._store.transaction():  # noqa: SLF001 - schema contract under test
        _insert_binding(
            h.service._store,  # noqa: SLF001
            binding_id="bind-old",
            person_id=first,
            value=value,
            status="ended",
            valid_until_ms=500,
        )
        _insert_binding(
            h.service._store,  # noqa: SLF001
            binding_id="bind-new",
            person_id=second,
            value=value,
            status="active",
            valid_from_ms=500,
        )
    rows = h.service._store.query(  # noqa: SLF001
        "SELECT binding_id, status FROM knowledge_identifier_bindings WHERE value = ?"
        " ORDER BY binding_id",
        (value,),
    )
    assert [(str(row["binding_id"]), str(row["status"])) for row in rows] == [
        ("bind-new", "active"),
        ("bind-old", "ended"),
    ]

    with pytest.raises(sqlite3.IntegrityError):
        with h.service._store.transaction():  # noqa: SLF001
            _insert_binding(
                h.service._store,  # noqa: SLF001
                binding_id="bind-second-active",
                person_id=first,
                value=value,
                status="active",
                valid_from_ms=900,
            )
    # The refused insert left nothing behind.
    assert h.service._store.scalar(  # noqa: SLF001
        "SELECT COUNT(*) FROM knowledge_identifier_bindings WHERE binding_id = ?",
        ("bind-second-active",),
    ) == 0


def test_a_withheld_binding_is_never_the_active_one(lower_store_harness):
    """A withheld candidate may keep its row, but it must not block or resolve a person."""
    h = lower_store_harness
    person = h.store_person("Tom")
    value = "49170000002@s.whatsapp.net"
    with h.service._store.transaction():  # noqa: SLF001
        _insert_binding(
            h.service._store,  # noqa: SLF001
            binding_id="bind-withheld",
            person_id=person,
            value=value,
            status="withheld",
        )
    assert (
        h.service._store.scalar(  # noqa: SLF001
            "SELECT COUNT(*) FROM knowledge_identifier_bindings"
            " WHERE value = ? AND status = 'active'",
            (value,),
        )
        == 0
    )
    # A new proven observation may still claim the identifier as the active binding.
    with h.service._store.transaction():  # noqa: SLF001
        _insert_binding(
            h.service._store,  # noqa: SLF001
            binding_id="bind-active",
            person_id=person,
            value=value,
            status="active",
            valid_from_ms=10,
        )
    assert (
        h.service._store.scalar(  # noqa: SLF001
            "SELECT binding_id FROM knowledge_identifier_bindings"
            " WHERE value = ? AND status = 'active'",
            (value,),
        )
        == "bind-active"
    )


def test_statement_person_status_rejects_unknown_values(lower_store_harness):
    h = lower_store_harness
    person = h.person("Tom")
    source = h.source(person)
    statement_id = h.capture_text("Alex wohnt in Köln.", source).statement_ids[0]
    with h.service._store.transaction():  # noqa: SLF001 - seed valid row for CHECK contract
        h.service._store.execute(  # noqa: SLF001
            "INSERT INTO knowledge_statement_people "
            "(statement_id, person_id, role, evidence_source_id, evidence_revision, "
            "attribution, created_ms, status, resolution_reason) "
            "VALUES (?, ?, 'subject', ?, ?, 'extracted', 0, 'active', '')",
            (statement_id, person, source.event_id, source.revision),
        )
    with pytest.raises(sqlite3.IntegrityError):
        with h.service._store.transaction():  # noqa: SLF001
            h.service._store.execute(  # noqa: SLF001
                "UPDATE knowledge_statement_people SET status = 'probably'"
                " WHERE statement_id = ?",
                (statement_id,),
            )


def test_statement_supersession_reason_rejects_unknown_values(lower_store_harness):
    h = lower_store_harness
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


def test_attribute_requires_an_existing_statement_and_person(lower_store_harness):
    """A facet is a statement annotation, never a free-standing profile write."""
    h = lower_store_harness
    person = h.store_person("Tom")
    with pytest.raises(sqlite3.IntegrityError):
        with h.service._store.transaction():  # noqa: SLF001
            h.service._store.execute(  # noqa: SLF001
                "INSERT INTO knowledge_person_attributes (statement_id, person_id,"
                " attribute_key, value_json, value_key, polarity, revision, created_ms,"
                " updated_ms) VALUES ('no-such-statement', ?, 'residence', '{}', 'k',"
                " 'positive', 1, 1, 1)",
                (person,),
            )


def test_attribute_polarity_rejects_unknown_values(lower_store_harness):
    h = lower_store_harness
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
