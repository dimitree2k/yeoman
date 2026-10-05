import sqlite3

import pytest
from yeoman_gateway.history.schema import SCHEMA_VERSION, create


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    create(conn)
    return conn


def _contact(db, cid="c1"):
    db.execute("INSERT INTO contacts VALUES (?, 'person', NULL, 'Frank', 'confirmed', NULL, '[]')", (cid,))


def _message(db, mid="whatsapp:g@g.us:M1", text="hallo"):
    db.execute(
        "INSERT INTO messages VALUES (?, 'whatsapp', 'g@g.us', 'M1', 'c1', '1@lid', 'native_identifier',"
        " 'in', 1000, 'provider_timestamp', ?, NULL, NULL, NULL, 'native', '[]')",
        (mid, text),
    )


def test_tables_and_version(db):
    names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
    assert {"contacts", "identifier_history", "messages", "message_events",
            "projector_state", "messages_current"} <= names
    assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_enums_are_checked(db):
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO contacts VALUES ('x', 'robot', NULL, NULL, 'confirmed', NULL, '[]')")


def test_messages_current_view(db):
    _contact(db)
    _message(db)
    events = [
        ("e1", "edit", '{"text": "hallo!"}', 2000),
        ("e2", "edit", '{"text": "hallo!!"}', 3000),
        ("e3", "reaction", '{"emoji": "😂", "removed": false, "current": true}', 2500),
        ("e4", "reaction", '{"emoji": "👍", "removed": true, "current": false}', 2600),
    ]
    for eid, kind, payload, ms in events:
        db.execute(
            "INSERT INTO message_events (event_id, kind, channel, chat_id, target_message_id, target_native_id,"
            " actor_contact_id, actor_identifier, actor_basis, occurred_ms, time_certainty, payload_json,"
            " provenance, source_refs) VALUES (?, ?, 'whatsapp', 'g@g.us', 'whatsapp:g@g.us:M1', 'M1',"
            " 'c1', '1@lid', 'native_identifier', ?, 'provider_timestamp', ?, 'native', '[]')",
            (eid, kind, ms, payload),
        )
    row = db.execute("SELECT current_text, deleted, reactions FROM messages_current").fetchone()
    assert row[0] == "hallo!!" and row[1] == 0
    assert '"😂"' in row[2] and "👍" not in row[2]


def test_projector_state_defaults_projector_version_to_one(db):
    columns = {row[1] for row in db.execute("PRAGMA table_info(projector_state)")}
    assert "projector_version" in columns
    db.execute("INSERT INTO projector_state (file, lines, sha256) VALUES ('messages.jsonl', 1, 'abc')")
    assert db.execute("SELECT projector_version FROM projector_state").fetchone()[0] == 1


def test_native_event_id_is_nullable_indexed_and_non_unique(db):
    columns = {row[1]: row[3] for row in db.execute("PRAGMA table_info(message_events)")}
    assert columns["native_event_id"] == 0
    indexes = {row[1]: row[2] for row in db.execute("PRAGMA index_list(message_events)")}
    assert indexes["message_events_native_event"] == 0
    _contact(db)
    _message(db)
    db.execute(
        "INSERT INTO message_events (event_id, kind, channel, chat_id, target_message_id, target_native_id,"
        " actor_contact_id, actor_identifier, actor_basis, occurred_ms, time_certainty, payload_json,"
        " provenance, source_refs, native_event_id) VALUES"
        " ('e1', 'edit', 'whatsapp', 'g@g.us', 'whatsapp:g@g.us:M1', 'M1', 'c1', '1@lid',"
        " 'native_identifier', 2000, 'provider_timestamp', '{}', 'native', '[]', 'P1'),"
        " ('e2', 'edit', 'whatsapp', 'g@g.us', 'whatsapp:g@g.us:M1', 'M1', 'c1', '1@lid',"
        " 'native_identifier', 3000, 'provider_timestamp', '{}', 'native', '[]', 'P1')"
    )
    db.execute(
        "INSERT INTO message_events (event_id, kind, channel, chat_id, target_native_id, actor_basis,"
        " time_certainty, payload_json, provenance, source_refs)"
        " VALUES ('e3', 'delete', 'whatsapp', 'g@g.us', 'M1', 'unknown', 'unknown', '{}', 'native', '[]')"
    )
    assert db.execute("SELECT count(*) FROM message_events WHERE native_event_id = 'P1'").fetchone()[0] == 2
