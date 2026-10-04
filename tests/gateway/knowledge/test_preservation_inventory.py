from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from yeoman_gateway.knowledge._preservation_inventory import (
    inspect_source,
    inventory_sources,
)


def _jsonl(path: Path, *rows: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def test_inspect_jsonl_keeps_original_and_creation_time_distinct_and_records_parse_errors(
    tmp_path: Path,
) -> None:
    path = tmp_path / "messages.jsonl"
    _jsonl(
        path,
        json.dumps(
            {
                "message_id": "native-jan",
                "chat_id": "chat-private",
                "timestamp": "2026-01-15T12:30:00Z",
                "created_at": "2026-02-01T01:02:03Z",
                "type": "message",
                "text": "private message body",
            }
        ),
        '{"message_id":',
        json.dumps({"message_id": "undated", "text": "also private"}),
    )

    result = inspect_source(path=path, source_class="conversation")

    jan, undated = result["records"]
    assert jan["original_time"] == "2026-01-15T12:30:00Z"
    assert jan["creation_time"] == "2026-02-01T01:02:03Z"
    assert jan["native_id"] == "native-jan"
    assert jan["chat"] == "chat-private"
    assert jan["locator"] == {"file": "messages.jsonl", "line": 1}
    assert undated["original_time"] is None
    assert undated["creation_time"] is None
    assert result["parse_errors"] == [{"line": 2, "error": "JSONDecodeError"}]
    assert "private message body" not in json.dumps(result)
    assert "also private" not in json.dumps(result)


def test_inventory_reports_january_as_explicitly_empty(tmp_path: Path) -> None:
    _jsonl(
        tmp_path / "data/inbound/sample.jsonl",
        json.dumps({"message_id": "march", "timestamp": "2026-03-01T00:00:00Z"}),
    )

    result = inventory_sources(home=tmp_path)

    january = result["coverage"]["months"]["2026-01"]
    assert january["records"] == 0
    assert january["status"] == "empty"
    assert not any(
        source["path"] == "data/inbound/*.jsonl" for source in result["sources"]
    )


def test_inspect_sqlite_inventories_unknown_schema_without_returning_row_values(
    tmp_path: Path,
) -> None:
    path = tmp_path / "future.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE events (native_id TEXT PRIMARY KEY,"
            "occurred_at TEXT, ingest_time TEXT, future_column TEXT)"
        )
        connection.execute(
            "INSERT INTO events VALUES (?, ?, ?, ?)",
            ("event-7", "2026-04-05T06:07:08Z", "2026-04-06T00:00:00Z", "secret payload"),
        )
        connection.execute("CREATE TABLE unknown_future (surprise_column TEXT)")
        connection.execute("INSERT INTO unknown_future VALUES ('unindexed payload')")

    result = inspect_source(path=path, source_class="conversation")

    table = next(item for item in result["tables"] if item["name"] == "events")
    assert table["row_count"] == 1
    assert {column["name"] for column in table["columns"]} == {
        "native_id",
        "occurred_at",
        "ingest_time",
        "future_column",
    }
    assert result["records"][0]["native_id"] == "event-7"
    assert result["records"][0]["original_time"] == "2026-04-05T06:07:08Z"
    assert result["records"][0]["creation_time"] == "2026-04-06T00:00:00Z"
    assert "secret payload" not in json.dumps(result)
    locator = result["records"][0]["locator"]
    assert locator["file"] == "future.db"
    assert locator["table"] == "events"
    assert len(locator["primary_key_sha256"]) == 64
    assert "event-7" not in json.dumps(locator)
    unknown = next(item for item in result["tables"] if item["name"] == "unknown_future")
    assert unknown["row_count"] == 1
    assert [column["name"] for column in unknown["columns"]] == ["surprise_column"]
    assert unknown["cursor"]["latest_original_time"] is None
    assert not any(record["locator"].get("table") == "unknown_future" for record in result["records"])
    assert "unindexed payload" not in json.dumps(result)


def test_arbitrary_primary_key_is_opaque_in_searchable_locator(tmp_path: Path) -> None:
    path = tmp_path / "text-key.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE events (text TEXT PRIMARY KEY, occurred_at TEXT)")
        connection.execute(
            "INSERT INTO events VALUES (?, ?)",
            ("message body must stay private", "2026-10-03T10:00:00Z"),
        )

    result = inspect_source(path=path, source_class="conversation")

    rendered = json.dumps(result)
    assert "message body must stay private" not in rendered
    locator = result["records"][0]["locator"]
    assert locator["primary_key_sha256"]
    assert "primary_key" not in locator


