"""Synthetic-only tests for the offline historical-source adapters."""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from yeoman_gateway.knowledge._history import HistoricalJournal
from yeoman_gateway.knowledge._history_adapters import (
    legacy_fidelity,
    normalize_time,
    read_catalogued_events,
)
from yeoman_gateway.knowledge._history_audience import HistoryAudience


def _bundle(root: Path, files: dict[str, dict[str, bytes]], *, complete: bool = True) -> Path:
    entries = []
    for source_id, members in files.items():
        source_root = root / "sources" / source_id
        copied = {}
        for name, data in members.items():
            path = source_root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            copied[name] = {"copied_sha256": hashlib.sha256(data).hexdigest()}
        entries.append(
            {
                "source_id": source_id,
                "kind": "static_sqlite_triple"
                if any(Path(name).suffix in {".db", ".sqlite", ".sqlite3"} for name in members)
                else "tree",
                "source_class": source_id,
                "status": "copied",
                "copied_files": copied,
            }
        )
    root.mkdir(parents=True, exist_ok=True)
    manifest = {
        "source_bundle_manifest_version": 2,
        "complete": complete,
        "sources": entries,
    }
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


def _db_bytes(path: Path, setup: str, rows: list[tuple] = ()) -> bytes:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(setup)
        for row in rows:
            connection.execute(row[0], row[1])
        connection.commit()
    finally:
        connection.close()
    return path.read_bytes()


def _jsonl(*records: object) -> bytes:
    return ("\n".join(json.dumps(record, separators=(",", ":")) for record in records) + "\n").encode()


def _event(**values):
    defaults = {
        "normalization_version": 1,
        "event_id": "history:test",
        "revision": 1,
        "channel": "whatsapp",
        "account": None,
        "chat_id": "chat",
        "native_id": None,
        "kind": "message",
        "direction": "in",
        "sender_raw": None,
        "principal": None,
        "observed_ms": None,
        "occurred_ms": None,
        "time_certainty": "unknown",
        "original_timestamp": None,
        "time_metadata": {},
        "text": None,
        "text_hash": None,
        "media_kind": None,
        "media_missing": False,
        "reply_target": None,
        "edit_target": None,
        "delete_target": None,
        "source_id": "fixture",
        "bundle_version": 2,
        "source_hash": "hash",
        "locator": {"file": "fixture.jsonl", "line": 1},
        "source_refs": (),
        "copies": (),
        "provenance_class": "native",
        "verbatim_unverified": False,
        "chat_kind": None,
        "native_evidence": (),
        "retention_status": "retained",
        "source_kind": "journal",
    }
    defaults.update(values)
    from yeoman_gateway.knowledge._history_records import NormalizedEvent

    return NormalizedEvent(**defaults)


def test_accounts_chats_and_unknown_accounts_do_not_collide(tmp_path: Path) -> None:
    database = tmp_path / "events.db"
    db = _db_bytes(
        database,
        "CREATE TABLE events(event_id TEXT, kind TEXT, channel TEXT, chat_id TEXT, direction TEXT, "
        "source_message_id TEXT, created_ms INTEGER, occurred_ms INTEGER, account TEXT, "
        "revision INTEGER, payload_json TEXT);",
        [
            ("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?)", ("a", "message", "whatsapp", "c1", "in", "same", 1000, 900, "acct-a", 1, '{"text":"x"}')),
            ("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?)", ("b", "message", "whatsapp", "c1", "in", "same", 1000, 900, "acct-b", 1, '{"text":"x"}')),
            ("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?)", ("c", "message", "whatsapp", "c2", "in", "same", 1000, 900, "acct-a", 1, '{"text":"x"}')),
            ("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?)", ("d", "message", "whatsapp", "c1", "in", "same", 1000, 900, "", 1, '{"text":"x"}')),
        ],
    )
    collection = _bundle(tmp_path / "collection", {"processing": {"data.db": db}})

    events, report = read_catalogued_events(collection=collection)

    assert {(event.account, event.chat_id, event.event_id) for event in events} == {
        ("acct-a", "c1", "a"),
        ("acct-b", "c1", "b"),
        ("acct-a", "c2", "c"),
        (None, "c1", "d"),
    }
    assert report["collection_verdict"] == "verified_available_sources"


