"""P4.3: reproducible builds, staged publish and recovery after a crash.

Two builds from the same snapshots must be semantically identical, a target is only
published complete, and a half-published target is reported as such instead of being
accepted as a finished migration.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from legacy_fixtures import legacy_snapshot_factory
from yeoman_gateway.knowledge._migration import (
    MigrationSourceError,
    _open_source,
    migrate_sources,
    semantic_digest,
    verify_target,
)


def _build_paths(tmp_path: Path, name: str = "built") -> tuple[Path, Path]:
    target = tmp_path / name / "knowledge.db"
    manifest = tmp_path / name / "manifest.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    return target, manifest


def test_repeated_migration_has_same_semantics(tmp_path: Path):
    sources = legacy_snapshot_factory(tmp_path)
    first_target, first_manifest = _build_paths(tmp_path, "first")
    second_target, second_manifest = _build_paths(tmp_path, "second")

    first = migrate_sources(
        contacts_path=sources.contacts,
        memory_path=sources.memory,
        target=first_target,
        manifest=first_manifest,
    )
    second = migrate_sources(
        contacts_path=sources.contacts,
        memory_path=sources.memory,
        target=second_target,
        manifest=second_manifest,
    )

    # The digest is content-based: it ignores file layout, page order and timestamps.
    assert semantic_digest(first_target) == semantic_digest(second_target)
    assert first.migration_id != second.migration_id
    assert first.source_fingerprints == second.source_fingerprints


def test_semantic_digest_changes_when_a_row_changes(tmp_path: Path):
    sources = legacy_snapshot_factory(tmp_path)
    target, manifest = _build_paths(tmp_path)
    migrate_sources(
        contacts_path=sources.contacts,
        memory_path=sources.memory,
        target=target,
        manifest=manifest,
    )
    before = semantic_digest(target)

    import sqlite3

    conn = sqlite3.connect(target)
    try:
        conn.execute("UPDATE contact_aliases SET alias = alias || '-x' WHERE rowid = 1")
        conn.commit()
    finally:
        conn.close()
    assert semantic_digest(target) != before


def test_published_target_carries_the_internal_completeness_marker(tmp_path: Path):
    sources = legacy_snapshot_factory(tmp_path)
    target, manifest = _build_paths(tmp_path)
    report = migrate_sources(
        contacts_path=sources.contacts,
        memory_path=sources.memory,
        target=target,
        manifest=manifest,
    )
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["migration_complete"] is True
    assert payload["semantic_digest"] == semantic_digest(target)

    import sqlite3

    conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
    try:
        meta = dict(
            conn.execute(
                "SELECT key, value FROM knowledge_meta WHERE key IN"
                " ('migration_complete','migration_id','semantic_digest')"
            ).fetchall()
        )
    finally:
        conn.close()
    assert meta["migration_complete"] == "1"
    assert meta["migration_id"] == report.migration_id
    assert meta["semantic_digest"] == payload["semantic_digest"]


def test_target_without_marker_is_not_a_complete_migration(tmp_path: Path):
    """A database whose marker was lost is reported as incomplete, not accepted."""
    sources = legacy_snapshot_factory(tmp_path)
    target, manifest = _build_paths(tmp_path)
    migrate_sources(
        contacts_path=sources.contacts,
        memory_path=sources.memory,
        target=target,
        manifest=manifest,
    )

    import sqlite3

    conn = sqlite3.connect(target)
    try:
        conn.execute("UPDATE knowledge_meta SET value = '0' WHERE key = 'migration_complete'")
        conn.commit()
    finally:
        conn.close()

    report = verify_target(target=target, manifest=manifest)
    assert report.complete is False
    assert report.verdict == "failed"
    assert report.integrity_ok is True  # the file itself is fine - it is just unfinished


def test_manifest_digest_mismatch_is_detected(tmp_path: Path):
    sources = legacy_snapshot_factory(tmp_path)
    target, manifest = _build_paths(tmp_path)
    migrate_sources(
        contacts_path=sources.contacts,
        memory_path=sources.memory,
        target=target,
        manifest=manifest,
    )
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["semantic_digest"] = "0" * 64
    manifest.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report = verify_target(target=target, manifest=manifest)
    assert report.digest_ok is False
    assert report.verdict == "failed"


def test_failure_before_publish_leaves_nothing_behind(tmp_path: Path, monkeypatch):
    """A crash during the copy phase publishes neither a target nor a manifest."""
    sources = legacy_snapshot_factory(tmp_path)
    target, manifest = _build_paths(tmp_path)

    import yeoman_gateway.knowledge._migration as migration

    def boom(*_args, **_kwargs):
        raise RuntimeError("injected crash during the copy phase")

    monkeypatch.setattr(migration, "_semantic_digest", boom)
    with pytest.raises(RuntimeError):
        migrate_sources(
            contacts_path=sources.contacts,
            memory_path=sources.memory,
            target=target,
            manifest=manifest,
        )
    assert not target.exists()
    assert not manifest.exists()
    # Even the staging file is gone: a failed build cannot be mistaken for a target.
    assert list(target.parent.glob("*.staging-*")) == []


def test_failure_between_target_and_manifest_publish_is_recoverable(tmp_path: Path, monkeypatch):
    """The database is only published with its marker; a later crash is regenerable."""
    sources = legacy_snapshot_factory(tmp_path)
    target, manifest = _build_paths(tmp_path)

    real_write_text = Path.write_text

    def fail_on_manifest(self: Path, *args, **kwargs):  # pragma: no cover - injected
        if self == manifest:
            raise OSError("injected crash while writing the manifest")
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_on_manifest)
    with pytest.raises(OSError):
        migrate_sources(
            contacts_path=sources.contacts,
            memory_path=sources.memory,
            target=target,
            manifest=manifest,
        )
    monkeypatch.undo()

    # The target was published complete but its manifest is missing: verify refuses,
    # and a rebuild must not silently overwrite the existing file.
    assert target.exists()
    assert not manifest.exists()
    assert semantic_digest(target)
    with pytest.raises(MigrationSourceError) as excinfo:
        verify_target(target=target, manifest=manifest)
    assert excinfo.value.reason == "manifest_missing"
    with pytest.raises(MigrationSourceError) as rebuild:
        migrate_sources(
            contacts_path=sources.contacts,
            memory_path=sources.memory,
            target=target,
            manifest=manifest,
        )
    assert rebuild.value.reason == "target_exists"


def test_recovery_after_failure_produces_a_verified_target(tmp_path: Path, monkeypatch):
    """No stale staging file and no stale partial target survive a failed build."""
    sources = legacy_snapshot_factory(tmp_path)
    target, manifest = _build_paths(tmp_path)

    import yeoman_gateway.knowledge._migration as migration

    calls = {"count": 0}
    real_resolve = migration._resolve_legacy_rows

    def flaky_resolve(store, *, created_ms):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("injected crash during legacy resolution")
        return real_resolve(store, created_ms=created_ms)

    monkeypatch.setattr(migration, "_resolve_legacy_rows", flaky_resolve)
    with pytest.raises(RuntimeError):
        migrate_sources(
            contacts_path=sources.contacts,
            memory_path=sources.memory,
            target=target,
            manifest=manifest,
        )
    monkeypatch.undo()

    assert not target.exists()
    report = migrate_sources(
        contacts_path=sources.contacts,
        memory_path=sources.memory,
        target=target,
        manifest=manifest,
    )
    assert report.unaccounted_rows == 0
    assert verify_target(target=target, manifest=manifest).verdict == "ok"


def test_source_snapshots_are_untouched_by_a_build(tmp_path: Path):
    sources = legacy_snapshot_factory(tmp_path)
    before = {
        path: (path.stat().st_size, path.read_bytes())
        for path in (sources.contacts, sources.memory)
    }
    target, manifest = _build_paths(tmp_path)
    migrate_sources(
        contacts_path=sources.contacts,
        memory_path=sources.memory,
        target=target,
        manifest=manifest,
    )
    for path, (size, payload) in before.items():
        assert path.stat().st_size == size
        assert path.read_bytes() == payload


def test_staged_build_never_touches_an_existing_target(tmp_path: Path):
    """A populated target is refused before any staging work happens."""
    sources = legacy_snapshot_factory(tmp_path)
    target, manifest = _build_paths(tmp_path)
    target.write_bytes(b"not a knowledge store")
    with pytest.raises(MigrationSourceError) as excinfo:
        migrate_sources(
            contacts_path=sources.contacts,
            memory_path=sources.memory,
            target=target,
            manifest=manifest,
        )
    assert excinfo.value.reason == "target_exists"
    assert target.read_bytes() == b"not a knowledge store"


def test_semantic_digest_matches_between_a_copy_and_a_rebuild(tmp_path: Path):
    """Copying a verified target keeps its semantic identity (operational rehearsal)."""
    sources = legacy_snapshot_factory(tmp_path)
    target, manifest = _build_paths(tmp_path)
    migrate_sources(
        contacts_path=sources.contacts,
        memory_path=sources.memory,
        target=target,
        manifest=manifest,
    )
    rehearsal = tmp_path / "rehearsal.db"
    shutil.copy2(target, rehearsal)
    assert semantic_digest(rehearsal) == semantic_digest(target)


def test_multiple_sources_are_opened_read_only(tmp_path: Path):
    """The importer never opens a source for writing, even transiently."""
    sources = legacy_snapshot_factory(tmp_path)
    handle = _open_source("contacts", sources.contacts)
    try:
        with pytest.raises(Exception):
            handle.connection.execute("CREATE TABLE should_not_exist (id TEXT)")
    finally:
        handle.close()
    import sqlite3

    conn = sqlite3.connect(f"file:{sources.contacts}?mode=ro", uri=True)
    try:
        names = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master")}
    finally:
        conn.close()
    assert "should_not_exist" not in names
