import json
import sqlite3

import pytest
from yeoman_gateway.history.reader import HistorySnapshot
from yeoman_gateway.history.schema import create


def message(db, mid, *, chat="g@g.us", ms=100, text="original", channel="whatsapp",
            sender="a", identifier="10001@s.whatsapp.net", media=None, provenance="native"):
    db.execute(
        "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, 'native_identifier', 'in', ?,"
        " 'native', ?, ?, NULL, NULL, ?, '[]')",
        (mid, channel, chat, mid, sender, identifier, ms, text,
         json.dumps(media) if media else None, provenance),
    )


def event(db, eid, kind, ms, payload, *, target=None, chat="g@g.us", certainty="native"):
    db.execute(
        "INSERT INTO message_events (event_id,kind,channel,chat_id,target_message_id,"
        "actor_basis,occurred_ms,time_certainty,payload_json,provenance,source_refs)"
        " VALUES (?,?,'whatsapp',?,?,'unknown',?, ?,?,'native','[]')",
        (eid, kind, chat, target, ms, certainty, json.dumps(payload)),
    )


def contact(db, cid, redirect=None, role=None):
    db.execute("INSERT INTO contacts VALUES (?, 'person', ?, ?, 'confirmed', ?, '[]')",
               (cid, role, cid, redirect))


def identifier(db, cid, value, *, start=None, end=None, strength="strong", kind=None):
    from yeoman_gateway.history.ids import classify
    db.execute(
        "INSERT INTO identifier_history (contact_id,channel,kind,value,strength,evidence,"
        "valid_from_ms,valid_until_ms,source_refs) VALUES (?,'whatsapp',?,?,?,'owner_attested',?,?,'[]')",
        (cid, kind or classify(value).kind, value, strength, start, end),
    )


@pytest.fixture
def case():
    db = sqlite3.connect(":memory:")
    create(db)
    contact(db, "a")
    contact(db, "b")
    snapshot = HistorySnapshot(1, (), db)
    yield db, snapshot
    snapshot.close()


def queries(snapshot):
    from yeoman_gateway.history.queries import HistoryQueries
    # Direct SQL fixtures bypass the projector; populate their derived index explicitly.
    snapshot.connection.execute("DELETE FROM messages_fts")
    snapshot.connection.execute(
        "INSERT INTO messages_fts(message_id,chat_id,text) SELECT message_id,chat_id,current_text"
        " FROM messages_current WHERE deleted=0 AND current_text IS NOT NULL AND current_text!=''")
    return HistoryQueries(snapshot)


def test_recent_reply_search_media_use_current_rows(case):
    db, snapshot = case
    message(db, "a", ms=None)
    message(db, "b", ms=100)
    message(db, "c", ms=100, media={"transcript": "derived words"}, provenance="derived_only")
    message(db, "d", ms=200)
    message(db, "e", ms=300)
    message(db, "foreign", chat="other@g.us", text="edited")
    message(db, "telegram", channel="telegram")
    event(db, "edit", "edit", 400, {"text": "edited"}, target="c")
    event(db, "delete", "delete", 400, {}, target="d")
    event(db, "r2", "reaction", 401, {"emoji": "two", "current": True}, target="c")
    event(db, "r1", "reaction", 401, {"emoji": "one", "current": True}, target="c")
    q = queries(snapshot)
    rows = q.recent(chat_id="g@g.us", limit=3)
    assert [r["message_id"] for r in rows] == ["b", "c", "e"]
    assert rows[1]["current_text"] == "edited"
    assert rows[1]["provenance"] == "derived_only"
    assert json.loads(rows[1]["media_json"]) == {"transcript": "derived words"}
    assert [r["emoji"] for r in json.loads(rows[1]["reactions"])] == ["one", "two"]
    assert [r["message_id"] for r in q.reply_window(chat_id="g@g.us", native_id="c", before=1, after=1)] == ["b", "c", "e"]
    assert [r["message_id"] for r in q.recent(chat_id="g@g.us", limit=2, before_id="c")] == ["a", "b"]
    assert [r["message_id"] for r in q.search(chat_ids=("g@g.us",), query="edited", limit=10)] == ["c"]
    assert q.media(chat_id="g@g.us", limit=10, native_id="c")[0]["current_text"] == "edited"
    assert q.media(chat_id="other@g.us", limit=10) == []
    assert q.message("d") is None and q.message("telegram") is None
    assert q.native_message(chat_id="other@g.us", native_id="c") is None
    assert [r["message_id"] for r in q.window(chat_id="g@g.us", after_ms=99, before_ms=200, limit=10)] == ["b", "c"]
    assert "a" not in [r["message_id"] for r in q.recent(chat_id="g@g.us", limit=10, after_ms=0)]
    event(db, "subject", "group_subject", 50, {"subject": "synthetic"})
    event(db, "description", "group_description", 60, {"description": "description"})
    chats = {r["chat_id"]: r for r in q.chats()}
    assert chats["g@g.us"]["subject"] == "synthetic"
    assert chats["g@g.us"]["description"] == "description"
    assert all(r["channel"] == "whatsapp" for r in chats.values())