def test_record_cursor_uses_latest_valid_full_instant_and_ignores_invalid_dates(
    tmp_path: Path,
) -> None:
    path = tmp_path / "data/inbound/events.jsonl"
    _jsonl(
        path,
        json.dumps({"event_id": "older", "occurred_at": "2026-10-01T23:59:00Z"}),
        json.dumps({"event_id": "lexically-newer", "occurred_at": "2026-10-03T00:30:00+02:00"}),
        json.dumps({"event_id": "instant-newer", "occurred_at": "2026-10-02T23:00:00Z"}),
        json.dumps({"event_id": "invalid", "occurred_at": "2026-10-99T00:00:00Z"}),
    )

    result = inventory_sources(home=tmp_path)

    source = next(item for item in result["sources"] if item["path"] == "data/inbound/events.jsonl")
    assert source["cursor"]["latest_original_time"] == "2026-10-02T23:00:00Z"
    assert result["coverage"]["unknown_dates"] == 1


def test_sqlite_table_cursor_uses_stored_at_ms_and_full_instant_order(tmp_path: Path) -> None:
    path = tmp_path / "events.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE events (native_id TEXT PRIMARY KEY, occurred_at TEXT, stored_at_ms INTEGER)"
        )
        connection.executemany(
            "INSERT INTO events VALUES (?, ?, ?)",
            [
                ("older", "2026-10-01T23:59:00Z", 1_790_880_000_000),
                ("lexically-newer", "2026-10-03T00:30:00+02:00", 1_791_000_000_000),
                ("instant-newer", "2026-10-02T23:00:00Z", 1_791_100_000_000),
            ],
        )

    result = inspect_source(path=path, source_class="conversation")

    table = next(item for item in result["tables"] if item["name"] == "events")
    assert table["cursor"]["latest_original_time"] == "2026-10-02T23:00:00Z"
    assert table["cursor"]["creation_time_column"] == "stored_at_ms"
    assert table["cursor"]["latest_creation_time"] == 1_791_100_000_000


def test_inspect_static_sqlite_triple_does_not_mutate_source_or_sidecars(tmp_path: Path) -> None:
    path = tmp_path / "static.db"
    writer = sqlite3.connect(path)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("CREATE TABLE event_log (event_id TEXT, occurred_ms INTEGER)")
    writer.execute("INSERT INTO event_log VALUES ('event-1', 1760000000000)")
    writer.commit()
    # Keep the writer open so the committed row remains in the WAL for inspection.
    sidecars = (Path(f"{path}-wal"), Path(f"{path}-shm"))
    before = {
        item: (hashlib.sha256(item.read_bytes()).hexdigest(), item.stat().st_mtime_ns)
        for item in (path, *sidecars)
        if item.exists()
    }
    try:
        result = inspect_source(path=path, source_class="static_backup")
        after = {
            item: (hashlib.sha256(item.read_bytes()).hexdigest(), item.stat().st_mtime_ns)
            for item in before
        }
    finally:
        writer.close()

    assert result["tables"][0]["row_count"] == 1
    assert after == before


def test_cold_live_sqlite_descriptor_inspects_wal_only_committed_row(tmp_path: Path) -> None:
    path = tmp_path / "processing.db"
    writer = sqlite3.connect(path)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("CREATE TABLE events (event_id TEXT PRIMARY KEY)")
    writer.execute("INSERT INTO events VALUES ('wal-only-row')")
    writer.commit()
    wal = Path(f"{path}-wal")
    try:
        assert wal.stat().st_size > 0
        result = inspect_source(path=path, source_class="normalized")
    finally:
        writer.close()

    events = next(table for table in result["tables"] if table["name"] == "events")
    assert events["row_count"] == 1
    assert result["records"][0]["native_id"] is None
    assert "wal-only-row" not in json.dumps(result["records"][0]["locator"])


def test_restricted_source_omits_row_values_from_searchable_metadata(tmp_path: Path) -> None:
    path = tmp_path / "policy-audit.jsonl"
    _jsonl(
        path,
        json.dumps(
            {
                "chat_id": "private-chat-id",
                "native_id": "private-native-id",
                "created_at": "2026-05-01T00:00:00Z",
                "token": "credential-value",
            }
        ),
    )

    result = inspect_source(path=path, source_class="operational")

    rendered = json.dumps(result)
    assert "private-chat-id" not in rendered
    assert "private-native-id" not in rendered
    assert "credential-value" not in rendered
    assert result["records"] == []


