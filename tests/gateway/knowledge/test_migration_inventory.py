"""P4.1 migration tests: offline inventory, explicit build, and the CLI surface.

Every test runs fully offline against synthetic snapshots.  The tests drive the module
under test through its public functions and the Typer surface; they never reach into
the migration implementation to make an assertion pass.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
import typer
from legacy_fixtures import (
    ACTIVE_NODE_ID,
    CONFLICT_CONTACT_ID,
    CONTACTS_TABLE_ROWS,
    LEGACY_SCHEMA_VERSION,
    LINKED_NODE_ID,
    MEMORY_TABLE_ROWS,
    OWNER_CONTACT_ID,
    SECOND_CONTACT_ID,
    THIRD_CONTACT_ID,
    legacy_snapshot_factory,
)
from typer.testing import CliRunner
from yeoman_gateway.cli.knowledge_commands import knowledge_app
from yeoman_gateway.knowledge._migration import (
    MigrationSourceError,
    UnsupportedSchema,
    inspect_sources,
    migrate_sources,
    semantic_digest,
    verify_target,
)
from yeoman_gateway.knowledge._store import QUARANTINE_REASONS, TOOL_VERSION

CONTACTS_TABLES = ("contact_aliases", "contact_fields", "contact_identifiers", "contacts")
MEMORY_TABLES = (
    "idea_backlog_items",
    "memory2_embeddings",
    "memory2_fact_jobs",
    "memory2_fact_principals",
    "memory2_fact_sources",
    "memory2_facts",
    "memory2_meta",
    "memory2_nodes",
    "memory2_nodes_fts",
)
BASE_ROWS = {**CONTACTS_TABLE_ROWS, **MEMORY_TABLE_ROWS}


def _cli_app() -> typer.Typer:
    """The parent application the knowledge sub-app is registered on.

    ``cli/commands.py`` is owned by another change, so the test wires the sub-app the
    same way that module will: one ``add_typer`` call.
    """
    parent = typer.Typer()
    parent.add_typer(knowledge_app, name="knowledge")
    return parent


def _readonly(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _rows(path: Path, sql: str, params: tuple[object, ...] = ()) -> list[tuple[object, ...]]:
    connection = _readonly(path)
    try:
        return [tuple(row) for row in connection.execute(sql, params).fetchall()]
    finally:
        connection.close()


def _scalar(path: Path, sql: str, params: tuple[object, ...] = ()) -> object:
    rows = _rows(path, sql, params)
    return rows[0][0] if rows else None


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _stat(path: Path) -> tuple[int, int, str]:
    stat = path.stat()
    return stat.st_mtime_ns, stat.st_size, _sha256(path)


def _build_paths(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / "built" / "knowledge.db", tmp_path / "built" / "manifest.json"


# ── inventory ────────────────────────────────────────────────────────────────


def test_inspect_reads_wal_snapshot_without_writing(tmp_path: Path) -> None:
    sources = legacy_snapshot_factory(tmp_path, with_wal=True)
    # Sanity: the backups really were taken from WAL-backed sources.
    assert sources.contacts.read_bytes()[18:20] == b"\x02\x02"
    assert sources.memory.read_bytes()[18:20] == b"\x02\x02"
    assert sources.contacts_db.exists() and sources.memory_db.exists()

    before = {path: _stat(path) for path in (sources.contacts, sources.memory)}
    directory_before = sorted(item.name for item in sources.contacts.parent.iterdir())
    inventory = inspect_sources(sources.contacts, sources.memory)

    assert inventory.contacts.fingerprint == _sha256(sources.contacts)
    assert inventory.memory.fingerprint == _sha256(sources.memory)
    assert inventory.contacts.tables == CONTACTS_TABLES
    assert inventory.memory.tables == MEMORY_TABLES
    assert dict(inventory.contacts.row_counts) == CONTACTS_TABLE_ROWS
    assert dict(inventory.memory.row_counts) == MEMORY_TABLE_ROWS
    assert inventory.contacts.schema_version == ""
    assert inventory.memory.schema_version == LEGACY_SCHEMA_VERSION
    assert inventory.contacts.unsupported == ()
    assert inventory.memory.unsupported == ()
    assert inventory.contacts.identifier_conflicts == ()
    assert len(inventory.memory.node_versions) == MEMORY_TABLE_ROWS["memory2_nodes"]
    assert tuple(item[0] for item in inventory.memory.node_versions) == tuple(
        sorted(item[0] for item in inventory.memory.node_versions)
    )
    assert inventory.statements
    assert json.loads(inventory.to_json())["memory"]["schema_version"] == LEGACY_SCHEMA_VERSION

    for path, snapshot in before.items():
        assert _stat(path) == snapshot
    # Not even SQLite's read-only WAL sidecars are left behind.
    assert sorted(item.name for item in sources.contacts.parent.iterdir()) == directory_before


def test_missing_source_is_reported(tmp_path: Path) -> None:
    sources = legacy_snapshot_factory(tmp_path)
    missing = tmp_path / "does-not-exist.db"
    target, manifest = _build_paths(tmp_path)

    with pytest.raises(MigrationSourceError) as inspect_error:
        inspect_sources(missing, sources.memory)
    assert inspect_error.value.reason == "missing_source"
    assert str(missing) in inspect_error.value.detail

    with pytest.raises(MigrationSourceError) as migrate_error:
        migrate_sources(
            contacts_path=missing,
            memory_path=sources.memory,
            target=target,
            manifest=manifest,
        )
    assert migrate_error.value.reason == "missing_source"
    assert not target.exists()
    assert not manifest.exists()


def test_non_sqlite_source_is_rejected(tmp_path: Path) -> None:
    sources = legacy_snapshot_factory(tmp_path)
    fake = tmp_path / "not-a-database.db"
    fake.write_text("plain text, definitely not a SQLite database\n" * 12, encoding="utf-8")

    with pytest.raises(MigrationSourceError) as error:
        inspect_sources(fake, sources.memory)
    assert error.value.reason == "not_a_database"


def test_hot_journal_and_wal_sidecars_are_rejected(tmp_path: Path) -> None:
    sources = legacy_snapshot_factory(tmp_path)
    journal = sources.contacts.with_name(sources.contacts.name + "-journal")
    journal.write_bytes(b"synthetic hot journal")
    try:
        with pytest.raises(MigrationSourceError) as error:
            inspect_sources(sources.contacts, sources.memory)
        assert error.value.reason == "hot_journal"
    finally:
        journal.unlink()

    wal = sources.contacts.with_name(sources.contacts.name + "-wal")
    wal.write_bytes(b"synthetic uncheckpointed wal")
    try:
        with pytest.raises(MigrationSourceError) as error:
            inspect_sources(sources.contacts, sources.memory)
        assert error.value.reason == "hot_wal"
    finally:
        wal.unlink()


def test_identifier_conflicts_are_reported_not_merged(tmp_path: Path) -> None:
    sources = legacy_snapshot_factory(tmp_path, conflicting_identifier=True)
    inventory = inspect_sources(sources.contacts, sources.memory)
    conflicts = inventory.contacts.identifier_conflicts

    # One value, two channels, two different owners: a conflict, reported per row.
    assert ("whatsapp", "4910000000001@s.whatsapp.net") in conflicts
    assert ("telegram", "4910000000001") in conflicts
    # One value, two channels, the same owner: not a conflict.
    assert ("whatsapp", "4910000000002@s.whatsapp.net") not in conflicts
    assert ("telegram", "4910000000002") not in conflicts
    assert inventory.memory.identifier_conflicts == ()

    target, manifest = _build_paths(tmp_path)
    report = migrate_sources(
        contacts_path=sources.contacts,
        memory_path=sources.memory,
        target=target,
        manifest=manifest,
    )

    query = "SELECT channel, identifier, contact_id FROM contact_identifiers ORDER BY 1, 2, 3"
    assert _rows(target, query) == _rows(sources.contacts, query)
    assert _scalar(target, "SELECT COUNT(*) FROM contacts") == 4
    assert CONFLICT_CONTACT_ID in {row[0] for row in _rows(target, "SELECT id FROM contacts")}
    # Nothing was merged: no binding row was invented for the ambiguous value.
    assert _scalar(target, "SELECT COUNT(*) FROM knowledge_identifier_bindings") == 0
    assert dict((name, imported) for name, _source, imported in report.tables)[
        "contact_identifiers"
    ] == 5
    assert report.unaccounted_rows == 0
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["identifier_conflicts"]["count"] == 2
    assert payload["identifier_conflicts"]["channels"] == ["telegram", "whatsapp"]


def test_target_equal_to_source_is_rejected(tmp_path: Path) -> None:
    sources = legacy_snapshot_factory(tmp_path)
    before = _stat(sources.contacts)
    manifest = tmp_path / "manifest.json"

    with pytest.raises(MigrationSourceError) as error:
        migrate_sources(
            contacts_path=sources.contacts,
            memory_path=sources.memory,
            target=sources.contacts,
            manifest=manifest,
        )
    assert error.value.reason == "target_is_source"
    assert not manifest.exists()
    assert _stat(sources.contacts) == before


def test_existing_target_is_never_overwritten(tmp_path: Path) -> None:
    sources = legacy_snapshot_factory(tmp_path)
    target = tmp_path / "existing.db"
    target.write_bytes(b"pre-existing bytes")
    manifest = tmp_path / "manifest.json"

    with pytest.raises(MigrationSourceError) as error:
        migrate_sources(
            contacts_path=sources.contacts,
            memory_path=sources.memory,
            target=target,
            manifest=manifest,
        )
    assert error.value.reason == "target_exists"
    assert target.read_bytes() == b"pre-existing bytes"
    assert not manifest.exists()


# ── build ────────────────────────────────────────────────────────────────────


def test_unknown_table_aborts_without_publishing_target(tmp_path: Path) -> None:
    sources = legacy_snapshot_factory(tmp_path, extra_table="unmapped_private_records")
    inventory = inspect_sources(sources.contacts, sources.memory)

    for source in (inventory.contacts, inventory.memory):
        assert source.unsupported == ("unmapped_private_records",)
        assert "unmapped_private_records" in source.tables

    target, manifest = _build_paths(tmp_path)
    with pytest.raises(UnsupportedSchema) as error:
        migrate_sources(
            contacts_path=sources.contacts,
            memory_path=sources.memory,
            target=target,
            manifest=manifest,
        )
    assert "unmapped_private_records" in error.value.detail
    assert not target.exists()
    assert not manifest.exists()
    assert not target.parent.exists()


def test_manifest_lists_every_source_table(tmp_path: Path) -> None:
    sources = legacy_snapshot_factory(tmp_path)
    inventory = inspect_sources(sources.contacts, sources.memory)
    target, manifest = _build_paths(tmp_path)

    report = migrate_sources(
        contacts_path=sources.contacts,
        memory_path=sources.memory,
        target=target,
        manifest=manifest,
    )

    source_tables = set(inventory.contacts.tables) | set(inventory.memory.tables)
    reported = {name for name, _source, _imported in report.tables}
    assert source_tables == reported
    assert report.manifest_path == manifest
    assert report.target_fingerprint == _sha256(target)
    assert report.unaccounted_rows == 0
    # Nothing is silently dropped: the legacy profile rows and unproven nodes are
    # explicitly quarantined rather than promoted.
    assert report.quarantined
    assert all(
        reason in QUARANTINE_REASONS for _table, reason, _count in report.quarantined
    )
    for name, source_rows, imported_rows in report.tables:
        assert source_rows == BASE_ROWS[name]
        assert imported_rows == source_rows
        assert _scalar(target, f'SELECT COUNT(*) FROM "{name}"') == imported_rows

    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert json.loads(report.to_json()) == payload
    # The database carries the authoritative completeness marker; the manifest mirrors it.
    assert payload["migration_complete"] is True
    assert payload["semantic_digest"] == semantic_digest(target)
    assert payload["legacy_resolution"]["quarantined"]
    assert payload["tool_version"] == TOOL_VERSION
    assert payload["target_fingerprint"] == report.target_fingerprint
    assert payload["unaccounted_rows"] == 0
    assert {entry["table"] for entry in payload["tables"]} == source_tables
    ignored = {entry["table"] for entry in payload["ignored"]}
    assert source_tables.isdisjoint(ignored)
    # Ignorable objects are still listed with a reason, never silently dropped.
    assert any(entry["reason"] == "sqlite-internal" for entry in payload["ignored"])
    assert any(entry["reason"] == "fts-shadow" for entry in payload["ignored"])

    before = _stat(target)
    verification = verify_target(target=target, manifest=manifest)
    assert verification.verdict == "ok"
    assert verification.integrity_ok
    assert verification.foreign_keys_ok
    assert verification.fingerprint_ok
    assert verification.counts_match
    assert verification.mismatches == ()
    assert _stat(target) == before  # verify never writes to the target


def test_imported_primary_keys_are_unchanged(tmp_path: Path) -> None:
    sources = legacy_snapshot_factory(tmp_path)
    target, manifest = _build_paths(tmp_path)
    migrate_sources(
        contacts_path=sources.contacts,
        memory_path=sources.memory,
        target=target,
        manifest=manifest,
    )

    for sql in (
        "SELECT id FROM contacts ORDER BY id",
        "SELECT id, alias, source FROM contact_aliases ORDER BY id",
        "SELECT id, kind, value FROM contact_fields ORDER BY id",
    ):
        assert _rows(target, sql) == _rows(sources.contacts, sql)
    for sql in (
        "SELECT id, content_hash, is_deleted FROM memory2_nodes ORDER BY id",
        "SELECT fact_id FROM memory2_facts ORDER BY fact_id",
        "SELECT job_key FROM memory2_fact_jobs ORDER BY job_key",
        "SELECT id, title FROM idea_backlog_items ORDER BY id",
    ):
        assert _rows(target, sql) == _rows(sources.memory, sql)

    assert _rows(target, "SELECT id FROM contacts ORDER BY id") == [
        (OWNER_CONTACT_ID,),
        (SECOND_CONTACT_ID,),
        (THIRD_CONTACT_ID,),
    ]
    assert {row[0] for row in _rows(target, "SELECT id FROM memory2_nodes")} >= {
        ACTIVE_NODE_ID,
        LINKED_NODE_ID,
    }


def test_legacy_nodes_without_contact_id_column_are_imported(tmp_path: Path) -> None:
    sources = legacy_snapshot_factory(tmp_path, with_contact_column=False)
    columns = [row[1] for row in _rows(sources.memory, "PRAGMA table_info(memory2_nodes)")]
    assert "contact_id" not in columns

    inventory = inspect_sources(sources.contacts, sources.memory)
    assert inventory.memory.unsupported == ()

    target, manifest = _build_paths(tmp_path)
    report = migrate_sources(
        contacts_path=sources.contacts,
        memory_path=sources.memory,
        target=target,
        manifest=manifest,
    )
    assert report.unaccounted_rows == 0
    assert dict((name, imported) for name, _source, imported in report.tables)[
        "memory2_nodes"
    ] == MEMORY_TABLE_ROWS["memory2_nodes"]
    assert _rows(target, "SELECT id, is_deleted FROM memory2_nodes ORDER BY id") == _rows(
        sources.memory, "SELECT id, is_deleted FROM memory2_nodes ORDER BY id"
    )
    # Legacy profile text and unproven person columns are quarantined, never promoted.
    assert report.quarantined
    assert verify_target(target=target, manifest=manifest).verdict == "ok"


def test_constraint_violating_row_is_quarantined(tmp_path: Path) -> None:
    sources = legacy_snapshot_factory(tmp_path, broken_embedding=True)
    target, manifest = _build_paths(tmp_path)
    report = migrate_sources(
        contacts_path=sources.contacts,
        memory_path=sources.memory,
        target=target,
        manifest=manifest,
    )

    counts = {name: (source, imported) for name, source, imported in report.tables}
    assert counts["memory2_embeddings"] == (2, 1)
    assert ("memory2_embeddings", "schema-unknown", 1) in report.quarantined
    # Every reason on the "unproven" path is one the target schema check accepts.
    assert all(reason in QUARANTINE_REASONS for _table, reason, _count in report.quarantined)
    assert report.unaccounted_rows == 0
    quarantined_rows = dict(
        ((table, reason), count)
        for table, reason, count in _rows(
            target,
            "SELECT source_table, reason, COUNT(*) FROM knowledge_quarantine GROUP BY 1, 2",
        )
    )
    assert quarantined_rows[("memory2_embeddings", "schema-unknown")] == 1
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert {"count": 1, "reason": "schema-unknown", "table": "memory2_embeddings"} in (
        payload["quarantined"]
    )
    # The quarantined row left no foreign-key violation behind.
    assert verify_target(target=target, manifest=manifest).verdict == "ok"


def test_fts_entry_for_deleted_node_is_accounted(tmp_path: Path) -> None:
    sources = legacy_snapshot_factory(tmp_path, fts_entry_for_deleted_node=True)
    target, manifest = _build_paths(tmp_path)
    report = migrate_sources(
        contacts_path=sources.contacts,
        memory_path=sources.memory,
        target=target,
        manifest=manifest,
    )

    counts = {name: (source, imported) for name, source, imported in report.tables}
    assert counts["memory2_nodes_fts"] == (4, 3)
    assert ("memory2_nodes_fts", "schema-unknown", 1) in report.quarantined
    assert all(
        reason in QUARANTINE_REASONS for _table, reason, _count in report.quarantined
    )
    assert report.unaccounted_rows == 0
    indexed = {row[0] for row in _rows(target, "SELECT entry_id FROM memory2_nodes_fts")}
    assert indexed == {ACTIVE_NODE_ID, LINKED_NODE_ID, "dddddddd-4444-4444-8444-dddddddddddd"}


def test_migrate_rejects_identical_sources(tmp_path: Path) -> None:
    sources = legacy_snapshot_factory(tmp_path)
    target, manifest = _build_paths(tmp_path)
    with pytest.raises(MigrationSourceError) as error:
        migrate_sources(
            contacts_path=sources.memory,
            memory_path=sources.memory,
            target=target,
            manifest=manifest,
        )
    assert error.value.reason == "sources_identical"
    assert not target.exists()
    assert not manifest.exists()


# ── CLI ──────────────────────────────────────────────────────────────────────


def test_cli_inspect_and_build_and_verify(tmp_path: Path) -> None:
    runner = CliRunner()
    app = _cli_app()
    sources = legacy_snapshot_factory(tmp_path, with_wal=True)
    target, manifest = _build_paths(tmp_path)

    inspect_result = runner.invoke(
        app,
        [
            "knowledge",
            "migration",
            "inspect",
            "--contacts",
            str(sources.contacts),
            "--memory",
            str(sources.memory),
        ],
    )
    assert inspect_result.exit_code == 0, inspect_result.output
    assert "memory2_nodes" in inspect_result.output
    assert str(sources.memory) in inspect_result.output

    build_result = runner.invoke(
        app,
        [
            "knowledge",
            "migration",
            "build",
            "--contacts",
            str(sources.contacts),
            "--memory",
            str(sources.memory),
            "--target",
            str(target),
            "--manifest",
            str(manifest),
        ],
    )
    assert build_result.exit_code == 0, build_result.output
    assert target.exists() and manifest.exists()
    assert "unaccounted rows: 0" in build_result.output

    verify_result = runner.invoke(
        app,
        [
            "knowledge",
            "migration",
            "verify",
            "--target",
            str(target),
            "--manifest",
            str(manifest),
        ],
    )
    assert verify_result.exit_code == 0, verify_result.output
    assert "verdict: ok" in verify_result.output

    combined = inspect_result.output + build_result.output + verify_result.output
    # Redaction: only names, counts and ids, never row content.
    assert "synthetic note one" not in combined
    assert "synthetic-note-a" not in combined
    assert "Owner Synthetic" not in combined


def test_cli_verify_reports_a_tampered_target(tmp_path: Path) -> None:
    runner = CliRunner()
    app = _cli_app()
    sources = legacy_snapshot_factory(tmp_path)
    target, manifest = _build_paths(tmp_path)
    build_result = runner.invoke(
        app,
        [
            "knowledge",
            "migration",
            "build",
            "--contacts",
            str(sources.contacts),
            "--memory",
            str(sources.memory),
            "--target",
            str(target),
            "--manifest",
            str(manifest),
        ],
    )
    assert build_result.exit_code == 0, build_result.output

    connection = sqlite3.connect(target)
    try:
        connection.execute("DELETE FROM contacts WHERE id = ?", (OWNER_CONTACT_ID,))
        connection.commit()
    finally:
        connection.close()

    verify_result = runner.invoke(
        app,
        [
            "knowledge",
            "migration",
            "verify",
            "--target",
            str(target),
            "--manifest",
            str(manifest),
        ],
    )
    assert verify_result.exit_code != 0
    assert "manifest_mismatch" in verify_result.output
    assert "verdict: failed" in verify_result.output


def test_cli_failure_paths_create_nothing(tmp_path: Path) -> None:
    runner = CliRunner()
    app = _cli_app()
    sources = legacy_snapshot_factory(tmp_path)
    missing = tmp_path / "missing.db"
    target, manifest = _build_paths(tmp_path)

    def build(contacts: Path, target_path: Path) -> object:
        return runner.invoke(
            app,
            [
                "knowledge",
                "migration",
                "build",
                "--contacts",
                str(contacts),
                "--memory",
                str(sources.memory),
                "--target",
                str(target_path),
                "--manifest",
                str(manifest),
            ],
        )

    missing_result = build(missing, target)
    assert missing_result.exit_code != 0
    assert "source_error" in missing_result.output
    assert not target.exists() and not manifest.exists()

    before = _stat(sources.contacts)
    source_result = build(sources.contacts, sources.contacts)
    assert source_result.exit_code != 0
    assert "target_exists" in source_result.output
    assert not manifest.exists()
    assert _stat(sources.contacts) == before

    existing = tmp_path / "existing.db"
    existing.write_bytes(b"pre-existing bytes")
    existing_result = build(sources.contacts, existing)
    assert existing_result.exit_code != 0
    assert "target_exists" in existing_result.output
    assert existing.read_bytes() == b"pre-existing bytes"
    assert not manifest.exists()

    missing_option_result = runner.invoke(
        app,
        [
            "knowledge",
            "migration",
            "build",
            "--contacts",
            str(sources.contacts),
            "--memory",
            str(sources.memory),
        ],
    )
    assert missing_option_result.exit_code != 0
    assert not target.exists() and not manifest.exists()

    inspect_missing_result = runner.invoke(
        app,
        [
            "knowledge",
            "migration",
            "inspect",
            "--contacts",
            str(missing),
            "--memory",
            str(sources.memory),
        ],
    )
    assert inspect_missing_result.exit_code != 0
    assert "source_error" in inspect_missing_result.output
    assert not target.exists() and not manifest.exists()