def test_raw_payload_id_not_archive_native_id(tmp_path: Path) -> None:
    raw = _jsonl(
        {
            "channel": "whatsapp",
            "account": "",
            "chat_id": "alice@s.whatsapp.net",
            "native_id": "archive-event-777",
            "kind": "message",
            "direction": "in",
            "received_ms": 1_760_000_000_000,
            "native": {"eventId": "live-event", "type": "MESSAGE_CREATE", "payload": {"messageId": "wa-777", "conversation": "hello"}},
        }
    )
    collection = _bundle(tmp_path / "collection", {"raw_archive": {"data/raw/whatsapp/2026-10.jsonl": raw}})

    events, _ = read_catalogued_events(collection=collection)

    assert len(events) == 1
    assert events[0].native_id == "wa-777"
    assert events[0].event_id == "live-event"
    assert events[0].native_id != "archive-event-777"
    assert events[0].chat_kind == "direct"


def test_bridge_and_raw_same_message_keep_two_locators(tmp_path: Path) -> None:
    raw = {
        "channel": "whatsapp",
        "chat_id": "group@g.us",
        "native_id": "archive-id",
        "kind": "message",
        "direction": "in",
        "received_ms": 1_760_000_000_000,
        "native": {"eventId": "live-id", "type": "MESSAGE_CREATE", "payload": {"messageId": "msg-1", "conversation": "hello"}},
    }
    bridge_ref = {"chatJid": "group@g.us", "messageId": "msg-1", "storedAtMs": 1_760_000_000_000, "expiresAtMs": 1_760_000_100_000, "encoded": base64.b64encode(b"bad-protobuf").decode()}
    collection = _bundle(
        tmp_path / "collection",
        {
            "raw_archive": {"data/raw/whatsapp/2026-10.jsonl": _jsonl(raw)},
            "bridge_refs": {"whatsapp-message-references/abc.json": _jsonl(bridge_ref)},
        },
    )

    events, report = read_catalogued_events(collection=collection)

    matching = [event for event in events if event.native_id == "msg-1"]
    assert len(matching) == 1
    assert len(matching[0].copies) == 2
    assert {copy["locator"]["file"] for copy in matching[0].copies} == {
        "data/raw/whatsapp/2026-10.jsonl",
        "whatsapp-message-references/abc.json",
    }
    assert report["unresolved_count"] == 1


def test_installed_bridge_decoder_reads_valid_reference_and_merges_copy(tmp_path: Path) -> None:
    bridge_dir = Path("/home/dm/Documents/yeoman/packages/bridge")
    if not (bridge_dir / "node_modules/@whiskeysockets/baileys/WAProto/index.js").is_file():
        pytest.skip("installed offline Baileys decoder is unavailable")
    encoded = "ChkKCmdyb3VwQGcudXMQABoJbXNnLXZhbGlkEg0KC2JyaWRnZSB0ZXh0GIDwnccG"
    raw = {
        "channel": "whatsapp",
        "chat_id": "group@g.us",
        "native_id": "archive-id",
        "kind": "message",
        "direction": "in",
        "received_ms": 1_760_000_000_000,
        "native": {"eventId": "live-id", "type": "MESSAGE_CREATE", "payload": {"messageId": "msg-valid", "conversation": "bridge text"}},
    }
    bridge_ref = {"chatJid": "group@g.us", "messageId": "msg-valid", "storedAtMs": 1_760_000_000_000, "expiresAtMs": 1_760_000_100_000, "encoded": encoded}
    collection = _bundle(
        tmp_path / "collection",
        {
            "raw_archive": {"data/raw/whatsapp/2026-10.jsonl": _jsonl(raw)},
            "bridge_refs": {"whatsapp-message-references/abc.json": _jsonl(bridge_ref)},
        },
    )

    events, report = read_catalogued_events(collection=collection, bridge_package_dir=bridge_dir)

    message = next(event for event in events if event.native_id == "msg-valid")
    assert message.event_id == "live-id"
    assert message.text == "bridge text"
    assert message.chat_kind == "group"
    assert len(message.copies) == 2
    assert report["unresolved_count"] == 0


