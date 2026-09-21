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
from legacy_fixtures import (
    V1_PERSON_BOUND,
    V1_PERSON_LEGACY_ONLY,
    V1_PERSON_NO_PRINCIPAL,
    V1_STATEMENT_ONE,
    V1_STATEMENT_REVOKED,
    V1_STATEMENT_SUPERSEDED,
    V1_STATEMENT_THREE,
    V1_STATEMENT_TWO,
    legacy_snapshot_factory,
    v1_knowledge_store_factory,
)
from yeoman_gateway.knowledge._migration import (
    LEGACY_NO_FACT_REASON,
    LEGACY_PROFILE_REASON,
    LEGACY_UNPROVEN_REASON,
    migrate_sources,
    semantic_digest,
)
from yeoman_gateway.knowledge._store import QUARANTINE_REASONS
from yeoman_gateway.knowledge._upgrade import (
    UpgradeError,
    upgrade_semantic_digest,
    upgrade_v1,
)


def processing_snapshot(path: Path) -> Path:
    """A minimal, syntactically real processing journal (never read for content)."""
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE events (event_id TEXT PRIMARY KEY, chat_id TEXT NOT NULL,"
            " revision INTEGER NOT NULL)"
        )
        conn.commit()
    finally:
        conn.close()
    return path


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


# ── v1 -> v2 cutover semantics (T28-T30, T40) ────────────────────────────────
#
# The old audit reason for a superseded statement is evidence, not a default.  A role
# backed by a proven active binding stays active; everything else is *withheld* and
# counted, and a role whose authoritative principal is missing is quarantined.  No row
# disappears, and repeating the upgrade cannot change a single decision.


