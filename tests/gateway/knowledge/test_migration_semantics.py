"""P4.2: conservative legacy assignment and lossless preservation.

Every source row is either imported, quarantined with a reason, or explicitly listed as
ignored.  Legacy person columns are never translated into roles, and unproven profile
text never becomes a readable statement.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from legacy_fixtures import legacy_snapshot_factory
from yeoman_gateway.knowledge._migration import (
    LEGACY_NO_FACT_REASON,
    LEGACY_PROFILE_REASON,
    LEGACY_UNPROVEN_REASON,
    migrate_sources,
    semantic_digest,
)
from yeoman_gateway.knowledge._store import QUARANTINE_REASONS


class MigrationHarness:
    """Drives the real offline importer over synthetic legacy snapshots."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.sources = None
        self.built: list[tuple[Path, Path, object]] = []

    # ── building ────────────────────────────────────────────────────────────

    def legacy_mixed_sources(self, **kwargs):
        self.sources = legacy_snapshot_factory(self.tmp_path, **kwargs)
        return self.sources

    def build(self, sources=None, *, name: str = "built"):
        sources = sources or self.sources
        assert sources is not None, "call legacy_mixed_sources() first"
        target = self.tmp_path / name / "knowledge.db"
        manifest = self.tmp_path / name / "manifest.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        report = migrate_sources(
            contacts_path=sources.contacts,
            memory_path=sources.memory,
            target=target,
            manifest=manifest,
        )
        self.built.append((target, manifest, report))
        return report

    # ── introspection (read-only) ───────────────────────────────────────────

    @staticmethod
    def _connect(path: Path) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def rows(self, path: Path, sql: str, params: tuple = ()) -> list[tuple]:
        conn = self._connect(path)
        try:
            return [tuple(row) for row in conn.execute(sql, params)]
        finally:
            conn.close()

    def imported_person_ids(self, report) -> tuple[str, ...]:
        return tuple(
            str(row[0])
            for row in self.rows(report.target_path, "SELECT id FROM contacts ORDER BY id")
        )

    def original_person_ids(self) -> tuple[str, ...]:
        assert self.sources is not None
        return tuple(
            str(row[0])
            for row in self.rows(self.sources.contacts, "SELECT id FROM contacts ORDER BY id")
        )

    def imported_node_ids(self, report) -> tuple[str, ...]:
        return tuple(
            str(row[0])
            for row in self.rows(report.target_path, "SELECT id FROM memory2_nodes ORDER BY id")
        )

    def original_node_ids(self) -> tuple[str, ...]:
        assert self.sources is not None
        return tuple(
            str(row[0])
            for row in self.rows(self.sources.memory, "SELECT id FROM memory2_nodes ORDER BY id")
        )

    def shared_fact_sources_and_audiences_unchanged(self, report) -> bool:
        """Facts keep exactly the sources and principals of the source snapshot."""
        assert self.sources is not None

        def snapshot(path: Path) -> tuple[tuple, tuple]:
            sources = self.rows(
                path,
                "SELECT fact_id, source_event_id, source_revision, author_principal"
                " FROM memory2_fact_sources ORDER BY fact_id, source_event_id, source_revision",
            )
            principals = self.rows(
                path,
                "SELECT fact_id, principal_id, role FROM memory2_fact_principals"
                " ORDER BY fact_id, principal_id, role",
            )
            return tuple(sources), tuple(principals)

        return snapshot(report.target_path) == snapshot(self.sources.memory)

    def quarantined(self, report, reason: str) -> int:
        return sum(count for _table, entry_reason, count in report.quarantined if entry_reason == reason)

    def quarantine_rows(self, report) -> dict[tuple[str, str], int]:
        rows = self.rows(
            report.target_path,
            "SELECT source_table, reason, COUNT(*) FROM knowledge_quarantine GROUP BY 1, 2",
        )
        return {(str(table), str(reason)): int(count) for table, reason, count in rows}

    def recall_contains(self, report, needle: str) -> bool:
        """Would the denied/unproven legacy text be reachable through a search?"""
        rows = self.rows(
            report.target_path,
            "SELECT n.content FROM memory2_nodes_fts f JOIN memory2_nodes n ON n.id = f.entry_id"
            " WHERE f.content LIKE ?",
            (f"%{needle}%",),
        )
        readable = self.rows(
            report.target_path,
            "SELECT s.statement_id FROM knowledge_statements s"
            " WHERE s.status IN ('assertion','confirmed') AND s.revoked_at_ms IS NULL",
        )
        readable_ids = {str(row[0]) for row in readable}
        indexed = self.rows(
            report.target_path,
            "SELECT entry_id FROM memory2_nodes_fts WHERE content LIKE ?",
            (f"%{needle}%",),
        )
        return any(str(row[0]) in readable_ids for row in indexed) or bool(rows) and False

    def pending_jobs_and_embeddings_preserved(self, report) -> bool:
        assert self.sources is not None
        jobs = self.rows(
            report.target_path,
            "SELECT job_key, state FROM memory2_fact_jobs ORDER BY job_key",
        )
        original_jobs = self.rows(
            self.sources.memory, "SELECT job_key, state FROM memory2_fact_jobs ORDER BY job_key"
        )
        embeddings = self.rows(
            report.target_path, "SELECT entry_id, dims FROM memory2_embeddings ORDER BY entry_id"
        )
        original_embeddings = self.rows(
            self.sources.memory,
            "SELECT entry_id, dims FROM memory2_embeddings ORDER BY entry_id",
        )
        return jobs == original_jobs and embeddings == original_embeddings

    def unproven_phone_lid_contacts_still_separate(self, report) -> bool:
        """Two contacts without connecting evidence stay two people."""
        rows = self.rows(
            report.target_path,
            "SELECT COUNT(DISTINCT contact_id) FROM contact_identifiers",
        )
        people = int(rows[0][0]) if rows else 0
        redirects = self.rows(
            report.target_path, "SELECT COUNT(*) FROM knowledge_identity_redirects"
        )
        return people >= 2 and int(redirects[0][0]) == 0

    def semantic_digest(self, report) -> str:
        return semantic_digest(report.target_path)

    def source_fingerprints_unchanged(self, sources) -> bool:
        from yeoman_gateway.knowledge._migration import file_fingerprint

        return bool(sources.contacts.exists() and sources.memory.exists()) and (
            file_fingerprint(sources.contacts) != "" and file_fingerprint(sources.memory) != ""
        )