def test_repeated_edits_remain_distinct(tmp_path: Path) -> None:
    archive = _jsonl(
        {"channel": "whatsapp", "chat_id": "g", "native_id": "archive-a", "kind": "message", "direction": "in", "received_ms": 1_760_000_000_001, "native": {"eventId": "edit-a", "type": "MESSAGE_EDIT", "payload": {"messageId": "m", "conversation": "first"}}},
        {"channel": "whatsapp", "chat_id": "g", "native_id": "archive-b", "kind": "message", "direction": "in", "received_ms": 1_760_000_000_002, "native": {"eventId": "edit-b", "type": "MESSAGE_EDIT", "payload": {"messageId": "m", "conversation": "second"}}},
    )
    collection = _bundle(tmp_path / "collection", {"raw_archive": {"data/raw/whatsapp/2026-10.jsonl": archive}})

    events, _ = read_catalogued_events(collection=collection)

    edits = [event for event in events if event.kind == "edit"]
    assert [event.event_id for event in edits] == ["edit-a", "edit-b"]
    assert [event.text for event in edits] == ["first", "second"]


def test_live_ids_and_all_copies_retained(tmp_path: Path) -> None:
    db = _db_bytes(
        tmp_path / "events.db",
        "CREATE TABLE events(event_id TEXT, kind TEXT, channel TEXT, chat_id TEXT, direction TEXT, source_message_id TEXT, created_ms INTEGER, occurred_ms INTEGER, account TEXT, revision INTEGER, payload_json TEXT);",
        [("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?)", ("live-event", "message", "whatsapp", "g", "in", "m", 1000, 900, "", 1, '{"text":"same"}'))],
    )
    raw = _jsonl({"channel": "whatsapp", "chat_id": "g", "native_id": "archive-id", "kind": "message", "direction": "in", "received_ms": 1000, "provenance": "journal", "native": {"eventId": "live-event", "type": "MESSAGE_CREATE", "payload": {"messageId": "m", "conversation": "same"}}})
    collection = _bundle(tmp_path / "collection", {"processing": {"data.db": db}, "raw_archive": {"data/raw/whatsapp/2026-10.jsonl": raw}})

    events, _ = read_catalogued_events(collection=collection)

    message = next(event for event in events if event.native_id == "m")
    assert message.event_id == "live-event"
    assert {copy["source_id"] for copy in message.copies} == {"processing", "raw_archive"}
    assert len(message.copies) == 2


def test_legacy_equal_diff_and_single_survivor_fidelity(tmp_path: Path) -> None:
    db = _db_bytes(
        tmp_path / "history.db",
        "CREATE TABLE inbound_messages(channel TEXT, chat_id TEXT, message_id TEXT, timestamp INTEGER, created_at TEXT, sender_id TEXT, text TEXT);"
        "CREATE TABLE memory2_nodes(id TEXT, channel TEXT, chat_id TEXT, sender_id TEXT, content TEXT, source_message_id TEXT, created_at TEXT, kind TEXT, is_deleted INTEGER);",
        [
            ("INSERT INTO inbound_messages VALUES (?,?,?,?,?,?,?)", ("whatsapp", "g", "eq", 1760000000, "2025-10-09T00:00:01+00:00", "p", "identical")),
            ("INSERT INTO inbound_messages VALUES (?,?,?,?,?,?,?)", ("whatsapp", "g", "diff", 1760000001, "2025-10-09T00:00:02+00:00", "p", "original")),
            ("INSERT INTO memory2_nodes VALUES (?,?,?,?,?,?,?,?,?)", ("n-eq", "whatsapp", "g", "p", "identical", "eq", "2025-10-09T00:00:01+00:00", "utterance", 0)),
            ("INSERT INTO memory2_nodes VALUES (?,?,?,?,?,?,?,?,?)", ("n-diff", "whatsapp", "g", "p", "enriched", "diff", "2025-10-09T00:00:02+00:00", "utterance", 0)),
            ("INSERT INTO memory2_nodes VALUES (?,?,?,?,?,?,?,?,?)", ("n-only", "whatsapp", "g", "p", "single", "solo", "2025-10-09T00:00:03+00:00", "utterance", 0)),
            ("INSERT INTO memory2_nodes VALUES (?,?,?,?,?,?,?,?,?)", ("n-summary", "whatsapp", "g", "p", "summary", "", "2025-10-09T00:00:04+00:00", "summary", 0)),
            ("INSERT INTO memory2_nodes VALUES (?,?,?,?,?,?,?,?,?)", ("n-erased", "whatsapp", "g", "p", "must-not-return", "erased", "2025-10-09T00:00:05+00:00", "utterance", 1)),
        ],
    )
    collection = _bundle(tmp_path / "collection", {"legacy": {"knowledge.db": db}})

    events, report = read_catalogued_events(collection=collection)
    classified = legacy_fidelity(events)
    legacy = {event.event_id: event for event in classified if event.source_kind == "memory2_nodes"}

    assert legacy["legacy-node:n-eq"].provenance_class == "recovered_text"
    assert legacy["legacy-node:n-diff"].provenance_class == "derived_only"
    assert legacy["legacy-node:n-only"].verbatim_unverified is True
    assert legacy["legacy-node:n-summary"].provenance_class == "derived_only"
    assert legacy["legacy-node:n-erased"].text is None
    assert legacy["legacy-node:n-erased"].retention_status == "erased"
    assert report["denial_evidence_count"] == 1


