import json
import sqlite3

import pytest
from hist_fixtures import FRANK_LID, T0, _bf, _raw, sample_layer1, write_jsonl
from hist_fixtures import SAMPLE_GROUP as G
from yeoman_gateway.history.project import project


@pytest.fixture
def built(tmp_path):
    live, dev = sample_layer1(tmp_path)
    report = project([live, dev], tmp_path / "out" / "history.db")
    conn = sqlite3.connect(tmp_path / "out" / "history.db")
    conn.row_factory = sqlite3.Row
    arvid = conn.execute("SELECT contact_id FROM contacts WHERE role = 'assistant'").fetchone()[0]
    return report, conn, arvid, (live, dev, tmp_path)


def test_frank_one_message_three_copies(built):
    _, conn, _, _ = built
    row = conn.execute("SELECT * FROM messages WHERE native_message_id = 'AC1'").fetchone()
    assert json.loads(row["source_refs"]) == [
        "backfill/memory.jsonl#1", "backfill/reply_context.jsonl#1", "whatsapp/2026-10.jsonl#1"]
    assert (row["text"], row["provenance"], row["sender_basis"], row["sender_identifier"]) == (
        "hallo", "native", "native_identifier", FRANK_LID)
    assert row["sender_contact_id"] == "945ae43e" and row["time_certainty"] == "provider_timestamp"
    feb = conn.execute("SELECT * FROM messages WHERE text = 'Februar-Nachricht'").fetchone()
    assert feb["sender_contact_id"] == "945ae43e" and feb["sender_basis"] == "derived_claim"
    assert feb["message_id"].startswith(f"whatsapp:{G}:derived:")
    assert feb["provenance"] == "verbatim_unverified"


def test_reaction_echo_is_one_arvid_event(built):
    _, conn, arvid, _ = built
    rows = conn.execute("SELECT * FROM message_events WHERE kind = 'reaction'").fetchall()
    assert len(rows) == 1
    assert rows[0]["actor_contact_id"] == arvid
    assert rows[0]["actor_identifier"] == G and rows[0]["actor_basis"] == "reaction_echo"
    assert rows[0]["target_message_id"] == f"whatsapp:{G}:AC1"
    assert json.loads(rows[0]["source_refs"]) == ["whatsapp/2026-10.jsonl#2", "whatsapp/2026-10.jsonl#4"]
    assert json.loads(rows[0]["payload_json"])["current"] is True


def test_edits_and_current_text(built):
    _, conn, _, _ = built
    assert conn.execute("SELECT count(*) FROM message_events WHERE kind = 'edit'").fetchone()[0] == 2
    current = conn.execute("SELECT current_text FROM messages_current WHERE native_message_id = 'AC1'")
    assert current.fetchone()[0] == "hallo!!"


def test_purged_outgoing_recovered_and_all_outgoing_is_arvid(built):
    _, conn, arvid, _ = built
    row = conn.execute("SELECT * FROM messages WHERE native_message_id = '3EB0P'").fetchone()
    assert row["text"] == "Antwort an Matthias" and row["direction"] == "out"
    assert row["sender_contact_id"] == arvid and row["provenance"] == "verbatim_unverified"
    assert len(json.loads(row["source_refs"])) == 2
    others = conn.execute("SELECT count(*) FROM messages WHERE direction = 'out' AND sender_contact_id IS NOT ?",
                          (arvid,))
    assert others.fetchone()[0] == 0
    matthias = conn.execute("SELECT status FROM contacts WHERE display_name = 'Matthias Hoffmann'")
    assert matthias.fetchone()[0] == "confirmed"


def test_description_is_media_not_text(built):
    _, conn, _, _ = built
    row = conn.execute("SELECT text, media_json, sender_basis FROM messages WHERE native_message_id = 'AC2'").fetchone()
    media = json.loads(row["media_json"])
    assert row["text"] is None and media["kind"] == "image"
    assert media["description"]["text"] == "Eine Stahlbrücke" and row["sender_basis"] == "derived_claim"