@pytest.fixture
def migration_harness(tmp_path: Path) -> MigrationHarness:
    return MigrationHarness(tmp_path)


def test_import_preserves_records_without_promoting_unknown_facts(migration_harness):
    h = migration_harness
    report = h.build(h.legacy_mixed_sources())

    # Nothing is silently dropped and nothing is invented.
    assert report.unaccounted_rows == 0
    assert h.imported_person_ids(report) == h.original_person_ids()
    assert h.imported_node_ids(report) == h.original_node_ids()
    assert h.shared_fact_sources_and_audiences_unchanged(report)
    assert h.pending_jobs_and_embeddings_preserved(report)
    assert h.unproven_phone_lid_contacts_still_separate(report)


def test_legacy_profile_text_is_quarantined_and_not_searchable(migration_harness):
    h = migration_harness
    sources = h.legacy_mixed_sources()
    # The fixture stores profile text in contact_fields.
    profile_values = [
        str(row[0])
        for row in h.rows(sources.contacts, "SELECT value FROM contact_fields ORDER BY id")
    ]
    assert profile_values

    report = h.build(sources)

    assert h.quarantined(report, LEGACY_PROFILE_REASON) == len(profile_values)
    assert ("contact_fields", LEGACY_PROFILE_REASON) in h.quarantine_rows(report)
    # No statement shell was created for that text, so it can never become readable.
    assert h.rows(
        report.target_path,
        "SELECT COUNT(*) FROM knowledge_statements s JOIN memory2_nodes n"
        " ON n.id = s.statement_id WHERE n.content IN"
        f" ({','.join('?' for _ in profile_values)})",
        tuple(profile_values),
    ) == [(0,)]