def test_capture_time_never_becomes_native(tmp_path: Path) -> None:
    db = _db_bytes(
        tmp_path / "legacy.db",
        "CREATE TABLE memory2_nodes(id TEXT, channel TEXT, chat_id TEXT, sender_id TEXT, content TEXT, source_message_id TEXT, created_at TEXT, kind TEXT, is_deleted INTEGER);",
        [("INSERT INTO memory2_nodes VALUES (?,?,?,?,?,?,?,?,?)", ("n", "whatsapp", "g", "p", "text", "m", "2026-09-01T10:00:00", "utterance", 0))],
    )
    collection = _bundle(tmp_path / "collection", {"legacy": {"memory.db": db}})

    events, _ = read_catalogued_events(collection=collection)

    legacy = events[0]
    assert legacy.observed_ms is not None
    assert legacy.occurred_ms is None
    assert legacy.time_certainty == "capture_time_approx"
    assert legacy.time_metadata["source_basis"] == "created_at_capture"
    assert legacy.time_metadata["timezone_basis"] == "Europe/Berlin"


def test_authored_effects_have_no_transport_receipt(tmp_path: Path) -> None:
    db = _db_bytes(
        tmp_path / "processing.db",
        "CREATE TABLE effects(effect_id TEXT, target_json TEXT, payload_json TEXT, payload_kind TEXT, state TEXT, created_ms INTEGER, payload_purged_ms INTEGER);",
        [("INSERT INTO effects VALUES (?,?,?,?,?,?,?)", ("effect-1", '{"channel":"whatsapp","chat_id":"g"}', '{"text":"authored text"}', "text", "pending", 1760000000000, None))],
    )
    collection = _bundle(tmp_path / "collection", {"processing": {"data.db": db}})

    events, _ = read_catalogued_events(collection=collection)

    assert len(events) == 1
    assert events[0].direction == "out"
    assert events[0].text == "authored text"
    assert events[0].transport_receipt == "not_claimed"
    assert events[0].source_authority == "none_authored_outbound_context_only"


def test_epoch_session_unknown_and_bot_outbound(tmp_path: Path) -> None:
    session = _jsonl(
        {"role": "user", "timestamp": "1970-01-01T00:00:00Z", "text": "old"},
        {"role": "assistant", "timestamp": "2026-09-01T12:00:00Z", "text": "bot"},
    )
    collection = _bundle(tmp_path / "collection", {"sessions": {"data/sessions/whatsapp_chat.jsonl": session}})

    events, _ = read_catalogued_events(collection=collection)

    assert len(events) == 2
    assert events[0].occurred_ms is None and events[0].time_certainty == "unknown"
    assert events[1].direction == "out"


