from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from yeoman_gateway.knowledge._preservation_catalog import (
    purge_collection,
    query_catalog,
    rebuild_catalog,
    refresh_collection,
)
from yeoman_gateway.knowledge._snapshot import SnapshotError, collect_sources, verify_source_bundle


def _source(source_id: str, path: Path, *, restricted: bool = False) -> dict:
    return {
        "source_id": source_id,
        "path": str(path),
        "kind": "file",
        "source_class": "synthetic",
        "restricted": restricted,
    }


def _manifest_paths(target: Path) -> list[Path]:
    return sorted((target / "versions").glob("*/manifest.json"))


def _jsonl(path: Path, *rows: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def test_new_bundle_metadata_comes_from_preserved_copy_and_contains_only_safe_fields(
    tmp_path: Path, monkeypatch
) -> None:
    from yeoman_gateway.knowledge import _preservation_inventory

    source = tmp_path / "source.jsonl"
    target = tmp_path / "archive"
    inspect = _preservation_inventory.inspect_source
    inspected: list[Path] = []

    def inspect_copy(*, path: Path, source_class: str) -> dict:
        assert path != source
        assert path.is_relative_to(target)
        inspected.append(path)
        return inspect(path=path, source_class=source_class)

    monkeypatch.setattr(_preservation_inventory, "inspect_source", inspect_copy)
    _jsonl(
        source,
        {
            "message_id": "event-a",
            "chat_id": "chat-a",
            "timestamp": "2026-10-02T12:00:00Z",
            "created_at": "2026-10-02T12:01:00Z",
            "type": "message",
            "text": "synthetic private body",
        },
    )

    report = collect_sources(sources=[_source("fixture", source)], target_dir=target)
    manifest_path = Path(report["manifest_path"])
    before_source = source.read_bytes()
    manifest = json.loads(manifest_path.read_text())
    entry = manifest["sources"][0]
    record = entry["record_metadata"]["records"][0]

    assert record == {
        "original_time": "2026-10-02T12:00:00Z",
        "creation_time": "2026-10-02T12:01:00Z",
        "chat": "chat-a",
        "record_type": "message",
        "native_id": "event-a",
        "locator": {"file": "sources/fixture/source.jsonl", "line": 1},
    }
    assert "synthetic private body" not in manifest_path.read_text()
    assert source.read_bytes() == before_source
    assert inspected


def test_native_id_uses_explicit_source_message_id_not_canonical_journal_id(tmp_path: Path) -> None:
    from yeoman_gateway.knowledge._preservation_inventory import inspect_source

    source = tmp_path / "events.jsonl"
    _jsonl(
        source,
        {"event_id": "canonical-event", "source_message_id": "native-message"},
        {"event_id": "canonical-only"},
    )

    records = inspect_source(path=source, source_class="conversation")["records"]

    assert [record["native_id"] for record in records] == ["native-message", None]


def test_refresh_is_idempotent_then_retains_changed_versions(tmp_path: Path) -> None:
    source = tmp_path / "messages.jsonl"
    _jsonl(source, {"event_id": "one", "occurred_at": "2026-10-01T00:00:00Z"})
    target = tmp_path / "collection"

    refresh_collection(sources=[_source("messages", source)], target_dir=target)
    first_manifests = _manifest_paths(target)
    second = refresh_collection(sources=[_source("messages", source)], target_dir=target)
    assert second["refreshed"] is False
    assert _manifest_paths(target) == first_manifests

    _jsonl(
        source,
        {"event_id": "one", "occurred_at": "2026-10-01T00:00:00Z"},
        {"event_id": "two", "occurred_at": "2026-10-02T00:00:00Z"},
    )
    third = refresh_collection(sources=[_source("messages", source)], target_dir=target)
    assert third["refreshed"] is True
    assert len(_manifest_paths(target)) == len(first_manifests) + 1
    assert query_catalog(target_dir=target, filters={"source_id": "messages"})


def test_vanished_source_is_source_gone_and_old_copy_remains_searchable(tmp_path: Path) -> None:
    source = tmp_path / "journal.jsonl"
    _jsonl(source, {"message_id": "kept", "occurred_at": "2026-10-01T00:00:00Z"})
    target = tmp_path / "collection"
    refresh_collection(sources=[_source("journal", source)], target_dir=target)
    source.unlink()

    report = refresh_collection(sources=[_source("journal", source)], target_dir=target)
    results = query_catalog(target_dir=target, filters={"source_id": "journal"})
    assert report["complete"] is False
    assert any(row["status"] == "source_gone" for row in results)
    assert any(row["native_id"] == "kept" for row in results)
    assert len(_manifest_paths(target)) == 2
    refresh_again = refresh_collection(sources=[_source("journal", source)], target_dir=target)
    assert refresh_again["refreshed"] is False
    assert len(_manifest_paths(target)) == 2


def test_never_present_source_stays_incomplete_until_a_copy_succeeds(tmp_path: Path) -> None:
    source = tmp_path / "not-yet-created.jsonl"
    target = tmp_path / "collection"

    missing = refresh_collection(sources=[_source("pending", source)], target_dir=target)
    result = query_catalog(target_dir=target, filters={"source_id": "pending"})
    assert missing["complete"] is False
    assert result[0]["status"] == "incomplete"
    assert len(_manifest_paths(target)) == 1

    _jsonl(source, {"message_id": "now-present", "occurred_at": "2026-10-01T00:00:00Z"})
    restored = refresh_collection(sources=[_source("pending", source)], target_dir=target)
    assert restored["complete"] is True
    assert any(row["native_id"] == "now-present" for row in query_catalog(
        target_dir=target, filters={"source_id": "pending"}
    ))


def test_catalog_rebuilds_from_manifests_without_opening_sources(tmp_path: Path) -> None:
    source = tmp_path / "source.jsonl"
    _jsonl(source, {"message_id": "from-manifest", "occurred_at": "2026-10-01T00:00:00Z"})
    target = tmp_path / "collection"
    refresh_collection(sources=[_source("fixture", source)], target_dir=target)
    source.unlink()
    (target / "catalog.sqlite3").unlink()

    report = rebuild_catalog(target_dir=target)
    results = query_catalog(target_dir=target, filters={"native_id": "from-manifest"})
    assert report["source_count"] == 1
    assert results[0]["locator"] == {"file": "sources/fixture/source.jsonl", "line": 1}


def test_query_separates_original_creation_unknown_time_and_locator_filters(tmp_path: Path) -> None:
    source = tmp_path / "messages.jsonl"
    _jsonl(
        source,
        {
            "message_id": "aware",
            "chat_id": "chat-a",
            "record_type": "message",
            "occurred_at": "2026-10-02T12:00:00+02:00",
            "stored_at": "2026-10-03T00:00:00Z",
        },
        {
            "message_id": "naive",
            "chat_id": "chat-b",
            "record_type": "reaction",
            "occurred_at": "2026-10-02T12:00:00",
            "stored_at": "2026-10-02T13:00:00Z",
        },
    )
    target = tmp_path / "collection"
    refresh_collection(sources=[_source("messages", source)], target_dir=target)

    original = query_catalog(
        target_dir=target,
        filters={
            "chat": "chat-a",
            "record_type": "message",
            "native_id": "aware",
            "original_after": "2026-10-02T09:00:00Z",
        },
    )
    creation = query_catalog(
        target_dir=target,
        filters={"creation_after": "2026-10-02T20:00:00Z", "unknown_dates": False},
    )
    unknown = query_catalog(target_dir=target, filters={"unknown_dates": True})

    assert [row["native_id"] for row in original] == ["aware"]
    assert original[0]["creation_time"] == "2026-10-03T00:00:00Z"
    assert original[0]["acquisition_finished_ms"] >= original[0]["acquisition_started_ms"]
    assert [row["native_id"] for row in creation] == ["aware"]
    assert unknown[0]["original_time"] == "2026-10-02T12:00:00"
    assert unknown[0]["locator"] == {"file": "sources/messages/messages.jsonl", "line": 2}


def test_query_uses_normalized_numeric_epoch_without_rewriting_its_raw_value(tmp_path: Path) -> None:
    source = tmp_path / "numeric.jsonl"
    _jsonl(source, {"message_id": "epoch", "occurred_at": 1_790_928_000.125})
    target = tmp_path / "collection"
    refresh_collection(sources=[_source("numeric", source)], target_dir=target)

    rows = query_catalog(
        target_dir=target,
        filters={"original_after": 1_790_928_000, "unknown_dates": False},
    )

    assert rows[0]["original_time"] == "1790928000.125"
    assert rows[0]["native_id"] == "epoch"


def test_restricted_metadata_is_structural_only_and_legacy_manifests_stay_immutable(
    tmp_path: Path,
) -> None:
    source = tmp_path / "restricted.jsonl"
    _jsonl(
        source,
        {"event_id": "secret-id", "chat_id": "private-chat", "text": "restricted body"},
    )
    target = tmp_path / "collection"
    report = collect_sources(
        sources=[_source("restricted", source, restricted=True)], target_dir=target
    )
    old_manifest = Path(report["manifest_path"])
    old_payload = json.loads(old_manifest.read_text())
    assert old_payload["sources"][0]["record_metadata"]["records"] == []
    assert "secret-id" not in old_manifest.read_text()
    assert "private-chat" not in old_manifest.read_text()
    assert "restricted body" not in old_manifest.read_text()

    # Model an immutable T2 v2 manifest that predates safe record metadata.
    entry = old_payload["sources"][0]
    entry.pop("record_metadata")
    old_manifest.write_text(json.dumps(old_payload, sort_keys=True))
    legacy_bytes = old_manifest.read_bytes()
    rebuild_catalog(target_dir=target)
    refreshed = refresh_collection(
        sources=[_source("restricted", source, restricted=True)], target_dir=target
    )
    assert refreshed["refreshed"] is True
    assert old_manifest.read_bytes() == legacy_bytes
    assert len(_manifest_paths(target)) == 2
    assert query_catalog(target_dir=target, filters={"source_id": "restricted"})
    refreshed_again = refresh_collection(
        sources=[_source("restricted", source, restricted=True)], target_dir=target
    )
    assert refreshed_again["refreshed"] is False
    assert len(_manifest_paths(target)) == 2


def test_historical_sqlite_locator_uses_manifest_main_file_and_hashes_unknown_primary_key(
    tmp_path: Path,
) -> None:
    import sqlite3

    source = tmp_path / "knowledge.db.v1-20260921-212318"
    with sqlite3.connect(source) as connection:
        connection.execute(
            "CREATE TABLE events (id TEXT PRIMARY KEY, occurred_at TEXT, body TEXT)"
        )
        connection.execute(
            "INSERT INTO events VALUES (?, ?, ?)",
            ("arbitrary-private-primary-key", "2026-10-02T12:00:00Z", "body stays private"),
        )
    target = tmp_path / "collection"
    refresh_collection(
        sources=[
            {
                "source_id": "historical",
                "path": str(source),
                "kind": "static_sqlite_triple",
                "source_class": "synthetic",
                "restricted": False,
            }
        ],
        target_dir=target,
    )

    result = query_catalog(
        target_dir=target,
        filters={"record_type": "sqlite_table", "source_id": "historical"},
    )
    manifest = json.loads(_manifest_paths(target)[0].read_text())
    entry = manifest["sources"][0]
    searchable = json.dumps(entry["record_metadata"])

    assert result[0]["locator"] == {
        "file": "sources/historical/knowledge.db.v1-20260921-212318",
        "table": "events",
    }
    assert entry["sqlite_main_file"] == source.name
    assert "arbitrary-private-primary-key" not in searchable
    assert "body stays private" not in searchable
    assert entry["record_metadata"]["records"][0]["locator"]["primary_key_sha256"]

    # A structural-only T2 manifest still resolves the historical main file,
    # and rebuilding does not need the original source.
    entry.pop("record_metadata")
    manifest_path = _manifest_paths(target)[0]
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    source.unlink()
    rebuild_catalog(target_dir=target)
    legacy = query_catalog(
        target_dir=target,
        filters={"record_type": "sqlite_table", "source_id": "historical"},
    )
    assert legacy[0]["locator"] == {
        "file": "sources/historical/knowledge.db.v1-20260921-212318",
        "table": "events",
    }


def test_restricted_sqlite_record_metadata_has_only_schema_and_counts(tmp_path: Path) -> None:
    import sqlite3

    source = tmp_path / "restricted.db"
    with sqlite3.connect(source) as connection:
        connection.execute(
            "CREATE TABLE events (id TEXT PRIMARY KEY, occurred_at TEXT, body TEXT)"
        )
        connection.execute(
            "INSERT INTO events VALUES (?, ?, ?)",
            ("private-id", "2026-10-02T12:00:00Z", "restricted row content"),
        )
    target = tmp_path / "collection"
    report = collect_sources(
        sources=[
            {
                "source_id": "restricted-db",
                "path": str(source),
                "kind": "static_sqlite_triple",
                "source_class": "synthetic",
                "restricted": True,
            }
        ],
        target_dir=target,
    )
    entry = json.loads(Path(report["manifest_path"]).read_text())["sources"][0]
    metadata = entry["record_metadata"]

    assert metadata["records"] == []
    assert metadata["tables"] == [
        {"name": "events", "columns": ["id", "occurred_at", "body"], "row_count": 1}
    ]
    rendered = json.dumps(metadata)
    assert "private-id" not in rendered
    assert "2026-10-02T12:00:00Z" not in rendered
    assert "restricted row content" not in rendered


def test_raw_reference_refresh_never_hashes_or_reads_raw_contents(tmp_path: Path, monkeypatch) -> None:
    from yeoman_gateway.knowledge import _source_bundles

    raw = tmp_path / "home/data/raw/whatsapp"
    raw.mkdir(parents=True)
    (raw / "2026-10.jsonl").write_text('{"event_id":"raw-only"}\n', encoding="utf-8")
    fingerprint = _source_bundles._fingerprint

    def forbid_raw_hash(path: Path) -> str:
        assert not path.is_relative_to(raw), "reference-only raw content was hashed"
        return fingerprint(path)

    monkeypatch.setattr(_source_bundles, "_fingerprint", forbid_raw_hash)
    report = refresh_collection(
        sources=[
            {
                "source_id": "raw-reference",
                "path": str(raw),
                "kind": "reference_only",
                "source_class": "raw_archive",
                "restricted": False,
            }
        ],
        target_dir=tmp_path / "collection",
    )
    manifest = json.loads(Path(report["manifest_path"]).read_text())
    entry = manifest["sources"][0]
    assert entry["status"] == "reference_only"
    assert entry["content_read"] is False
    assert "record_metadata" not in entry
    assert not entry.get("copied_files")


def test_purge_previews_requires_confirmation_and_tombstone_blocks_resurrection(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.jsonl"
    _jsonl(source, {"message_id": "purged", "occurred_at": "2026-10-01T00:00:00Z"})
    target = tmp_path / "collection"
    refresh_collection(sources=[_source("purge-me", source)], target_dir=target)
    first_manifest = _manifest_paths(target)[0]
    _jsonl(
        source,
        {"message_id": "purged", "occurred_at": "2026-10-01T00:00:00Z"},
        {"message_id": "new-version", "occurred_at": "2026-10-02T00:00:00Z"},
    )
    refresh_collection(sources=[_source("purge-me", source)], target_dir=target)
    manifests = _manifest_paths(target)
    assert len(manifests) == 2
    operator = str(os.getuid())
    with pytest.raises(SnapshotError, match="UID"):
        purge_collection(
            target_dir=target,
            source_id="purge-me",
            operator=str(os.getuid() + 1),
            confirmed=True,
        )

    preview = purge_collection(
        target_dir=target, source_id="purge-me", operator=operator, confirmed=False
    )
    assert preview["requires_confirmation"] is True
    assert set(preview["affected_bundles"]) == {str(path.parent) for path in manifests}
    assert first_manifest.exists()

    result = purge_collection(
        target_dir=target, source_id="purge-me", operator=operator, confirmed=True
    )
    assert result["purged"] is True
    assert all(not path.parent.exists() for path in manifests)
    assert (target / "purge-audit.jsonl").is_file()
    assert query_catalog(target_dir=target, filters={"source_id": "purge-me"}) == []
    rebuild_catalog(target_dir=target)
    assert query_catalog(target_dir=target, filters={"source_id": "purge-me"}) == []
    with pytest.raises(SnapshotError, match="purged"):
        refresh_collection(sources=[_source("purge-me", source)], target_dir=target)
    assert _manifest_paths(target) == []

    reacquire = _source("purge-me", source)
    reacquire["owner_disposition"] = "reacquire_after_purge"
    accepted = refresh_collection(sources=[reacquire], target_dir=target)
    assert accepted["complete"] is True
    assert len(_manifest_paths(target)) == 1
    assert any(
        row["native_id"] == "new-version"
        for row in query_catalog(target_dir=target, filters={"source_id": "purge-me"})
    )
    audit = [json.loads(line) for line in (target / "purge-audit.jsonl").read_text().splitlines()]
    assert [event["action"] for event in audit] == [
        "purge_started",
        "purge_completed",
        "reacquire",
    ]


def test_purging_one_source_preserves_co_resident_source_bytes_and_provenance(
    tmp_path: Path,
) -> None:
    import hashlib

    source_a = tmp_path / "a.jsonl"
    source_b = tmp_path / "b.jsonl"
    _jsonl(source_a, {"message_id": "a-old", "occurred_at": "2026-10-01T00:00:00Z"})
    _jsonl(source_b, {"message_id": "b-keep", "occurred_at": "2026-10-01T01:00:00Z"})
    target = tmp_path / "collection"
    refresh_collection(
        sources=[_source("A", source_a), _source("B", source_b)], target_dir=target
    )

    manifest = _manifest_paths(target)[0]
    original = json.loads(manifest.read_text(encoding="utf-8"))
    original_b = next(entry for entry in original["sources"] if entry["source_id"] == "B")
    b_copy = manifest.parent / "sources" / "B" / source_b.name
    b_digest = hashlib.sha256(b_copy.read_bytes()).hexdigest()
    b_query_before = query_catalog(target_dir=target, filters={"source_id": "B"})

    preview = purge_collection(
        target_dir=target, source_id="A", operator=str(os.getuid()), confirmed=False
    )
    assert preview["affected_source_ids"] == ["A"]
    assert preview["retained_source_ids"] == ["B"]
    purge_collection(target_dir=target, source_id="A", operator=str(os.getuid()), confirmed=True)

    remaining_manifests = _manifest_paths(target)
    assert remaining_manifests == [manifest]
    updated = json.loads(manifest.read_text(encoding="utf-8"))
    assert updated["sources"] == [original_b]
    assert verify_source_bundle(manifest=manifest)["verdict"] == "ok"
    assert hashlib.sha256(b_copy.read_bytes()).hexdigest() == b_digest
    assert query_catalog(target_dir=target, filters={"source_id": "A"}) == []
    assert query_catalog(target_dir=target, filters={"source_id": "B"}) == b_query_before


def test_failed_purge_stays_private_pending_and_old_versions_never_return(
    tmp_path: Path, monkeypatch
) -> None:
    from yeoman_gateway.knowledge import _preservation_catalog

    source = tmp_path / "a.jsonl"
    _jsonl(source, {"message_id": "old-id", "occurred_at": "2026-10-01T00:00:00Z"})
    target = tmp_path / "collection"
    refresh_collection(sources=[_source("A", source)], target_dir=target)
    old_versions = {path.parent.name for path in _manifest_paths(target)}

    _jsonl(
        source,
        {"message_id": "old-id", "occurred_at": "2026-10-01T00:00:00Z"},
        {"message_id": "new-id", "occurred_at": "2026-10-02T00:00:00Z"},
    )
    refresh_collection(sources=[_source("A", source)], target_dir=target)
    old_versions.update(path.parent.name for path in _manifest_paths(target))
    assert len(old_versions) == 2

    remove_tree = _preservation_catalog._remove_tree

    def fail_bundle_cleanup(path: Path) -> None:
        if path.parent.name == "versions" or path.name == "A":
            raise OSError("synthetic purge interruption")
        remove_tree(path)

    monkeypatch.setattr(_preservation_catalog, "_remove_tree", fail_bundle_cleanup)
    with pytest.raises(SnapshotError, match="incomplete"):
        purge_collection(target_dir=target, source_id="A", operator=str(os.getuid()), confirmed=True)

    failed_audit = [
        json.loads(line)
        for line in (target / "purge-audit.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert failed_audit[0]["action"] == "purge_started"
    assert set(failed_audit[0]["versions"]) == old_versions
    # The audit is already durable, so stale catalog rows cannot leak during failure.
    assert query_catalog(target_dir=target, filters={"source_id": "A"}) == []
    rebuild_catalog(target_dir=target)
    assert query_catalog(target_dir=target, filters={"source_id": "A"}) == []
    with pytest.raises(SnapshotError, match="pending"):
        refresh_collection(
            sources=[{**_source("A", source), "owner_disposition": "reacquire_after_purge"}],
            target_dir=target,
        )
    with pytest.raises(SnapshotError, match="pending"):
        collect_sources(sources=[_source("A", source)], target_dir=target)

    monkeypatch.setattr(_preservation_catalog, "_remove_tree", remove_tree)
    retried = purge_collection(
        target_dir=target, source_id="A", operator=str(os.getuid()), confirmed=True
    )
    assert retried["purged"] is True
    assert query_catalog(target_dir=target, filters={"source_id": "A"}) == []
    with pytest.raises(SnapshotError, match="purged"):
        collect_sources(sources=[_source("A", source)], target_dir=target)

    # A new authorized acquisition has changed source bytes and cannot revive archived IDs.
    _jsonl(source, {"message_id": "new-after-purge", "occurred_at": "2026-10-03T00:00:00Z"})
    reacquired = refresh_collection(
        sources=[{**_source("A", source), "owner_disposition": "reacquire_after_purge"}],
        target_dir=target,
    )
    assert reacquired["complete"] is True
    rebuild_catalog(target_dir=target)
    rows = query_catalog(target_dir=target, filters={"source_id": "A"})
    native_ids = [row["native_id"] for row in rows if row["native_id"] is not None]
    assert native_ids == ["new-after-purge"]
    assert not (old_versions & {row["version_id"] for row in rows})


def test_snapshot_cli_exposes_catalog_operations(tmp_path: Path) -> None:
    from typer.testing import CliRunner
    from yeoman_gateway.cli.knowledge_commands import knowledge_app

    runner = CliRunner()
    for command, options in (
        ("refresh", ("--sources", "--target-dir")),
        (
            "query",
            (
                "--target-dir", "--source-id", "--chat", "--record-type", "--native-id",
                "--original-after", "--original-before", "--creation-after", "--creation-before",
                "--unknown-dates",
            ),
        ),
        ("rebuild", ("--target-dir",)),
        ("purge", ("--target-dir", "--source-id", "--yes")),
    ):
        result = runner.invoke(knowledge_app, ["snapshot", command, "--help"])
        assert result.exit_code == 0, result.output
        assert all(option in result.output for option in options)


def test_snapshot_cli_runs_refresh_query_rebuild_and_confirmed_purge(tmp_path: Path) -> None:
    from typer.testing import CliRunner
    from yeoman_gateway.cli.knowledge_commands import knowledge_app

    source = tmp_path / "cli.jsonl"
    _jsonl(source, {"message_id": "cli-event", "occurred_at": "2026-10-02T12:00:00Z"})
    descriptors = tmp_path / "sources.json"
    descriptors.write_text(json.dumps([_source("cli-source", source)]), encoding="utf-8")
    target = tmp_path / "collection"
    runner = CliRunner()

    refresh = runner.invoke(
        knowledge_app,
        ["snapshot", "refresh", "--sources", str(descriptors), "--target-dir", str(target)],
    )
    assert refresh.exit_code == 0, refresh.output
    query = runner.invoke(
        knowledge_app,
        ["snapshot", "query", "--target-dir", str(target), "--source-id", "cli-source"],
    )
    assert query.exit_code == 0, query.output
    assert "cli-event" in query.output
    rebuild = runner.invoke(knowledge_app, ["snapshot", "rebuild", "--target-dir", str(target)])
    assert rebuild.exit_code == 0, rebuild.output
    purge = runner.invoke(
        knowledge_app,
        ["snapshot", "purge", "--target-dir", str(target), "--source-id", "cli-source", "--yes"],
    )
    assert purge.exit_code == 0, purge.output
    assert _manifest_paths(target) == []