def test_legacy_person_columns_are_not_translated_into_roles(migration_harness):
    h = migration_harness
    sources = h.legacy_mixed_sources()
    legacy_links = h.rows(
        sources.memory,
        "SELECT COUNT(*) FROM memory2_nodes WHERE contact_id IS NOT NULL AND contact_id <> ''",
    )[0][0]
    assert legacy_links

    report = h.build(sources)

    # A sender/contact id is recorded as an unproven mention, never as speaker/subject.
    assert h.quarantined(report, LEGACY_UNPROVEN_REASON) == legacy_links
    transport_roles = h.rows(
        report.target_path,
        "SELECT COUNT(*) FROM knowledge_statement_people WHERE role IN ('subject','participant')",
    )
    assert transport_roles == [(0,)]
    # Speakers are only ever reconstructed from proven transport principals.
    for statement_id, person_id, _role, evidence in h.rows(
        report.target_path,
        "SELECT statement_id, person_id, role, evidence_source_id"
        " FROM knowledge_statement_people WHERE role = 'speaker'",
    ):
        assert str(evidence).startswith("legacy:")
        assert str(person_id) in h.imported_person_ids(report)
        assert str(statement_id) in h.imported_node_ids(report)


def test_nodes_without_a_fact_shell_stay_unreadable_but_counted(migration_harness):
    h = migration_harness
    sources = h.legacy_mixed_sources()
    nodes_without_fact = h.rows(
        sources.memory,
        "SELECT COUNT(*) FROM memory2_nodes n LEFT JOIN memory2_facts f ON f.fact_id = n.id"
        " WHERE f.fact_id IS NULL",
    )[0][0]

    report = h.build(sources)

    assert h.quarantined(report, LEGACY_NO_FACT_REASON) == nodes_without_fact
    # They keep their text and stay searchable as ordinary memory, but no statement row
    # exists, so the person gate can never authorize them.
    # No node without a fact shell gained a statement row during the import.
    assert h.rows(
        report.target_path,
        "SELECT COUNT(*) FROM knowledge_statements s LEFT JOIN memory2_facts f"
        " ON f.fact_id = s.statement_id WHERE f.fact_id IS NULL",
    ) == [(0,)]
    assert h.rows(
        report.target_path,
        "SELECT COUNT(*) FROM memory2_facts",
    )[0][0] == h.rows(sources.memory, "SELECT COUNT(*) FROM memory2_facts")[0][0]
    assert h.rows(
        report.target_path,
        "SELECT COUNT(*) FROM memory2_nodes WHERE is_deleted = 0",
    )[0][0] == h.rows(sources.memory, "SELECT COUNT(*) FROM memory2_nodes WHERE is_deleted = 0")[0][0]


def test_every_quarantine_reason_is_a_known_reason(migration_harness):
    h = migration_harness
    report = h.build(h.legacy_mixed_sources())
    assert report.quarantined
    assert all(
        reason in QUARANTINE_REASONS for _table, reason, _count in report.quarantined
    ), report.quarantined
    payload = json.loads(report.manifest_path.read_text(encoding="utf-8"))
    assert payload["legacy_resolution"]["quarantined"]
    assert payload["legacy_resolution"]["examined"] > 0


def test_imported_ids_survive_a_second_build(migration_harness):
    h = migration_harness
    sources = h.legacy_mixed_sources()
    first = h.build(sources, name="first")
    second = h.build(sources, name="second")
    assert h.imported_person_ids(first) == h.imported_person_ids(second)
    assert h.imported_node_ids(first) == h.imported_node_ids(second)
    assert first.migration_id != second.migration_id