def test_berlin_naive_and_dst_fold_ambiguous() -> None:
    winter = normalize_time("2026-01-15T12:00:00", basis="provider_timestamp", source_kind="inbound")
    fold = normalize_time("2026-10-25T02:30:00", basis="provider_timestamp", source_kind="inbound")
    gap = normalize_time("2026-03-29T02:30:00", basis="provider_timestamp", source_kind="inbound")

    assert winter["occurred_ms"] == 1_768_474_800_000
    assert winter["time_certainty"] == "provider_timestamp"
    assert winter["time_metadata"]["source_basis"] == "provider_timestamp"
    assert winter["time_metadata"]["timezone_basis"] == "Europe/Berlin"
    assert fold["occurred_ms"] is None and fold["time_certainty"] == "ambiguous"
    assert gap["occurred_ms"] is None and gap["time_certainty"] == "ambiguous"


def test_malformed_line_or_unknown_schema_is_reported(tmp_path: Path) -> None:
    jsonl = b'{"not":"a message"}\nnot-json\n'
    db = _db_bytes(
        tmp_path / "unknown.db",
        "CREATE TABLE future_table(secret TEXT); INSERT INTO future_table VALUES ('hidden');",
    )
    collection = _bundle(tmp_path / "collection", {"sessions": {"sessions/input.jsonl": jsonl}, "legacy": {"data.db": db}})

    events, report = read_catalogued_events(collection=collection)

    assert events == []
    assert report["omitted_counts"]["malformed_json_line"] == 1
    assert report["unsupported_tables"]["future_table"]["count"] == 1
    assert "hidden" not in json.dumps(report)