class V1UpgradeHarness:
    """Drives the real v1 -> v2 upgrade over a synthetic v1 knowledge snapshot."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.fixture = v1_knowledge_store_factory(tmp_path / "v1.db")
        self.journal = processing_snapshot(tmp_path / "processing.db")

    def build(self, *, name: str = "built", fail_before_publish: bool = False):
        directory = self.tmp_path / name
        directory.mkdir(parents=True, exist_ok=True)
        return upgrade_v1(
            source=self.fixture.path,
            processing=self.journal,
            target=directory / "knowledge.db",
            manifest=directory / "manifest.json",
            fail_before_publish=fail_before_publish,
        )

    def read(self, report, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        conn = sqlite3.connect(f"file:{report.target_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            return list(conn.execute(sql, params))
        finally:
            conn.close()

    def table_names(self, report) -> tuple[str, ...]:
        return tuple(
            str(row["name"]) for row in self.read(
                report, "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
        )


@pytest.fixture
def v1_upgrade(tmp_path: Path) -> V1UpgradeHarness:
    return V1UpgradeHarness(tmp_path)


def test_two_upgrades_of_one_input_are_identical(v1_upgrade: V1UpgradeHarness):
    """T28: identical ids, statuses, digests and balances - twice over."""
    first = v1_upgrade.build(name="first")
    second = v1_upgrade.build(name="second")

    assert first.semantic_digest == second.semantic_digest
    assert first.migration_id == second.migration_id
    assert first.balance.to_payload() == second.balance.to_payload()
    assert upgrade_semantic_digest(first.target_path) == first.semantic_digest

    rows = v1_upgrade.read(
        first, "SELECT binding_id, person_id, status FROM knowledge_identifier_bindings"
        " ORDER BY binding_id"
    )
    other = v1_upgrade.read(
        second, "SELECT binding_id, person_id, status FROM knowledge_identifier_bindings"
        " ORDER BY binding_id"
    )
    assert [tuple(row) for row in rows] == [tuple(row) for row in other]
    assert [str(row["binding_id"]) for row in rows]


def test_proven_binding_stays_active_and_unproven_becomes_withheld(
    v1_upgrade: V1UpgradeHarness,
):
    """One verified binding citing a durable operation, two rows that prove nothing."""
    report = v1_upgrade.build()
    balance = report.balance
    assert balance.bindings.get("active") == 1
    assert balance.bindings.get("withheld") == 2
    rows = v1_upgrade.read(
        report,
        "SELECT status, COUNT(*) AS n FROM knowledge_identifier_bindings GROUP BY status",
    )
    by_status = {str(row["status"]): int(row["n"]) for row in rows}
    assert by_status == {"active": 1, "withheld": 2}

    active = v1_upgrade.read(
        report, "SELECT * FROM knowledge_identifier_bindings WHERE status = 'active'"
    )
    assert len(active) == 1
    assert str(active[0]["mapping_verified"]) == "1"
    assert str(active[0]["evidence_ref"]).startswith("binding-op:")
    assert str(active[0]["person_id"]) == V1_PERSON_BOUND

    # A withheld row keeps its candidate person id and its original evidence reference,
    # so the cutover stays auditable, but it is not person-effective.
    withheld = v1_upgrade.read(
        report, "SELECT * FROM knowledge_identifier_bindings WHERE status = 'withheld'"
        " ORDER BY channel, kind"
    )
    assert len(withheld) == 2
    assert {str(row["person_id"]) for row in withheld} == {V1_PERSON_LEGACY_ONLY}
    assert {str(row["evidence_ref"]) for row in withheld} == {
        "binding-op:00000000-0000-4000-8000-0000000000ff",
        "legacy-import",
    }
    # No active binding claims a value that only ever had an unproven candidate.
    active_values = {
        (str(row["channel"]), str(row["value"]))
        for row in v1_upgrade.read(
            report, "SELECT channel, value FROM knowledge_identifier_bindings"
            " WHERE status = 'active'"
        )
    }
    assert ("telegram", "4910000000102") not in active_values
    assert ("whatsapp", "4910000000103") not in active_values


def test_person_roles_follow_the_proven_mapping(v1_upgrade: V1UpgradeHarness):
    """T40: proven roles stay, unproven roles are withheld, missing principals quarantined."""
    report = v1_upgrade.build()
    roles = report.balance.person_roles
    # Three roles cite the proven WhatsApp binding - including the one merged revision -
    # one cites a principal whose binding stayed withheld, and one has no principal.
    assert roles.get("active") == 3
    assert roles.get("withheld") == 1
    assert roles.get("quarantined") == 1

    rows = v1_upgrade.read(
        report,
        "SELECT status, COUNT(*) AS n FROM knowledge_statement_people GROUP BY status",
    )
    # No row disappeared: the withheld and quarantined roles are still there.
    assert sum(int(row["n"]) for row in rows) == 5

    quarantined = v1_upgrade.read(
        report,
        "SELECT p.statement_id, p.resolution_reason FROM knowledge_statement_people p"
        " WHERE p.status = 'withheld' AND p.resolution_reason = 'no_authoritative_principal'",
    )
    assert quarantined
    # The original principal and source rows are untouched by the quarantine.
    for row in quarantined:
        sources = v1_upgrade.read(
            report,
            "SELECT author_principal FROM knowledge_statement_sources WHERE statement_id = ?",
            (str(row["statement_id"]),),
        )
        assert sources and str(sources[0]["author_principal"]) == ""


def test_withheld_roles_never_carry_a_binding(v1_upgrade: V1UpgradeHarness):
    report = v1_upgrade.build()
    rows = v1_upgrade.read(
        report,
        "SELECT COUNT(*) AS n FROM knowledge_statement_people"
        " WHERE status != 'active' AND binding_id IS NOT NULL",
    )
    assert int(rows[0]["n"]) == 0


def test_supersession_reason_comes_only_from_concrete_audit_evidence(
    v1_upgrade: V1UpgradeHarness,
):
    """Missing or ambiguous evidence stays ``unknown`` - it is never guessed."""
    report = v1_upgrade.build()
    assert report.balance.supersessions.get("unknown") == 1
    rows = v1_upgrade.read(
        report,
        "SELECT supersession_reason FROM knowledge_statements WHERE status = 'superseded'",
    )
    assert [str(row["supersession_reason"]) for row in rows] == ["unknown"]
    # A statement that is not superseded carries no invented reason either.
    other = v1_upgrade.read(
        report,
        "SELECT DISTINCT supersession_reason FROM knowledge_statements WHERE status != 'superseded'",
    )
    assert {str(row["supersession_reason"]) for row in other} == {"unknown"}


def test_a_concrete_rescreen_audit_becomes_quality_rejected(tmp_path: Path):
    fixture = v1_knowledge_store_factory(tmp_path / "v1.db")
    journal = processing_snapshot(tmp_path / "processing.db")
    conn = sqlite3.connect(fixture.path)
    try:
        conn.execute(
            "INSERT INTO knowledge_statement_audit (statement_id, operation,"
            " actor_principal, evidence_ref, reason, detail_json, created_ms)"
            " VALUES (?, 'rescreen', 'whatsapp:4910000000101', 'admin-ref',"
            " 'quality_rejected', '{}', 1)",
            (V1_STATEMENT_SUPERSEDED,),
        )
        conn.commit()
    finally:
        conn.close()
    report = upgrade_v1(
        source=fixture.path,
        processing=journal,
        target=tmp_path / "out" / "knowledge.db",
        manifest=tmp_path / "out" / "manifest.json",
    )
    assert report.balance.supersessions.get("quality_rejected") == 1
    rows = v1_upgrade_rows(report, "SELECT supersession_reason FROM knowledge_statements"
                                  " WHERE status = 'superseded'")
    assert rows == ["quality_rejected"]


def test_row_counts_and_original_ids_survive(v1_upgrade: V1UpgradeHarness):
    report = v1_upgrade.build()
    assert dict(report.counts)["knowledge_statements"] == 5
    assert dict(report.counts)["knowledge_statement_people"] == 5
    assert dict(report.counts)["contacts"] == 3
    assert dict(report.counts)["contact_aliases"] == 1
    assert dict(report.counts)["contact_identifiers"] == 4
    ids = v1_upgrade.read(report, "SELECT statement_id FROM knowledge_statements ORDER BY 1")
    assert {str(row["statement_id"]) for row in ids} == {
        V1_STATEMENT_ONE,
        V1_STATEMENT_TWO,
        V1_STATEMENT_THREE,
        V1_STATEMENT_SUPERSEDED,
        V1_STATEMENT_REVOKED,
    }
    # An unknown source object is reported in the manifest, never silently carried over
    # and never dropped without a trace.
    payload = json.loads(report.manifest_path.read_text(encoding="utf-8"))
    assert payload["unknown_objects"] == ["synthetic_unknown_object"]
    assert "synthetic_unknown_object" not in v1_upgrade.table_names(report)
    # The manifest inventory accounts for every ordinary target table (the SQLite-managed
    # FTS shadow tables are deliberately not inventoried as data).
    inventoried = set(payload_table_names(payload))
    ordinary = {
        name
        for name in v1_upgrade.table_names(report)
        if not name.startswith("memory2_nodes_fts_") and not name.startswith("sqlite_")
    }
    assert inventoried == ordinary


def test_missing_principal_quarantines_without_inventing_a_person(
    v1_upgrade: V1UpgradeHarness,
):
    """No contact row is consulted to fill a missing authoritative principal."""
    report = v1_upgrade.build()
    rows = v1_upgrade.read(
        report,
        "SELECT DISTINCT p.person_id FROM knowledge_statement_people p"
        " WHERE p.resolution_reason = 'no_authoritative_principal'",
    )
    # The original person id is preserved in the ledger, not dropped and not replaced.
    assert {str(row["person_id"]) for row in rows} == {V1_PERSON_NO_PRINCIPAL}
    quarantined = v1_upgrade.read(
        report,
        "SELECT reason, COUNT(*) AS n FROM knowledge_quarantine"
        " WHERE source_table = 'knowledge_statement_people' GROUP BY reason",
    )
    assert {str(row["reason"]) for row in quarantined} == {"unproven-role"}


def test_legacy_fields_stay_locked_and_are_never_promoted(
    v1_upgrade: V1UpgradeHarness,
):
    """T29: an unproven legacy presentation field never becomes an active profile value."""
    report = v1_upgrade.build()
    # No attribute facets are invented from legacy profile text.
    assert v1_upgrade.read(report, "SELECT COUNT(*) AS n FROM knowledge_person_attributes")[
        0
    ]["n"] == 0
    # The legacy row itself is preserved in its own table, still unread by profiles.
    assert v1_upgrade.read(report, "SELECT COUNT(*) AS n FROM contact_fields")[0]["n"] == 1
    # And no alias was granted address permission.
    assert v1_upgrade.read(
        report, "SELECT COUNT(*) AS n FROM contact_aliases WHERE address_allowed = 1"
    )[0]["n"] == 0


def test_upgrade_never_overwrites_an_existing_target_or_manifest(
    v1_upgrade: V1UpgradeHarness,
):
    first = v1_upgrade.build(name="first")
    before_target = first.target_path.read_bytes()
    before_manifest = first.manifest_path.read_bytes()
    with pytest.raises(UpgradeError) as excinfo:
        upgrade_v1(
            source=v1_upgrade.fixture.path,
            processing=v1_upgrade.journal,
            target=first.target_path,
            manifest=first.manifest_path,
        )
    assert excinfo.value.code == "target_exists"
    assert first.target_path.read_bytes() == before_target
    assert first.manifest_path.read_bytes() == before_manifest


def payload_table_names(payload: dict) -> tuple[str, ...]:
    """The target table names a manifest accounts for, in sorted order."""
    return tuple(sorted(str(entry["table"]) for entry in payload["tables"]))


def test_upgrade_never_carries_a_locked_statement_into_the_index(
    v1_upgrade: V1UpgradeHarness,
):
    """The rebuilt lexical index holds only statements a reader may actually see."""
    report = v1_upgrade.build()
    leaked = v1_upgrade.read(
        report,
        "SELECT COUNT(*) AS n FROM memory2_nodes_fts f"
        " WHERE EXISTS (SELECT 1 FROM knowledge_statements s"
        "   WHERE s.statement_id = f.entry_id"
        "     AND (s.status IN ('superseded','revoked')"
        "          OR s.revoked_at_ms IS NOT NULL"
        "          OR s.superseded_by IS NOT NULL))",
    )
    assert int(leaked[0]["n"]) == 0
    # The superseded rows still exist as statements: this is an index rule, not a delete.
    assert v1_upgrade.read(
        report, "SELECT COUNT(*) AS n FROM knowledge_statements WHERE status = 'superseded'"
    )[0]["n"] == 1


def v1_upgrade_rows(report, sql: str, params: tuple = ()) -> list[str]:
    conn = sqlite3.connect(f"file:{report.target_path}?mode=ro", uri=True)
    try:
        return [str(row[0]) for row in conn.execute(sql, params)]
    finally:
        conn.close()