def test_accounting(built):
    report, _, _, _ = built
    assert report["accounting_ok"] is True
    raw = report["outcomes"]["whatsapp/2026-10.jsonl"]
    assert raw["invalid_json"] == 1 and raw["skipped:receipt"] == 1
    assert raw["skipped:outbound_result_paired"] == 1
    session = report["outcomes"]["backfill/session_jsonl.jsonl"]
    assert session["out_of_scope_channel"] == 1 and session["skipped:tool_trace"] == 1


def _dump(path):
    conn = sqlite3.connect(path)
    tables = {t: conn.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall()
              for t in ("contacts", "messages", "message_events")}
    tables["identifier_history"] = conn.execute(
        "SELECT contact_id, kind, value, evidence, source_refs FROM identifier_history ORDER BY 1, 2, 3").fetchall()
    return tables


def test_rebuild_identical_and_protected_refused(built, monkeypatch):
    _, _, _, (live, dev, tmp_path) = built
    project([live, dev], tmp_path / "again.db")
    assert _dump(tmp_path / "out" / "history.db") == _dump(tmp_path / "again.db")
    monkeypatch.setenv("YEOMAN_HOME", str(tmp_path / "home"))
    with pytest.raises(PermissionError):
        project([live, dev], tmp_path / "home" / "data" / "raw" / "history.db")