def test_tampered_manifest_and_locator_traversal_rejected(tmp_path: Path) -> None:
    data = _jsonl({"channel": "whatsapp", "chat_id": "g", "native_id": "a", "kind": "message", "direction": "in", "native": {"payload": {"messageId": "m", "conversation": "x"}}})
    tampered = _bundle(tmp_path / "tampered", {"raw_archive": {"data/raw/a.jsonl": data}})
    manifest_path = tampered / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["sources"][0]["copied_files"]["data/raw/a.jsonl"]["copied_sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="hash"):
        read_catalogued_events(collection=tampered)

    bad = tmp_path / "bad-reference"
    bad.mkdir()
    (bad / "manifest.json").write_text(
        json.dumps(
            {
                "source_bundle_manifest_version": 2,
                "complete": True,
                "sources": [
                    {
                        "source_id": "raw",
                        "status": "reference_only",
                        "kind": "reference_only",
                        "source_class": "raw_archive",
                        "source_path": str(tmp_path),
                        "record_metadata": {"records": [{"locator": {"file": "../outside.jsonl", "line": 1}}]},
                        "reference_file_stats": {},
                    }
                ],
            }
        )
    )
    with pytest.raises(ValueError, match="locator"):
        read_catalogued_events(collection=bad)


def test_reference_only_sources_read_only_declared_lines(tmp_path: Path) -> None:
    source_root = tmp_path / "source-reference"
    source_root.mkdir()
    native = _jsonl(
        {"channel": "whatsapp", "chat_id": "g", "native_id": "archive-a", "kind": "message", "direction": "in", "native": {"payload": {"messageId": "a", "conversation": "first"}}},
        {"channel": "whatsapp", "chat_id": "g", "native_id": "archive-b", "kind": "message", "direction": "in", "native": {"payload": {"messageId": "b", "conversation": "second"}}},
    )
    source_file = source_root / "2026-10.jsonl"
    source_file.write_bytes(native)
    digest = hashlib.sha256(native).hexdigest()
    collection = tmp_path / "collection"
    collection.mkdir()
    (collection / "manifest.json").write_text(
        json.dumps(
            {
                "source_bundle_manifest_version": 2,
                "complete": True,
                "sources": [
                    {
                        "source_id": "raw_archive",
                        "kind": "reference_only",
                        "source_class": "raw_archive",
                        "status": "reference_only",
                        "source_path": str(source_root),
                        "reference_file_stats": {"2026-10.jsonl": {"sha256": digest}},
                        "record_metadata": {
                            "records": [
                                {"locator": {"file": "2026-10.jsonl", "line": 1}},
                                {"locator": {"file": "2026-10.jsonl", "line": 2}},
                            ]
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    events, report = read_catalogued_events(collection=collection)

    assert {event.native_id for event in events} == {"a", "b"}
    assert report["sources"][0]["parsed_count"] == 2


def test_static_triple_original_unchanged(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    connection = sqlite3.connect(source)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.execute("CREATE TABLE events(event_id TEXT, kind TEXT, channel TEXT, chat_id TEXT, direction TEXT, source_message_id TEXT, created_ms INTEGER, occurred_ms INTEGER, account TEXT, revision INTEGER, payload_json TEXT)")
    connection.execute("INSERT INTO events VALUES ('live', 'message', 'whatsapp', 'g', 'in', 'm', 10, 9, '', 1, '{\"text\":\"x\"}')")
    connection.commit()
    members = {"source.db": source.read_bytes()}
    for suffix in ("-wal", "-shm"):
        companion = Path(f"{source}{suffix}")
        if companion.exists():
            members[companion.name] = companion.read_bytes()
    before = {name: hashlib.sha256(data).hexdigest() for name, data in members.items()}
    collection = _bundle(tmp_path / "collection", {"static": members})

    events, _ = read_catalogued_events(collection=collection)

    after = {name: hashlib.sha256(Path(f"{source.parent}/{name}").read_bytes()).hexdigest() for name in members}
    connection.close()
    assert len(events) == 1
    assert after == before


def test_native_and_provider_times_are_proof_eligible_and_unknown_chats_stay_visible(tmp_path: Path) -> None:
    journal = HistoricalJournal(tmp_path / "target")
    audience = HistoryAudience(journal)
    try:
        event = {
            "channel": "whatsapp",
            "account": "acct",
            "chat_id": "g",
            "occurred_ms": 100,
            "time_certainty": "native",
            "source_refs": [{"source_id": "raw", "locator": {"file": "a.jsonl", "line": 1}}],
            "native_evidence": {
                "evidence_class": "native_snapshot",
                "members": ["whatsapp:1"],
                "source_refs": [{"source_id": "raw", "locator": {"file": "a.jsonl", "line": 1}}],
                "valid_from_ms": 0,
                "valid_until_ms": 200,
            },
        }
        assert audience.resolve(event).status == "known"
        provider = {**event, "time_certainty": "provider_timestamp"}
        assert audience.resolve(provider).status == "known"

        coverage = audience.coverage(
            [
                {"channel": "whatsapp", "account": None, "chat_id": "group-a", "occurred_ms": 100, "time_certainty": "unknown"},
                {"channel": "whatsapp", "account": None, "chat_id": "group-b", "occurred_ms": 100, "time_certainty": "unknown"},
            ]
        )
        assert {(item["channel"], item["account"], item["chat_id"]) for item in coverage["buckets"]} == {
            ("whatsapp", "unknown", "group-a"),
            ("whatsapp", "unknown", "group-b"),
        }
        assert all(item["unknown_audience_count"] == 1 for item in coverage["buckets"])
    finally:
        journal.close()


def test_reference_only_accounts_for_every_declared_locator(tmp_path: Path) -> None:
    source_root = tmp_path / "reference-source"
    source_root.mkdir()
    data = (
        b"not-json\n[]\n"
        + _jsonl({"not": "a raw event"})
        + _jsonl(
            {
                "channel": "whatsapp",
                "chat_id": "group@g.us",
                "direction": "in",
                "kind": "message",
                "native": {
                    "eventId": "raw-line-four",
                    "type": "MESSAGE_CREATE",
                    "payload": {"messageId": "msg-valid", "conversation": "raw text"},
                },
            }
        )
    )
    source_file = source_root / "refs.jsonl"
    source_file.write_bytes(data)
    collection = tmp_path / "collection"
    collection.mkdir()
    (collection / "manifest.json").write_text(
        json.dumps(
            {
                "source_bundle_manifest_version": 2,
                "complete": True,
                "sources": [
                    {
                        "source_id": "raw_archive",
                        "kind": "reference_only",
                        "source_class": "raw_archive",
                        "status": "reference_only",
                        "source_path": str(source_root),
                        "reference_file_stats": {
                            "refs.jsonl": {"sha256": hashlib.sha256(data).hexdigest()}
                        },
                        "record_metadata": {
                            "records": [
                                {"locator": {"file": "refs.jsonl", "line": line}}
                                for line in (1, 2, 3, 4, 8)
                            ]
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    events, report = read_catalogued_events(collection=collection)

    assert [event.native_id for event in events] == ["msg-valid"]
    assert {
        (item["locator"]["file"], item["locator"]["line"])
        for item in report["unresolved"]
    } == {("refs.jsonl", 1), ("refs.jsonl", 2), ("refs.jsonl", 3), ("refs.jsonl", 8)}
    assert report["parsed_count"] == 1


def test_raw_and_journal_native_senders_and_targets_survive(tmp_path: Path) -> None:
    db = _db_bytes(
        tmp_path / "events.db",
        "CREATE TABLE events(event_id TEXT, kind TEXT, channel TEXT, chat_id TEXT, direction TEXT, "
        "source_message_id TEXT, target_message_id TEXT, created_ms INTEGER, occurred_ms INTEGER, payload_json TEXT);",
        [
            ("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?)", ("journal-edit", "edit", "whatsapp", "g", "in", "edit-envelope", "journal-edit-target", 1000, 900, '{"text":"edited"}')),
            ("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?)", ("journal-delete", "delete", "whatsapp", "g", "in", "delete-envelope", "journal-delete-target", 1001, 901, '{"revoked":true}')),
        ],
    )
    raw = _jsonl(
        {
            "channel": "whatsapp",
            "chat_id": "group@g.us",
            "direction": "in",
            "native": {
                "eventId": "raw-edit",
                "type": "MESSAGE_EDIT",
                "payload": {
                    "messageId": "raw-edit-target",
                    "senderId": "raw-sender-id",
                    "participantJid": "4915@s.whatsapp.net",
                    "conversation": "edited text",
                },
            },
        },
        {
            "channel": "whatsapp",
            "chat_id": "group@g.us",
            "direction": "in",
            "native": {
                "eventId": "raw-delete",
                "type": "MESSAGE_DELETE",
                "payload": {"messageId": "raw-delete-target"},
            },
        },
    )
    collection = _bundle(
        tmp_path / "collection",
        {"processing": {"events.db": db}, "raw_archive": {"data/raw/whatsapp/events.jsonl": raw}},
    )

    events, _ = read_catalogued_events(collection=collection)
    by_event_id = {event.event_id: event for event in events}

    assert by_event_id["raw-edit"].edit_target == "raw-edit-target"
    assert by_event_id["raw-edit"].sender_raw == "raw-sender-id"
    assert by_event_id["raw-edit"].principal is None
    assert by_event_id["raw-edit"].as_dict().get("sender_id_raw") == "raw-sender-id"
    assert by_event_id["raw-edit"].as_dict().get("participant_jid_raw") == "4915@s.whatsapp.net"
    assert by_event_id["raw-delete"].delete_target == "raw-delete-target"
    assert by_event_id["journal-edit"].edit_target == "journal-edit-target"
    assert by_event_id["journal-delete"].delete_target == "journal-delete-target"


def test_purged_event_marker_and_scope_survive_compatible_merge(tmp_path: Path) -> None:
    db = _db_bytes(
        tmp_path / "events.db",
        "CREATE TABLE events(event_id TEXT, kind TEXT, channel TEXT, chat_id TEXT, direction TEXT, "
        "source_message_id TEXT, target_message_id TEXT, created_ms INTEGER, occurred_ms INTEGER, "
        "account TEXT, revision INTEGER, payload_json TEXT, payload_purged_ms INTEGER);",
        [
            ("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", ("purged-event", "message", "telegram", "room", "in", "purged-id", None, 2000, 1000, "", 1, None, 1500)),
            ("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", ("missing-event", "message", "telegram", "room", "in", "missing-id", None, 2001, 1001, "", 1, None, None)),
        ],
    )
    raw = _jsonl(
        {
            "channel": "telegram",
            "chat_id": "room",
            "direction": "in",
            "kind": "message",
            "native": {
                "eventId": "purged-event",
                "type": "MESSAGE_CREATE",
                "payload": {"messageId": "purged-id", "conversation": "surviving independent copy"},
            },
        }
    )
    collection = _bundle(
        tmp_path / "collection",
        {"processing": {"events.db": db}, "raw_archive": {"data/raw/telegram/events.jsonl": raw}},
    )

    events, _ = read_catalogued_events(collection=collection)
    purged = next(event for event in events if event.native_id == "purged-id")
    missing = next(event for event in events if event.native_id == "missing-id")
    journal_copy = next(copy for copy in purged.copies if copy["source_id"] == "processing")

    assert purged.text == "surviving independent copy"
    assert purged.as_dict().get("payload_purged_ms") == 1500
    assert purged.source_authority == "payload_purged"
    assert journal_copy.get("payload_purged_ms") == 1500
    assert (journal_copy.get("channel"), journal_copy.get("chat_id"), journal_copy.get("account")) == (
        "telegram",
        "room",
        None,
    )
    assert missing.as_dict().get("payload_purged_ms") is None
    assert missing.source_authority == "payload_missing"


def test_legacy_author_role_and_human_only_fidelity_witnesses(tmp_path: Path) -> None:
    db = _db_bytes(
        tmp_path / "legacy.db",
        "CREATE TABLE memory2_nodes(id TEXT, channel TEXT, chat_id TEXT, sender_id TEXT, content TEXT, "
        "source_message_id TEXT, created_at TEXT, kind TEXT, is_deleted INTEGER, source_role TEXT);",
        [
            ("INSERT INTO memory2_nodes VALUES (?,?,?,?,?,?,?,?,?,?)", ("bot-node", "whatsapp", "g", "bot", "bot utterance", "bot-message", "2025-10-09T00:00:01+00:00", "utterance", 0, "assistant")),
            ("INSERT INTO memory2_nodes VALUES (?,?,?,?,?,?,?,?,?,?)", ("human-node", "whatsapp", "g", "user", "same words", "human-message", "2025-10-09T00:00:02+00:00", "utterance", 0, "user")),
        ],
    )
    raw = _jsonl(
        {
            "channel": "whatsapp",
            "chat_id": "g",
            "direction": "out",
            "native": {
                "eventId": "bot-copy",
                "type": "MESSAGE_CREATE",
                "payload": {"messageId": "bot-message", "conversation": "bot utterance"},
            },
        },
        {
            "channel": "whatsapp",
            "chat_id": "g",
            "direction": "out",
            "native": {
                "eventId": "authored-copy",
                "type": "MESSAGE_CREATE",
                "payload": {"messageId": "human-message", "conversation": "same words"},
            },
        },
    )
    collection = _bundle(
        tmp_path / "collection",
        {"legacy": {"knowledge.db": db}, "raw_archive": {"data/raw/whatsapp/events.jsonl": raw}},
    )

    events, _ = read_catalogued_events(collection=collection)
    legacy = {event.event_id: event for event in events if event.source_kind == "memory2_nodes"}
    derived_copy = _event(
        event_id="derived-human-copy",
        source_id="derived",
        source_kind="derived_fixture",
        channel="whatsapp",
        chat_id="g",
        native_id="human-message",
        kind="message",
        direction="in",
        text="same words",
        text_hash=hashlib.sha256(b"same words").hexdigest(),
        provenance_class="derived_only",
        source_authority="derived_only",
    )

    bot = legacy["legacy-node:bot-node"]
    human = legacy["legacy-node:human-node"]
    assert bot.as_dict().get("source_role") == "assistant"
    assert bot.direction == "out"
    assert bot.source_authority == "none_authored_outbound_context_only"
    assert human.as_dict().get("source_role") == "user"
    assert human.provenance_class == "legacy_unverified"
    assert legacy_fidelity([human, derived_copy])[0].provenance_class == "legacy_unverified"
