"""Lossless bundle collection against synthetic sources only."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest
from yeoman_gateway.knowledge._snapshot import (
    SnapshotError,
    collect_sources,
    verify_source_bundle,
)


def _source(source_id: str, path: Path, kind: str = "file", **overrides) -> dict:
    return {
        "source_id": source_id,
        "path": str(path),
        "kind": kind,
        "source_class": "synthetic",
        "restricted": False,
        **overrides,
    }


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifest(root: Path) -> dict:
    paths = sorted((root / "versions").glob("*/manifest.json"))
    return json.loads(paths[-1].read_text())


def test_live_sqlite_backup_captures_committed_wal_rows_without_changing_source(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.db"
    connection = sqlite3.connect(source)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, body BLOB)")
    connection.execute("INSERT INTO events(body) VALUES (?)", (sqlite3.Binary(b"\x00\xff"),))
    connection.commit()
    assert Path(f"{source}-wal").exists()
    source_before = {
        p.name: (_hash(p), p.stat().st_mtime_ns) for p in tmp_path.glob("source.db*")
    }
    before = {
        p.name: (_hash(p), p.stat().st_mtime_ns)
        for p in (source, Path(f"{source}-wal"))
    }

    report = collect_sources(
        sources=[_source("journal", source, "live_sqlite")], target_dir=tmp_path / "out"
    )
    result = verify_source_bundle(
        manifest=Path(report["manifest_path"]), restore_dir=tmp_path / "restore"
    )
    # SQLite can update transient read marks in -shm while opening a read-only WAL
    # connection; the database and committed WAL bytes remain unchanged.
    assert before == {
        p.name: (_hash(p), p.stat().st_mtime_ns)
        for p in (source, Path(f"{source}-wal"))
    }
    connection.close()

    entry = _manifest(tmp_path / "out")["sources"][0]
    assert entry["status"] == "copied"
    assert entry["consistency"] == "sqlite_online_backup"
    assert entry["sqlite"]["counts"]["events"] == 1
    assert {
        name: (info["sha256"], info["mtime_ns"])
        for name, info in entry["source_components"].items()
    } == source_before
    restored = Path(result["restored_sources"]["journal"]) / "data.db"
    db = sqlite3.connect(f"file:{restored}?mode=ro", uri=True)
    try:
        assert db.execute("SELECT body FROM events").fetchone()[0] == b"\x00\xff"
    finally:
        db.close()


def test_static_sqlite_triple_is_byte_copied_before_inspection(tmp_path: Path) -> None:
    source = tmp_path / "frozen.db"
    connection = sqlite3.connect(source)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.execute("CREATE TABLE unknown_payload (opaque BLOB, extra TEXT)")
    connection.execute("INSERT INTO unknown_payload VALUES (?, ?)", (b"\x00\xffsecret", "v"))
    connection.commit()
    companions = [Path(f"{source}-wal"), Path(f"{source}-shm")]
    assert all(path.exists() for path in companions)
    files = [source, *companions]
    before = {p.name: (_hash(p), p.stat().st_mtime_ns) for p in files}

    report = collect_sources(
        sources=[_source("frozen", source, "static_sqlite_triple")], target_dir=tmp_path / "out"
    )

    entry = _manifest(tmp_path / "out")["sources"][0]
    copied = Path(report["bundle_dir"]) / "sources" / "frozen"
    assert set(entry["copied_files"]) == {"frozen.db", "frozen.db-wal", "frozen.db-shm"}
    assert all((copied / p.name).read_bytes() == p.read_bytes() for p in files)
    assert before == {p.name: (_hash(p), p.stat().st_mtime_ns) for p in files}
    assert {
        name: info["sha256"] for name, info in entry["source_components"].items()
    } == {p.name: _hash(p) for p in files}
    assert entry["sqlite"]["schemas"]["unknown_payload"] == ["opaque", "extra"]
    assert all(
        p.stat().st_mode & 0o777 == (0o700 if p.is_dir() else 0o600)
        for p in Path(report["bundle_dir"]).rglob("*")
    )
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in copied.iterdir() if p.is_file())


def test_unknown_database_values_and_malformed_jsonl_are_preserved_verbatim(
    tmp_path: Path,
) -> None:
    source = tmp_path / "opaque.db"
    db = sqlite3.connect(source)
    db.execute("CREATE TABLE unexpected (new_column BLOB)")
    db.execute("INSERT INTO unexpected VALUES (?)", (sqlite3.Binary(b"\x00\xff"),))
    db.commit()
    db.close()
    jsonl = tmp_path / "events.jsonl"
    jsonl.write_bytes(b'{"ok":1}\nnot json\n')

    report = collect_sources(
        sources=[_source("db", source, "static_sqlite_triple"), _source("lines", jsonl)],
        target_dir=tmp_path / "out",
    )
    verified = verify_source_bundle(manifest=Path(report["manifest_path"]))
    restored = verify_source_bundle(
        manifest=Path(report["manifest_path"]), restore_dir=tmp_path / "restore"
    )
    manifest = _manifest(tmp_path / "out")
    entries = {entry["source_id"]: entry for entry in manifest["sources"]}

    assert verified["verdict"] == "ok"
    assert entries["db"]["sqlite"]["schemas"]["unexpected"] == ["new_column"]
    assert entries["lines"]["file_stats"] == {"json_lines": 2, "malformed_json_lines": 1}
    assert (Path(report["bundle_dir"]) / "sources/lines/events.jsonl").read_bytes() == jsonl.read_bytes()
    restored_db = Path(restored["restored_sources"]["db"]) / source.name
    connection = sqlite3.connect(f"file:{restored_db}?mode=ro", uri=True)
    try:
        assert connection.execute("SELECT new_column FROM unexpected").fetchone()[0] == b"\x00\xff"
    finally:
        connection.close()


def test_duplicate_ids_reject_and_interrupted_collection_resumes_completed_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first.bin"
    second = tmp_path / "second.bin"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    with pytest.raises(SnapshotError, match="duplicate"):
        collect_sources(
            sources=[_source("same", first), _source("same", second)], target_dir=tmp_path / "dup"
        )

    import yeoman_gateway.knowledge._source_bundles as bundles

    original = bundles._copy_file
    calls: list[str] = []

    def interrupt_second(source: Path, target: Path) -> str:
        calls.append(source.name)
        if source == second:
            raise KeyboardInterrupt
        return original(source, target)

    monkeypatch.setattr(bundles, "_copy_file", interrupt_second)
    descriptors = [_source("a", first), _source("b", second)]
    with pytest.raises(KeyboardInterrupt):
        collect_sources(sources=descriptors, target_dir=tmp_path / "resume")
    assert calls == ["first.bin", "second.bin"]

    resumed_calls: list[str] = []

    def record_resume(source: Path, target: Path) -> str:
        resumed_calls.append(source.name)
        return original(source, target)

    monkeypatch.setattr(bundles, "_copy_file", record_resume)
    report = collect_sources(sources=descriptors, target_dir=tmp_path / "resume")
    assert (Path(report["bundle_dir"]) / "sources/a/first.bin").read_bytes() == b"first"
    assert (Path(report["bundle_dir"]) / "sources/b/second.bin").read_bytes() == b"second"
    assert calls == ["first.bin", "second.bin"]
    assert resumed_calls == ["second.bin"]


def test_resume_reacquires_a_completed_source_that_changed(tmp_path: Path, monkeypatch) -> None:
    import yeoman_gateway.knowledge._source_bundles as bundles

    first = tmp_path / "first.bin"
    second = tmp_path / "second.bin"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    original = bundles._copy_file

    descriptors = [_source("a", first), _source("b", second)]
    calls = 0

    def interrupt_second(source: Path, target: Path) -> str:
        nonlocal calls
        if source == second:
            calls += 1
            raise KeyboardInterrupt
        return original(source, target)

    monkeypatch.setattr(bundles, "_copy_file", interrupt_second)
    with pytest.raises(KeyboardInterrupt):
        collect_sources(sources=descriptors, target_dir=tmp_path / "resume")
    original_stat = first.stat()
    first.write_bytes(b"other")
    os.utime(first, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    monkeypatch.setattr(bundles, "_copy_file", original)
    report = collect_sources(sources=descriptors, target_dir=tmp_path / "resume")
    copied = Path(report["bundle_dir"]) / "sources/a/first.bin"
    assert copied.read_bytes() == b"other"
    assert calls == 1


def test_missing_and_unreadable_sources_are_recorded_incomplete(tmp_path: Path) -> None:
    missing = tmp_path / "missing.bin"
    unreadable = tmp_path / "unreadable.bin"
    unreadable.write_bytes(b"denied")
    unreadable.chmod(0)
    try:
        report = collect_sources(
            sources=[_source("missing", missing), _source("denied", unreadable)],
            target_dir=tmp_path / "out",
        )
    finally:
        unreadable.chmod(0o600)
    entries = {item["source_id"]: item for item in _manifest(tmp_path / "out")["sources"]}
    assert report["complete"] is False
    assert entries["missing"]["status"] == "incomplete"
    assert entries["denied"]["status"] == "incomplete"
    assert (tmp_path / "out").stat().st_mode & 0o777 == 0o700
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in (tmp_path / "out").rglob("*") if p.is_file())


def test_raw_is_reference_only_and_collection_rejects_overlap_symlinks_and_traversal(
    tmp_path: Path,
) -> None:
    raw = tmp_path / "data" / "raw"
    raw.mkdir(parents=True)
    raw_file = raw / "2026.jsonl"
    raw_file.write_text("native secret")
    report = collect_sources(
        sources=[_source("raw", raw_file, source_class="raw")], target_dir=tmp_path / "collection"
    )
    entry = _manifest(tmp_path / "collection")["sources"][0]
    assert entry["status"] == "reference_only"
    assert "native secret" not in json.dumps(_manifest(tmp_path / "collection"))
    assert not (Path(report["bundle_dir"]) / "sources/raw").exists()

    with pytest.raises(SnapshotError):
        collect_sources(sources=[_source("nested", raw)], target_dir=raw / "nested-output")
    with pytest.raises(SnapshotError):
        collect_sources(sources=[_source("inside", raw_file)], target_dir=tmp_path)
    link = tmp_path / "linked"
    link.symlink_to(raw_file)
    with pytest.raises(SnapshotError):
        collect_sources(sources=[_source("link", link)], target_dir=tmp_path / "link-out")
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "linked-dir").symlink_to(raw, target_is_directory=True)
    with pytest.raises(SnapshotError):
        collect_sources(sources=[_source("tree", tree, "tree")], target_dir=tmp_path / "tree-out")
    with pytest.raises(SnapshotError):
        collect_sources(sources=[_source("../escape", raw_file)], target_dir=tmp_path / "escape-out")
    credentials = tmp_path / "secrets" / "token.json"
    credentials.parent.mkdir()
    credentials.write_text("credential")
    excluded = collect_sources(
        sources=[_source("credential", credentials)], target_dir=tmp_path / "safe-out"
    )
    assert _manifest(tmp_path / "safe-out")["sources"][0]["status"] == "excluded"
    assert "token.json" not in json.dumps(_manifest(tmp_path / "safe-out"))
    assert excluded["complete"] is True


def test_collection_cli_requires_explicit_sources_and_preserves_v1_commands(tmp_path: Path) -> None:
    from typer.testing import CliRunner
    from yeoman_gateway.cli.knowledge_commands import knowledge_app

    runner = CliRunner()
    help_result = runner.invoke(knowledge_app, ["snapshot", "collect", "--help"])
    assert help_result.exit_code == 0, help_result.output
    assert "--sources" in help_result.output and "--target-dir" in help_result.output
    verify_help = runner.invoke(knowledge_app, ["snapshot", "verify-bundle", "--help"])
    restore_help = runner.invoke(knowledge_app, ["snapshot", "restore", "--help"])
    assert verify_help.exit_code == 0 and "--manifest" in verify_help.output
    assert "--restore-dir" in verify_help.output
    assert restore_help.exit_code == 0 and "--manifest" in restore_help.output
    assert "--restore-dir" in restore_help.output
    sources = tmp_path / "sources.json"
    source = tmp_path / "synthetic.jsonl"
    source.write_bytes(b"{\"synthetic\":true}\n")
    sources.write_text(json.dumps([_source("fixture", source)]))
    result = runner.invoke(
        knowledge_app,
        ["snapshot", "collect", "--sources", str(sources), "--target-dir", str(tmp_path / "out")],
    )
    assert result.exit_code == 0, result.output
    assert "complete: yes" in result.output
    manifest = sorted((tmp_path / "out" / "versions").glob("*/manifest.json"))[-1]
    verified = runner.invoke(
        knowledge_app, ["snapshot", "verify-bundle", "--manifest", str(manifest)]
    )
    assert verified.exit_code == 0 and "bundle verdict: ok" in verified.output
    restored = runner.invoke(
        knowledge_app,
        [
            "snapshot",
            "restore",
            "--manifest",
            str(manifest),
            "--restore-dir",
            str(tmp_path / "restored"),
        ],
    )
    assert restored.exit_code == 0 and "bundle verdict: ok" in restored.output
    assert runner.invoke(knowledge_app, ["snapshot", "verify"]).exit_code == 2


def test_capacity_is_checked_before_creating_the_collection(tmp_path: Path, monkeypatch) -> None:
    from types import SimpleNamespace

    import yeoman_gateway.knowledge._source_bundles as bundles

    source = tmp_path / "large.bin"
    source.write_bytes(b"more than ten bytes")
    monkeypatch.setattr(bundles.shutil, "disk_usage", lambda _path: SimpleNamespace(free=10))
    target = tmp_path / "no-space"
    with pytest.raises(SnapshotError) as error:
        collect_sources(sources=[_source("large", source)], target_dir=target)
    assert error.value.code == "capacity_insufficient"
    assert not target.exists()


def test_verification_rejects_changed_and_unlisted_bundle_files(tmp_path: Path) -> None:
    source = tmp_path / "source.jsonl"
    source.write_bytes(b"{\"row\":1}\n")
    report = collect_sources(
        sources=[_source("source", source)], target_dir=tmp_path / "out"
    )
    bundle_source = Path(report["bundle_dir"]) / "sources/source/source.jsonl"
    bundle_source.write_bytes(b"tampered")
    changed = verify_source_bundle(manifest=Path(report["manifest_path"]))
    assert changed["verdict"] == "failed"
    assert "bundle_hash_mismatch" in changed["errors"]

    bundle_source.write_bytes(source.read_bytes())
    (bundle_source.parent / "unlisted.bin").write_bytes(b"extra")
    extra = verify_source_bundle(manifest=Path(report["manifest_path"]))
    assert extra["verdict"] == "failed"
    assert "bundle_layout_mismatch" in extra["errors"]


def test_each_collection_keeps_a_distinct_immutable_version(tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"first variant")
    descriptor = _source("variant", source)
    first = collect_sources(sources=[descriptor], target_dir=tmp_path / "out")
    source.write_bytes(b"second variant")
    second = collect_sources(sources=[descriptor], target_dir=tmp_path / "out")
    old = Path(first["bundle_dir"]) / "sources/variant/source.bin"
    new = Path(second["bundle_dir"]) / "sources/variant/source.bin"
    assert first["bundle_dir"] != second["bundle_dir"]
    assert old.read_bytes() == b"first variant"
    assert new.read_bytes() == b"second variant"


def test_backup_handles_uri_special_characters_without_touching_the_neighbor(tmp_path: Path) -> None:
    from yeoman_gateway.knowledge._snapshot import _backup

    source = tmp_path / "source # not-a-uri?x.db"
    neighbor = tmp_path / "source "
    db = sqlite3.connect(source)
    db.execute("CREATE TABLE marker (value TEXT)")
    db.execute("INSERT INTO marker VALUES ('correct')")
    db.commit()
    db.close()
    neighbor.write_text("not a database")

    _backup(source, tmp_path / "copy.db")
    copied = sqlite3.connect(f"file:{tmp_path / 'copy.db'}?mode=ro", uri=True)
    try:
        assert copied.execute("SELECT value FROM marker").fetchone()[0] == "correct"
    finally:
        copied.close()
    assert neighbor.read_text() == "not a database"


def test_static_sqlite_rejects_dangling_sidecar_symlink(tmp_path: Path) -> None:
    source = tmp_path / "static.db"
    db = sqlite3.connect(source)
    db.execute("CREATE TABLE rows (value TEXT)")
    db.commit()
    db.close()
    Path(f"{source}-wal").symlink_to(tmp_path / "missing-wal")

    with pytest.raises(SnapshotError, match="symlink"):
        collect_sources(
            sources=[_source("static", source, "static_sqlite_triple")],
            target_dir=tmp_path / "out",
        )


def test_failed_restore_leaves_destination_empty(tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"preserved")
    report = collect_sources(
        sources=[_source("source", source)], target_dir=tmp_path / "out"
    )
    manifest = Path(report["manifest_path"])
    payload = json.loads(manifest.read_text())
    payload["complete"] = False
    manifest.write_text(json.dumps(payload))
    restore = tmp_path / "restore"

    result = verify_source_bundle(manifest=manifest, restore_dir=restore)

    assert result["verdict"] == "failed"
    assert not list(restore.iterdir())


def test_historical_static_sqlite_name_verifies_and_restores(tmp_path: Path) -> None:
    source = tmp_path / "knowledge.db.v1-20260921-212318"
    db = sqlite3.connect(source)
    db.execute("CREATE TABLE preserved (payload BLOB)")
    db.execute("INSERT INTO preserved VALUES (?)", (sqlite3.Binary(b"\x00\xff"),))
    db.commit()
    db.close()

    report = collect_sources(
        sources=[_source("history", source, "static_sqlite_triple")],
        target_dir=tmp_path / "collection",
    )
    verified = verify_source_bundle(
        manifest=Path(report["manifest_path"]), restore_dir=tmp_path / "restore"
    )

    assert verified["verdict"] == "ok"
    manifest = json.loads(Path(report["manifest_path"]).read_text())
    assert manifest["sources"][0]["sqlite_main_file"] == source.name
    restored = Path(verified["restored_sources"]["history"]) / source.name
    db = sqlite3.connect(f"{restored.resolve().as_uri()}?mode=ro", uri=True)
    try:
        assert db.execute("SELECT payload FROM preserved").fetchone()[0] == b"\x00\xff"
    finally:
        db.close()


@pytest.mark.parametrize("protected", ["raw", "raw-spool"])
def test_tree_containing_nested_raw_or_spool_is_incomplete_without_copying_content(
    tmp_path: Path, protected: str
) -> None:
    source = tmp_path / "historical"
    folder = source / "data" / protected
    folder.mkdir(parents=True)
    (folder / "event.jsonl").write_bytes(b'{"private":"synthetic raw fixture"}\n')

    report = collect_sources(
        sources=[_source("tree", source, "tree")], target_dir=tmp_path / "collection"
    )

    manifest = json.loads(Path(report["manifest_path"]).read_text())
    entry = manifest["sources"][0]
    assert report["complete"] is False
    assert entry["status"] == "incomplete"
    assert entry["reason_code"] == "raw_path_requires_reference"
    assert "synthetic raw fixture" not in Path(report["manifest_path"]).read_text()
    assert not (Path(report["bundle_dir"]) / "sources/tree").exists()


def test_collection_and_restore_reject_protected_raw_destinations(tmp_path: Path) -> None:
    source = tmp_path / "ordinary.bin"
    source.write_bytes(b"ordinary synthetic bytes")
    report = collect_sources(
        sources=[_source("ordinary", source)], target_dir=tmp_path / "collection"
    )
    manifest = Path(report["manifest_path"])

    for protected in ("raw", "raw-spool"):
        collection_target = tmp_path / "data" / protected / "collection"
        with pytest.raises(SnapshotError, match="protected raw"):
            collect_sources(
                sources=[_source("ordinary", source)], target_dir=collection_target
            )
        assert not collection_target.exists()
        assert not (tmp_path / "data").exists()

        restore_target = tmp_path / "data" / protected / "restore"
        with pytest.raises(SnapshotError, match="protected raw"):
            verify_source_bundle(manifest=manifest, restore_dir=restore_target)
        assert not restore_target.exists()
        assert not (tmp_path / "data").exists()

    raw_root = tmp_path / "data" / "raw"
    raw_root.mkdir(parents=True)
    alias = tmp_path / "raw-link"
    alias.symlink_to(raw_root, target_is_directory=True)
    with pytest.raises(SnapshotError):
        collect_sources(
            sources=[_source("ordinary", source)], target_dir=alias / "collection"
        )
    with pytest.raises(SnapshotError):
        verify_source_bundle(manifest=manifest, restore_dir=alias / "restore")
    assert not (raw_root / "collection").exists()
    assert not (raw_root / "restore").exists()

    traversal = raw_root / ".." / "raw" / "traversal"
    with pytest.raises(SnapshotError):
        collect_sources(sources=[_source("ordinary", source)], target_dir=traversal)
    with pytest.raises(SnapshotError):
        verify_source_bundle(manifest=manifest, restore_dir=traversal)
    assert not (raw_root / "traversal").exists()


def test_new_nested_collection_directories_are_private(tmp_path: Path) -> None:
    source = tmp_path / "source-tree"
    (source / "a" / "b").mkdir(parents=True)
    (source / "a" / "b" / "file.bin").write_bytes(b"synthetic")

    report = collect_sources(
        sources=[_source("tree", source, "tree")],
        target_dir=tmp_path / "new-parent" / "collection",
    )

    new_parent = tmp_path / "new-parent"
    assert new_parent.stat().st_mode & 0o777 == 0o700
    for directory in (
        new_parent / "collection",
        Path(report["bundle_dir"]) / "sources/tree/a",
        Path(report["bundle_dir"]) / "sources/tree/a/b",
    ):
        assert directory.stat().st_mode & 0o777 == 0o700
    restore_report = verify_source_bundle(
        manifest=Path(report["manifest_path"]),
        restore_dir=tmp_path / "restore-parent" / "restore",
    )
    restore_root = tmp_path / "restore-parent" / "restore"
    assert restore_report["verdict"] == "ok"
    assert (tmp_path / "restore-parent").stat().st_mode & 0o777 == 0o700
    assert all(
        (restore_root / relative).stat().st_mode & 0o777 == 0o700
        for relative in (
            "sources",
            "sources/tree",
            "sources/tree/a",
            "sources/tree/a/b",
        )
    )