def test_unknown_source_class_defaults_to_restricted_metadata(tmp_path: Path) -> None:
    path = tmp_path / "unknown.json"
    path.write_text(
        json.dumps({"chat_id": "unclassified-private-chat", "message_id": "unclassified-id"}),
        encoding="utf-8",
    )

    result = inspect_source(path=path, source_class="unknown")

    assert result["restricted"] is True
    assert result["records"] == []
    assert "unclassified-private-chat" not in json.dumps(result)


def test_cold_location_does_not_turn_live_sqlite_sources_into_static_triples(tmp_path: Path) -> None:
    cold = tmp_path / "backups/preservation/2026-10-03-cold"
    path = cold / "data/processing/processing.db"
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE events (event_id TEXT)")
        connection.execute("INSERT INTO events VALUES ('event-1')")

    result = inventory_sources(home=cold)

    processing = next(source for source in result["sources"] if source["path"] == "data/processing/processing.db")
    assert processing["kind"] == "live_sqlite"


def test_inventory_records_missing_sources_and_excludes_authentication_paths(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("TOKEN=must-not-read\n", encoding="utf-8")
    auth = tmp_path / "secrets/whatsapp-auth/creds.json"
    auth.parent.mkdir(parents=True)
    auth.write_text('{"secret":"must-not-read"}', encoding="utf-8")

    result = inventory_sources(home=tmp_path)

    missing = next(item for item in result["sources"] if item["path"] == "data/inbound/archive.db")
    assert missing["status"] == "missing"
    paths = {item["path"] for item in result["sources"]}
    assert ".env" not in paths
    assert "secrets/whatsapp-auth/creds.json" not in paths
    assert "must-not-read" not in json.dumps(result)


def test_bridge_reference_metadata_excludes_encoded_native_payload(tmp_path: Path) -> None:
    path = tmp_path / "reference.json"
    path.write_text(
        json.dumps(
            {
                "chatJid": "group-private",
                "messageId": "native-message-1",
                "storedAtMs": 1760000000000,
                "expiresAtMs": 1760604800000,
                "encoded": "opaque-native-payload",
            }
        ),
        encoding="utf-8",
    )

    result = inspect_source(path=path, source_class="native")

    record = result["records"][0]
    assert record["chat"] == "group-private"
    assert record["native_id"] == "native-message-1"
    assert record["original_time"] is None
    assert record["creation_time"] == 1760000000000
    assert "opaque-native-payload" not in json.dumps(result)


def test_inventory_includes_static_sidecars_once_and_scans_raw_archive_subdirectories(
    tmp_path: Path,
) -> None:
    db = tmp_path / "data/knowledge/knowledge.db.v1-test"
    db.parent.mkdir(parents=True)
    writer = sqlite3.connect(db)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("CREATE TABLE sample (message_id TEXT)")
    writer.execute("INSERT INTO sample VALUES ('message-1')")
    writer.commit()
    _jsonl(
        tmp_path / "data/raw/whatsapp/2026-06.jsonl",
        json.dumps({"message_id": "raw-1", "timestamp": "2026-06-02T00:00:00Z", "text": "private"}),
    )
    (tmp_path / "data/raw/media").mkdir(parents=True)
    (tmp_path / "data/raw/media/blob.bin").write_bytes(b"media")

    try:
        result = inventory_sources(home=tmp_path)
    finally:
        writer.close()

    db_source = next(source for source in result["sources"] if source["path"] == db.relative_to(tmp_path).as_posix())
    assert {item["path"] for item in db_source["files"]} == {
        db.name,
        f"{db.name}-wal",
        f"{db.name}-shm",
    }
    assert not any(source["path"].endswith(("-wal", "-shm")) for source in result["sources"])
    raw = next(source for source in result["sources"] if source["path"] == "data/raw")
    assert raw["kind"] == "reference_only"
    assert [record["native_id"] for record in raw["records"]] == ["raw-1"]
    media = next(source for source in result["sources"] if source["path"] == "data/raw/media")
    assert media["source_class"] == "media"
    assert result["coverage"]["months"]["2026-06"]["records"] == 1
    assert "private" not in json.dumps(result)