def test_query_scope_limit_and_sql_literals(case):
    db, snapshot = case
    message(db, "literal", text="100%_\\")
    q = queries(snapshot)
    assert q.search(chat_ids=("g@g.us",), query="100", limit=1)[0]["message_id"] == "literal"
    assert q.search(chat_ids=("g@g.us",), query="%_\\", limit=1) == []
    assert q.search(chat_ids=("g@g.us",), query="' OR 1=1 --", limit=1) == []
    assert q.search(chat_ids=("g@g.us' OR 1=1 --",), query="", limit=1) == []
    assert q.search(chat_ids=(), query="", limit=1) == []
    statements = []
    db.set_trace_callback(statements.append)
    for bad in (True, False, -1, 0, 501, 1.5, "2"):
        for call in (
            lambda: q.recent(chat_id="g@g.us", limit=bad),
            lambda: q.window(chat_id="g@g.us", after_ms=0, before_ms=100, limit=bad),
            lambda: q.search(chat_ids=("g@g.us",), query="", limit=bad),
            lambda: q.media(chat_id="g@g.us", limit=bad),
        ):
            with pytest.raises(ValueError):
                call()
    for bad in (True, 1.5, "2"):
        with pytest.raises(ValueError):
            q.recent(chat_id="g@g.us", limit=1, after_ms=bad)
        with pytest.raises(ValueError):
            q.window(chat_id="g@g.us", after_ms=bad, before_ms=100, limit=1)
    for before, after in ((True, 0), (-1, 0), (500, 1), (0, 501)):
        with pytest.raises(ValueError):
            q.reply_window(chat_id="g@g.us", native_id="literal", before=before, after=after)
    assert statements == []
    db.execute("PRAGMA query_only=ON")
    assert db.execute("PRAGMA query_only").fetchone()[0] == 1
    assert q.message("literal")
    with pytest.raises(sqlite3.OperationalError):
        db.execute("DELETE FROM messages")


def test_v3_reader_indexes_require_rebuild(case):
    from yeoman_gateway.history.incremental import ProjectionIndex, RebuildRequired, apply_committed
    from yeoman_gateway.history.schema import PROJECTOR_VERSION, SCHEMA_VERSION
    from yeoman_gateway.history.verify import _table_digest

    db, _ = case
    message(db, "m")
    db.commit()
    before = _table_digest(db)
    assert SCHEMA_VERSION == PROJECTOR_VERSION == 4
    for name, columns in (
        ("messages_chat_time", ["channel", "chat_id", "sent_ms", "message_id"]),
        ("message_events_chat_time", ["channel", "chat_id", "kind", "occurred_ms", "event_id"]),
    ):
        assert [r[2] for r in db.execute(f"PRAGMA index_info({name})")] == columns
    db.execute("PRAGMA user_version=3")
    with pytest.raises(RebuildRequired, match="schema"):
        apply_committed(db, ProjectionIndex(), None, ())
    with pytest.raises(RebuildRequired):
        create(db)
    assert _table_digest(db) == before
    rebuilt = sqlite3.connect(":memory:")
    create(rebuilt)
    for table in ("contacts", "identifier_history", "messages", "message_events"):
        for row in db.execute(f"SELECT * FROM {table}"):
            rebuilt.execute(f"INSERT INTO {table} VALUES ({','.join('?' for _ in row)})", row)
    assert _table_digest(rebuilt) == before
    assert rebuilt.execute("PRAGMA user_version").fetchone()[0] == 4
    rebuilt.close()


def test_fts_search_ranks_terms_and_ignores_fts_syntax(case):
    db, snapshot = case
    message(db, "strong", text="Haus Haus Haus grüße", ms=100)
    message(db, "weak", text="Haus grüße viele andere lange Wörter im Garten", ms=200)
    message(db, "tie-a", text="Gleichstand", ms=300)
    message(db, "tie-b", text="Gleichstand", ms=300)
    message(db, "tie-old", text="Gleichstand", ms=200)
    message(db, "foreign", text="Haus grüße", chat="other@g.us")
    message(db, "deleted", text="Haus grüße")
    message(db, "syntax", text="OR NOT NEAR text haus")
    event(db, "delete", "delete", 500, {}, target="deleted")
    q = queries(snapshot)
    assert [r["message_id"] for r in q.search(chat_ids=("g@g.us",), query="HAU gruße", limit=10)] == ["strong", "weak"]
    assert [r["message_id"] for r in q.search(chat_ids=("g@g.us",), query="gleich", limit=10)] == ["tie-a", "tie-b", "tie-old"]
    for text in ('"haus" OR NOT NEAR text:haus', 'NEAR(haus OR, 3)', 'haus NOT missing'):
        expected = ["syntax"] if text.startswith('"haus"') else []
        assert [r["message_id"] for r in q.search(chat_ids=("g@g.us",), query=text, limit=10)] == expected
    assert q.search(chat_ids=("g@g.us",), query="*** : ()", limit=10) == []
    assert [r["message_id"] for r in q.search(chat_ids=("other@g.us",), query="haus", limit=1)] == ["foreign"]
    assert [r["message_id"] for r in q.search(chat_ids=("g@g.us",), query="grüße", limit=1, after_ms=100, before_ms=201)] == ["weak"]
