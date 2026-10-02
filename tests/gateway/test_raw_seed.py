"""Seed existing history into the raw archive once, older than the live writer's START."""

from __future__ import annotations

import json
import sqlite3
import stat
from pathlib import Path

import pytest
from yeoman_gateway.storage.raw_seed import SeedPaths, seed_raw_archive
from yeoman_shared.raw_archive.records import iter_records
from yeoman_shared.raw_archive.verify import latest_manifest
from yeoman_shared.raw_archive.writer import RawArchive

START = 1_790_000_000_000
BEFORE = START - 86_400_000
AFTER = START + 1_000


def _paths(tmp_path: Path) -> SeedPaths:
    processing = tmp_path / "processing.db"
    con = sqlite3.connect(processing)
    con.execute(
        "CREATE TABLE events (event_id TEXT, kind TEXT, channel TEXT, chat_id TEXT, direction TEXT,"
        " source_message_id TEXT, created_ms INTEGER, account TEXT, payload_json TEXT)"
    )
    con.executemany(
        "INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)",
        [
            (
                "e1",
                "message",
                "whatsapp",
                "g1",
                "in",
                "M1",
                BEFORE,
                "acc",
                json.dumps({"text": "a"}),
            ),
            (
                "e2",
                "message",
                "whatsapp",
                "g1",
                "in",
                "M2",
                AFTER,
                "acc",
                json.dumps({"text": "b"}),
            ),
            ("e3", "message", "whatsapp", "g1", "in", "M3", BEFORE, "acc", None),
        ],
    )
    con.commit()
    reply = tmp_path / "reply_context.db"
    con = sqlite3.connect(reply)
    con.execute(
        "CREATE TABLE inbound_messages (channel TEXT, chat_id TEXT, message_id TEXT, participant TEXT,"
        " sender_id TEXT, text TEXT, timestamp INTEGER, created_at TEXT, sender_name TEXT)"
    )
    con.executemany(
        "INSERT INTO inbound_messages VALUES (?,?,?,?,?,?,?,?,?)",
        [
            ("whatsapp", "g1", "M1", "p", "s", "dup of journal", BEFORE // 1000, "", "n"),
            ("whatsapp", "g1", "M0", "p", "s", "only here", (BEFORE - 5_000) // 1000, "", "n"),
        ],
    )
    con.commit()
    inbound = tmp_path / "inbound"
    inbound.mkdir()
    (inbound / "whatsapp_g2@g.us.jsonl").write_text(
        json.dumps({"_type": "metadata"})
        + "\n"
        + json.dumps(
            {"role": "user", "content": "old session line", "timestamp": "2026-01-01T10:00:00"}
        )
        + "\n"
        + json.dumps(
            {"role": "user", "content": "newer session line", "timestamp": "2026-03-14T10:00:00"}
        )
        + "\n"
    )
    (inbound / "whatsapp_g2@g.us_thread_th_x.jsonl").write_text(
        json.dumps({"_type": "metadata"}) + "\n"
    )
    knowledge = tmp_path / "knowledge.db"
    con = sqlite3.connect(knowledge)
    con.execute(
        "CREATE TABLE memory2_nodes (id TEXT, channel TEXT, chat_id TEXT, sender_id TEXT, content TEXT,"
        " source_message_id TEXT, created_at TEXT, kind TEXT, is_deleted INTEGER)"
    )
    con.executemany(
        "INSERT INTO memory2_nodes VALUES (?,?,?,?,?,?,?,?,?)",
        [
            ("n1", "whatsapp", "g1", "s", "dup", "M1", "2026-09-01T00:00:00+00:00", "utterance", 0),
            (
                "n2",
                "whatsapp",
                "g3",
                "s",
                "unique",
                "M9",
                "2026-02-14T00:00:00+00:00",
                "utterance",
                0,
            ),
            (
                "n3",
                "whatsapp",
                "g3",
                "s",
                "deleted",
                "M8",
                "2026-02-14T00:00:00+00:00",
                "utterance",
                1,
            ),
            (
                "n4",
                "whatsapp",
                "g2@g.us",
                "s",
                "memory2 only",
                "M10",
                "2026-02-14T00:00:00+00:00",
                "utterance",
                0,
            ),
        ],
    )
    con.commit()
    return SeedPaths(
        processing_db=processing,
        reply_context_db=reply,
        inbound_dir=inbound,
        knowledge_db=knowledge,
    )


def _archive_root(tmp_path: Path) -> Path:
    RawArchive(
        tmp_path / "raw",
        spool=tmp_path / "spool",
        status_path=tmp_path / "s.json",
        clock=lambda: START,
    )
    return tmp_path / "raw"


def _seed_lines(root: Path, source: str) -> list[dict]:
    path = root / "whatsapp" / f"seed-{source}.jsonl"
    return [r for _, r, _ in iter_records(path) if r] if path.exists() else []


def test_seed_writes_each_message_once_in_priority_order(tmp_path: Path) -> None:
    root = _archive_root(tmp_path)
    report = seed_raw_archive(root, _paths(tmp_path), now_ms=START + 10)
    journal = _seed_lines(root, "journal")
    assert [r["native_id"] for r in journal] == ["M1"]
    assert journal[0]["provenance"] == "journal" and journal[0]["native"] == {"text": "a"}
    assert [r["native_id"] for r in _seed_lines(root, "reply_context")] == ["M0"]
    assert [r["native"]["content"] for r in _seed_lines(root, "session_jsonl")] == [
        "old session line"
    ]
    assert report.per_source["session_jsonl"]["skipped_duplicate"] == 1
    assert [r["native_id"] for r in _seed_lines(root, "memory2")] == ["M9", "M10"]
    assert report.per_source["journal"]["skipped_not_before_start"] == 1
    assert report.per_source["journal"]["skipped_no_payload"] == 1
    assert report.per_source["reply_context"]["skipped_duplicate"] == 1
    assert report.per_source["memory2"]["skipped_duplicate"] == 1


def test_seed_files_are_sealed_with_manifest_entries(tmp_path: Path) -> None:
    root = _archive_root(tmp_path)
    seed_raw_archive(root, _paths(tmp_path), now_ms=START + 10)
    path = root / "whatsapp" / "seed-journal.jsonl"
    assert stat.S_IMODE(path.stat().st_mode) == 0o444
    assert latest_manifest(root)["whatsapp/seed-journal.jsonl"]["note"] == "seed"


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    root = _archive_root(tmp_path)
    report = seed_raw_archive(root, _paths(tmp_path), dry_run=True, now_ms=START + 10)
    assert report.per_source["journal"]["written"] == 1
    assert not (root / "whatsapp").exists()


def test_seeding_twice_is_refused(tmp_path: Path) -> None:
    root = _archive_root(tmp_path)
    paths = _paths(tmp_path)
    seed_raw_archive(root, paths, now_ms=START + 10)
    with pytest.raises(RuntimeError, match="already seeded"):
        seed_raw_archive(root, paths, now_ms=START + 20)


def test_seeding_without_a_live_start_marker_is_refused(tmp_path: Path) -> None:
    (tmp_path / "raw").mkdir()
    with pytest.raises(RuntimeError, match="START"):
        seed_raw_archive(tmp_path / "raw", _paths(tmp_path))


def test_missing_source_stores_are_reported_not_fatal(tmp_path: Path) -> None:
    root = _archive_root(tmp_path)
    paths = SeedPaths(
        processing_db=tmp_path / "no1.db",
        reply_context_db=tmp_path / "no2.db",
        inbound_dir=tmp_path / "noinbound",
        knowledge_db=tmp_path / "no3.db",
    )
    report = seed_raw_archive(root, paths, now_ms=START + 10)
    assert all(counts["read"] == 0 for counts in report.per_source.values())


def test_default_memory2_sources_include_legacy_store_and_deduplicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("YEOMAN_HOME", str(tmp_path / "yeoman"))
    paths = SeedPaths.default()
    legacy = paths.legacy_memory_db
    assert paths.knowledge_db == tmp_path / "yeoman" / "data" / "knowledge" / "knowledge.db"
    assert legacy == tmp_path / "yeoman" / "data" / "memory" / "memory.db"

    rows_by_path = (
        (paths.knowledge_db, [("k1", "g4", "X", "primary", "2026-09-01T00:00:00+00:00")]),
        (
            legacy,
            [
                ("m1", "g4", "X", "legacy duplicate", "2026-09-02T00:00:00+00:00"),
                ("m2", "g5", "Y", "legacy only", "2026-08-01T00:00:00+00:00"),
            ],
        ),
    )
    for path, rows in rows_by_path:
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as con:
            con.execute(
                "CREATE TABLE memory2_nodes (id TEXT, channel TEXT, chat_id TEXT, sender_id TEXT,"
                " content TEXT, source_message_id TEXT, created_at TEXT, kind TEXT, is_deleted INTEGER)"
            )
            con.executemany(
                "INSERT INTO memory2_nodes VALUES (?, 'whatsapp', ?, 's', ?, ?, ?, 'utterance', 0)",
                [
                    (node_id, chat_id, content, native_id, created_at)
                    for node_id, chat_id, native_id, content, created_at in rows
                ],
            )

    root = _archive_root(tmp_path)
    report = seed_raw_archive(root, paths, now_ms=START + 10)
    records = _seed_lines(root, "memory2")
    assert {record["native_id"]: record["native"]["content"] for record in records} == {
        "X": "primary",
        "Y": "legacy only",
    }
    assert all(record["provenance"] == "memory2" for record in records)
    assert report.per_source["memory2"]["read"] == 3
    assert report.per_source["memory2"]["written"] == 2
    assert report.per_source["memory2"]["skipped_duplicate"] == 1
