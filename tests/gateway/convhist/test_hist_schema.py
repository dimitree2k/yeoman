import sqlite3

import pytest
from yeoman_gateway.history.schema import PROJECTOR_VERSION, SCHEMA_VERSION, create


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


def test_projector_state_requires_explicit_version_and_valid_json(db):
    columns = {row[1] for row in db.execute("PRAGMA table_info(projector_state)")}
    assert columns == {"file", "lines", "end_offset", "sha256", "projector_version", "state_json"}
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO projector_state (file, lines, end_offset, sha256, state_json)"
                   " VALUES ('messages.jsonl', 1, 10, 'abc', '{}')")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO projector_state VALUES ('messages.jsonl', 1, 10, 'abc', 3, 'bad')")
    db.execute("INSERT INTO projector_state VALUES ('messages.jsonl', 1, 10, 'abc', 3, '{}')")
    assert db.execute("SELECT projector_version FROM projector_state").fetchone()[0] == 3


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


def test_identifier_schema_distinguishes_ownership_from_observation(db):
    columns = {row[1]: row[3] for row in db.execute("PRAGMA table_info(identifier_history)")}
    assert columns.get("valid_from_ms") == 0 and columns.get("valid_until_ms") == 0
    assert SCHEMA_VERSION == 3
    assert PROJECTOR_VERSION == 3
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert tables == {"contacts", "identifier_history", "messages", "message_events", "projector_state"}
    _contact(db)
    sql = ("INSERT INTO identifier_history (contact_id, channel, kind, value, strength, evidence,"
           " first_seen_ms, last_seen_ms, valid_from_ms, valid_until_ms, source_refs)"
           " VALUES ('c1', 'whatsapp', 'pn_jid', '2@s.whatsapp.net', 'strong', 'owner_attested',"
           " 150, 450, ?, ?, '[]')")
    for bounds in ((100, 200), (300, 400), (None, 100), (400, None), (None, None), (0, None)):
        db.execute(sql, bounds)
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(sql, bounds)
    assert db.execute("SELECT first_seen_ms, last_seen_ms, valid_from_ms, valid_until_ms"
                      " FROM identifier_history ORDER BY id").fetchall() == [
        (150, 450, 100, 200), (150, 450, 300, 400), (150, 450, None, 100),
        (150, 450, 400, None), (150, 450, None, None), (150, 450, 0, None)]
    for bounds in ((200, 100), (100, 100), (1.5, 200), (100, "invalid")):
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(sql, bounds)
    old = sqlite3.connect(":memory:")
    try:
        old.executescript("CREATE TABLE contacts (contact_id TEXT);"
                          "INSERT INTO contacts VALUES ('keep'); PRAGMA user_version = 1;")
        before = list(old.iterdump())
        with pytest.raises(ValueError, match="rebuild"):
            create(old)
        assert list(old.iterdump()) == before
    finally:
        old.close()


def test_history_v2_requires_rebuild(tmp_path):
    from yeoman_gateway.history.incremental import RebuildRequired
    from yeoman_gateway.history.verify import table_digest

    path = tmp_path / "v2.db"
    with sqlite3.connect(path) as old:
        old.executescript("CREATE TABLE contacts (contact_id TEXT);"
                         "INSERT INTO contacts VALUES ('synthetic-keep'); PRAGMA user_version = 2;")
    before = path.read_bytes()
    with sqlite3.connect(path) as old:
        with pytest.raises(RebuildRequired, match="history schema 2 requires rebuild"):
            create(old)
    with pytest.raises(RebuildRequired, match="history schema 2 requires rebuild"):
        table_digest(path)
    assert path.read_bytes() == before
    assert not path.with_name(path.name + ".building").exists()