def test_native_event_ids_do_not_identify_revisions(tmp_path):
    live, dev = tmp_path / "live", tmp_path / "dev"
    write_jsonl(live / "whatsapp/events.jsonl", [
        _raw("edit", "edit", {"chatJid": G, "messageId": "AC1", "participantJid": FRANK_LID,
                              "text": "one", "timestamp": T0 // 1000, "nativeEventId": "reused"}),
        _raw("edit", "edit", {"chatJid": G, "messageId": "AC1", "participantJid": FRANK_LID,
                              "text": "two", "timestamp": T0 // 1000 + 1, "nativeEventId": "reused"}),
    ])
    report = project([live, dev], tmp_path / "history.db")
    conn = sqlite3.connect(tmp_path / "history.db")
    rows = conn.execute("SELECT event_id, native_event_id, payload_json FROM message_events ORDER BY occurred_ms").fetchall()
    assert report["events"] == 2
    assert [row[1] for row in rows] == ["reused", "reused"]
    assert len({row[0] for row in rows}) == 2 and all(row[0] != "reused" for row in rows)


def test_event_native_id_uses_first_source_ranked_copy(tmp_path):
    live, dev = tmp_path / "live", tmp_path / "dev"
    write_jsonl(live / "whatsapp/events.jsonl", [
        _raw("edit", "edit", {"chatJid": G, "messageId": "AC1", "participantJid": FRANK_LID,
                              "text": "same", "timestamp": T0 // 1000, "nativeEventId": "raw-id"}),
    ])
    write_jsonl(dev / "backfill/journal.jsonl", [
        _bf("journal", "edit", {"chatJid": G, "messageId": "AC1", "participantJid": FRANK_LID,
                                 "text": "same", "nativeEventId": "journal-id"}, ms=T0 + 1000),
    ])
    project([live, dev], tmp_path / "history.db")
    conn = sqlite3.connect(tmp_path / "history.db")
    (row,) = conn.execute("SELECT native_event_id, source_refs FROM message_events").fetchall()
    assert row[0] == "raw-id"
    assert json.loads(row[1]) == ["backfill/journal.jsonl#1", "whatsapp/events.jsonl#1"]


def test_purged_event_payload_joins_only_one_complete_candidate(tmp_path):
    live, dev = tmp_path / "live", tmp_path / "dev"
    write_jsonl(live / "whatsapp/events.jsonl", [
        _raw("edit", "edit", {"chatJid": G, "messageId": "AC1", "participantJid": FRANK_LID,
                              "text": "complete", "timestamp": T0 // 1000}),
        _raw("edit", "edit", {"chatJid": G, "messageId": "AC1", "participantJid": FRANK_LID,
                              "timestamp": T0 // 1000 + 1}),
        _raw("edit", "edit", {"chatJid": G, "messageId": "AC2", "participantJid": FRANK_LID,
                              "timestamp": T0 // 1000}),
    ])
    report = project([live, dev], tmp_path / "history.db")
    conn = sqlite3.connect(tmp_path / "history.db")
    rows = conn.execute("SELECT target_native_id, payload_json, source_refs FROM message_events ORDER BY target_native_id").fetchall()
    assert len(rows) == 2 and report["events"] == 2
    assert rows[0][0] == "AC1" and json.loads(rows[0][1])["text"] == "complete"
    assert json.loads(rows[0][2]) == ["whatsapp/events.jsonl#1", "whatsapp/events.jsonl#2"]
    assert rows[1][0] == "AC2" and json.loads(rows[1][1])["text"] is None
    assert report["review"]["unmatched_event_payloads"] == ["whatsapp/events.jsonl#3"]


def test_ambiguous_purged_event_is_retained_for_review(tmp_path):
    live, dev = tmp_path / "live", tmp_path / "dev"
    write_jsonl(live / "whatsapp/events.jsonl", [
        _raw("edit", "edit", {"chatJid": G, "messageId": "AC1", "participantJid": FRANK_LID,
                              "text": "revision one", "timestamp": T0 // 1000}),
        _raw("edit", "edit", {"chatJid": G, "messageId": "AC1", "participantJid": FRANK_LID,
                              "text": "revision two", "timestamp": T0 // 1000 + 1}),
        _raw("edit", "edit", {"chatJid": G, "messageId": "AC1", "participantJid": FRANK_LID,
                              "timestamp": T0 // 1000 + 2}),
    ])
    report = project([live, dev], tmp_path / "history.db")
    conn = sqlite3.connect(tmp_path / "history.db")
    rows = conn.execute("SELECT payload_json, source_refs FROM message_events ORDER BY occurred_ms").fetchall()
    assert len(rows) == 3 and report["events"] == 3
    assert report["review"]["unmatched_event_payloads"] == ["whatsapp/events.jsonl#3"]
    assert json.loads(rows[-1][0])["text"] is None
    assert json.loads(rows[-1][1]) == ["whatsapp/events.jsonl#3"]


def test_explicit_reaction_removal_is_not_treated_as_purged_payload(tmp_path):
    live, dev = tmp_path / "live", tmp_path / "dev"
    write_jsonl(live / "whatsapp/events.jsonl", [
        _raw("reaction", "reaction", {"chatJid": G, "targetMessageId": "AC1", "senderId": FRANK_LID,
                                      "emoji": "😂", "removed": False, "timestamp": T0 // 1000}),
        _raw("reaction", "reaction", {"chatJid": G, "targetMessageId": "AC1", "senderId": FRANK_LID,
                                      "emoji": None, "removed": True, "timestamp": T0 // 1000 + 1}),
    ])
    project([live, dev], tmp_path / "history.db")
    conn = sqlite3.connect(tmp_path / "history.db")
    rows = conn.execute("SELECT payload_json FROM message_events WHERE kind = 'reaction'").fetchall()
    assert len(rows) == 2
    assert {json.loads(row[0])["removed"] for row in rows} == {False, True}


def test_events_without_times_do_not_merge_by_payload_alone(tmp_path):
    live, dev = tmp_path / "live", tmp_path / "dev"
    record = {"chatJid": G, "messageId": "AC1", "participantJid": FRANK_LID, "text": "same"}
    write_jsonl(live / "whatsapp/events.jsonl", [
        _raw("edit", "edit", record, received=0),
        _raw("edit", "edit", record, received=0),
    ])
    report = project([live, dev], tmp_path / "history.db")
    conn = sqlite3.connect(tmp_path / "history.db")
    rows = conn.execute("SELECT source_refs FROM message_events").fetchall()
    assert report["events"] == 2
    assert {tuple(json.loads(row[0])) for row in rows} == {
        ("whatsapp/events.jsonl#1",), ("whatsapp/events.jsonl#2",)}
