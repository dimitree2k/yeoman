import json
import sqlite3
from contextlib import closing

import pytest
from hist_fixtures import FRANK_LID, FRANK_PN, T0, _bf, _raw, sample_layer1, write_jsonl
from hist_fixtures import SAMPLE_GROUP as G
from yeoman_gateway.history.attestations import make
from yeoman_gateway.history.ids import classify
from yeoman_gateway.history.project import project


@pytest.fixture
def built(tmp_path):
    live, dev = sample_layer1(tmp_path)
    report = project([live, dev], tmp_path / "out" / "history.db")
    conn = sqlite3.connect(tmp_path / "out" / "history.db")
    conn.row_factory = sqlite3.Row
    arvid = conn.execute("SELECT contact_id FROM contacts WHERE role = 'assistant'").fetchone()[0]
    try:
        yield report, conn, arvid, (live, dev, tmp_path)
    finally:
        conn.close()


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


def test_batch_copies_merge_by_native_anchor_and_repeated_text_stays_separate(tmp_path):
    def record(native_id):
        return _bf("memory", "message", {
            "chatJid": G, "messageId": native_id, "segments": [
                {"senderId": "4915550000000@s.whatsapp.net", "text": "same words"},
                {"senderId": "4915550000000@s.whatsapp.net", "text": "tail", "messageId": native_id},
            ],
        }, provenance="verbatim_unverified")

    live, dev = tmp_path / "live", tmp_path / "dev"
    write_jsonl(dev / "owner/attestations.jsonl", [make(
        "contact", 1, "test identity", identifiers=["4915550000000@s.whatsapp.net"]
    )])
    write_jsonl(dev / "backfill/memory.jsonl", [record("LAST-A")])
    write_jsonl(dev / "backfill/knowledge_memory.jsonl", [record("LAST-A"), record("LAST-B")])
    report = project([live, dev], tmp_path / "history.db")
    with closing(sqlite3.connect(tmp_path / "history.db")) as conn:
        conn.row_factory = sqlite3.Row
        repeats = conn.execute("SELECT * FROM messages WHERE text = 'same words' ORDER BY message_id").fetchall()
        assert len(repeats) == 2 and repeats[0]["message_id"] != repeats[1]["message_id"]
        assert sorted(len(json.loads(row["source_refs"])) for row in repeats) == [1, 2]
        assert all(row["native_message_id"] is None and row["sender_basis"] == "derived_claim" for row in repeats)
        assert report["messages"] == 4 and report["accounting_ok"]


def test_unanchored_batch_segments_stay_separate_from_native_text_candidate(tmp_path):
    live, dev = tmp_path / "live", tmp_path / "dev"
    write_jsonl(dev / "backfill/memory.jsonl", [_bf("memory", "message", {
        "chatJid": G, "segments": [
            {"senderId": "4915550000000@s.whatsapp.net", "text": "repeated"},
            {"senderId": "4915550000000@s.whatsapp.net", "text": "repeated"},
        ],
    }, provenance="verbatim_unverified")])
    write_jsonl(live / "whatsapp/2026-10.jsonl", [_raw(
        "message", "message", {"chatJid": G, "messageId": "UNRELATED", "senderId": "4915550000000@s.whatsapp.net",
                                  "text": "repeated"})])

    report = project([live, dev], tmp_path / "history.db")
    with closing(sqlite3.connect(tmp_path / "history.db")) as conn:
        rows = conn.execute("SELECT native_message_id, source_refs FROM messages WHERE text = 'repeated'").fetchall()
        assert len(rows) == 3
        assert sum(native_id == "UNRELATED" for native_id, _ in rows) == 1
        assert {refs for _, refs in rows if "memory.jsonl" in refs} == {
            '["backfill/memory.jsonl#1/0"]', '["backfill/memory.jsonl#1/1"]'}
        assert report["messages"] == 3 and report["accounting_ok"]


def test_accounting(built):
    report, _, _, _ = built
    assert report["accounting_ok"] is True
    raw = report["outcomes"]["whatsapp/2026-10.jsonl"]
    assert raw["invalid_json"] == 1 and raw["skipped:receipt"] == 1
    assert raw["skipped:outbound_result_paired"] == 1
    session = report["outcomes"]["backfill/session_jsonl.jsonl"]
    assert session["out_of_scope_channel"] == 1 and session["skipped:tool_trace"] == 1


def test_group_only_message_is_retained_with_unknown_sender(tmp_path):
    live = tmp_path / "live"
    write_jsonl(live / "whatsapp/2026-10.jsonl", [
        _raw("message", "message", {"chatJid": G, "messageId": "AC-group", "senderId": G,
                                     "text": "preserved", "timestamp": T0 // 1000}),
    ])
    report = project([live], tmp_path / "history.db")
    with closing(sqlite3.connect(tmp_path / "history.db")) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM messages WHERE native_message_id = 'AC-group'").fetchone()

        assert report["messages"] == 1
        assert row["text"] == "preserved" and row["sender_contact_id"] is None
        assert row["sender_identifier"] is None and row["sender_basis"] == "unknown"


def _dump(path):
    with closing(sqlite3.connect(path)) as conn:
        tables = {t: conn.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall()
                  for t in ("contacts", "messages", "message_events")}
        tables["identifier_history"] = conn.execute(
            "SELECT contact_id, channel, kind, value, strength, evidence, source_refs,"
            " valid_from_ms, valid_until_ms, ended_ms, first_seen_ms, last_seen_ms"
            " FROM identifier_history ORDER BY 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12").fetchall()
        return tables


def test_rebuild_identical_and_protected_refused(built, monkeypatch):
    _, _, _, (live, dev, tmp_path) = built
    project([live, dev], tmp_path / "again.db")
    assert _dump(tmp_path / "out" / "history.db") == _dump(tmp_path / "again.db")
    # Persistent determinism also covers temporal validity, ends and observations.
    temporal = tmp_path / "temporal"
    phone = "491100000003@s.whatsapp.net"
    write_jsonl(temporal / "owner/attestations.jsonl", [
        make("identifier", 1, "window", anchor=FRANK_PN, identifier=phone,
             valid_from_ms=100, valid_until_ms=300),
        make("identifier_ended", 2, "end", identifier=phone, ended_ms=200),
    ])
    write_jsonl(temporal / "backfill/journal.jsonl", [
        _bf("journal", "message", {"messageId": "T", "senderId": phone, "text": "temporal"},
            ms=150, certainty="provider_timestamp")])
    temporal_db = tmp_path / "temporal.db"
    project([temporal], temporal_db)
    project([temporal], tmp_path / "temporal-again.db")
    expected = _dump(temporal_db)
    assert expected == _dump(tmp_path / "temporal-again.db")
    # Each temporal field must participate in the comparison.
    with closing(sqlite3.connect(temporal_db)) as conn:
        for field in ("valid_from_ms", "valid_until_ms", "ended_ms", "first_seen_ms", "last_seen_ms"):
            conn.execute(f"UPDATE identifier_history SET {field} = {field} + 1 WHERE value = ?", (phone,))
            conn.commit()
            assert _dump(temporal_db) != expected, field
            conn.execute(f"UPDATE identifier_history SET {field} = {field} - 1 WHERE value = ?", (phone,))
            conn.commit()
    monkeypatch.setenv("YEOMAN_HOME", str(tmp_path / "home"))
    with pytest.raises(PermissionError):
        project([live, dev], tmp_path / "home" / "data" / "raw" / "history.db")


def test_dangling_protected_building_symlink_is_refused_without_touching_output(
    tmp_path, monkeypatch
):
    live, dev = sample_layer1(tmp_path)
    output = tmp_path / "out" / "history.db"
    project([live, dev], output)
    original = output.read_bytes()
    monkeypatch.setenv("YEOMAN_HOME", str(tmp_path / "home"))
    protected_target = tmp_path / "home" / "data" / "raw" / "must-not-exist.db"
    building = output.with_name(output.name + ".building")
    building.symlink_to(protected_target)

    with pytest.raises(PermissionError):
        project([live, dev], output)

    assert output.read_bytes() == original
    assert building.is_symlink() and not protected_target.exists()


def test_existing_building_file_is_refused_and_preserved(tmp_path):
    live, dev = sample_layer1(tmp_path)
    output = tmp_path / "out" / "history.db"
    output.parent.mkdir()
    building = output.with_name(output.name + ".building")
    building.write_bytes(b"owned by another invocation")

    with pytest.raises(FileExistsError):
        project([live, dev], output)

    assert building.read_bytes() == b"owned by another invocation"


def test_failed_rebuild_preserves_previous_db_and_removes_owned_stage(tmp_path, monkeypatch):
    live, dev = sample_layer1(tmp_path)
    output = tmp_path / "history.db"
    project([live, dev], output)
    original = output.read_bytes()

    def fail_create(_conn):
        raise RuntimeError("synthetic schema failure")

    monkeypatch.setattr("yeoman_gateway.history.project.create", fail_create)
    with pytest.raises(RuntimeError, match="synthetic schema failure"):
        project([live, dev], output)

    assert output.read_bytes() == original
    assert not output.with_name(output.name + ".building").exists()


def test_producer_membership_snapshot_journal_copy_deduplicates_with_raw(tmp_path):
    from yeoman_gateway.history.convert.journal import _event
    from yeoman_gateway.processing.signals import WhatsAppSignalMapper

    participants = [{"lid": FRANK_LID, "phoneJid": FRANK_PN, "admin": True}]
    signal = WhatsAppSignalMapper().map(
        {"chatJid": G, "snapshotAtMs": T0, "complete": True, "memberCount": 1,
         "participants": participants},
        kind="membership_snapshot", event_key="snapshot", observed_at_ms=T0, strict=True,
    )
    assert signal is not None
    journal_line = _event({
        "event_id": signal.event_id, "kind": signal.kind, "channel": "whatsapp", "chat_id": G,
        "principal": "", "direction": "in", "occurred_ms": T0,
        "created_ms": T0, "payload_json": json.dumps(signal.to_event_payload()),
    })
    live, dev = tmp_path / "live", tmp_path / "dev"
    write_jsonl(live / "whatsapp/2026-10.jsonl", [
        _raw("membership_snapshot", "membership_snapshot", {
            "chatJid": G, "snapshotAtMs": T0, "complete": True,
            "participants": participants, "timestamp": T0,
        })
    ])
    write_jsonl(dev / "backfill/journal.jsonl", [journal_line])

    report = project([live, dev], tmp_path / "history.db")
    with closing(sqlite3.connect(tmp_path / "history.db")) as conn:
        row = conn.execute("SELECT payload_json, source_refs FROM message_events").fetchone()
        pair = conn.execute(
            "SELECT count(DISTINCT contact_id) FROM identifier_history WHERE value IN (?,?)",
            (FRANK_LID, FRANK_PN),
        ).fetchone()[0]

        assert report["events"] == 1
        assert json.loads(row[1]) == ["backfill/journal.jsonl#1", "whatsapp/2026-10.jsonl#1"]
        assert json.loads(row[0])["participants"] == [[FRANK_LID, FRANK_PN]]
        assert pair == 1


def test_native_event_ids_do_not_identify_revisions(tmp_path):
    live, dev = tmp_path / "live", tmp_path / "dev"
    write_jsonl(live / "whatsapp/events.jsonl", [
        _raw("edit", "edit", {"chatJid": G, "messageId": "AC1", "participantJid": FRANK_LID,
                              "text": "one", "timestamp": T0 // 1000, "nativeEventId": "reused"}),
        _raw("edit", "edit", {"chatJid": G, "messageId": "AC1", "participantJid": FRANK_LID,
                              "text": "two", "timestamp": T0 // 1000 + 1, "nativeEventId": "reused"}),
    ])
    report = project([live, dev], tmp_path / "history.db")
    with closing(sqlite3.connect(tmp_path / "history.db")) as conn:
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
    with closing(sqlite3.connect(tmp_path / "history.db")) as conn:
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
    with closing(sqlite3.connect(tmp_path / "history.db")) as conn:
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
    with closing(sqlite3.connect(tmp_path / "history.db")) as conn:
        rows = conn.execute("SELECT payload_json, source_refs FROM message_events ORDER BY occurred_ms").fetchall()
        assert len(rows) == 3 and report["events"] == 3
        assert report["review"]["unmatched_event_payloads"] == ["whatsapp/events.jsonl#3"]
        assert json.loads(rows[-1][0])["text"] is None
        assert json.loads(rows[-1][1]) == ["whatsapp/events.jsonl#3"]



def test_complete_event_window_is_anchored_to_first_copy(tmp_path):
    live, dev = tmp_path / "live", tmp_path / "dev"
    write_jsonl(live / "whatsapp/events.jsonl", [
        _raw("edit", "edit", {"chatJid": G, "messageId": "AC1", "participantJid": FRANK_LID,
                              "text": "same", "timestamp": T0 // 1000 + offset // 1000})
        for offset in (0, 120_000, 240_000)
    ])
    project([live, dev], tmp_path / "history.db")
    with closing(sqlite3.connect(tmp_path / "history.db")) as conn:
        rows = conn.execute("SELECT source_refs FROM message_events ORDER BY occurred_ms").fetchall()
        assert [json.loads(row[0]) for row in rows] == [
            ["whatsapp/events.jsonl#1", "whatsapp/events.jsonl#2"],
            ["whatsapp/events.jsonl#3"],
        ]


def test_purged_event_matches_only_complete_copies_within_window(tmp_path):
    live, dev = tmp_path / "live", tmp_path / "dev"
    write_jsonl(live / "whatsapp/events.jsonl", [
        _raw("edit", "edit", {"chatJid": G, "messageId": "AC1", "participantJid": FRANK_LID,
                              "text": "complete", "timestamp": T0 // 1000}),
        _raw("edit", "edit", {"chatJid": G, "messageId": "AC1", "participantJid": FRANK_LID,
                              "timestamp": T0 // 1000 + 100}),
        _raw("edit", "edit", {"chatJid": G, "messageId": "AC1", "participantJid": FRANK_LID,
                              "timestamp": T0 // 1000 + 200}),
    ])
    report = project([live, dev], tmp_path / "history.db")
    with closing(sqlite3.connect(tmp_path / "history.db")) as conn:
        rows = conn.execute("SELECT payload_json, source_refs FROM message_events ORDER BY occurred_ms").fetchall()
        assert len(rows) == 2 and report["events"] == 2
        assert json.loads(rows[0][1]) == ["whatsapp/events.jsonl#1", "whatsapp/events.jsonl#2"]
        assert json.loads(rows[0][0])["text"] == "complete"
        assert json.loads(rows[1][1]) == ["whatsapp/events.jsonl#3"]
        assert json.loads(rows[1][0])["text"] is None
        assert report["review"]["unmatched_event_payloads"] == ["whatsapp/events.jsonl#3"]


@pytest.mark.parametrize(("complete", "expected_count", "matched"), [
    ([{"messageId": "AC1", "emoji": "😂", "at": 0}], 1, True),
    ([{"messageId": "AC1", "emoji": "😂", "at": 20_001}], 2, False),
    ([{"messageId": "AC1", "emoji": "😂", "at": 0},
     {"messageId": "AC2", "emoji": "👍", "at": 20_000}], 3, False),
])
def test_unanchored_purged_assistant_reaction_joins_only_unique_candidate(
        tmp_path, complete, expected_count, matched):
    live, dev = tmp_path / "live", tmp_path / "dev"
    records = [_bf("journal", "reaction", {"fromAssistant": True, "targetMessageId": item["messageId"],
                    "emoji": item["emoji"], "removed": False}, ms=T0 + item["at"])
               for item in complete]
    records.append(_bf("journal", "reaction", {"fromAssistant": True, "removed": False},
                       ms=T0 + 10_000))
    write_jsonl(dev / "backfill/journal.jsonl", records)
    write_jsonl(live / "owner/attestations.jsonl", [make(
        "contact", 1, "assistant fixture", identifiers=["4915202777685@s.whatsapp.net"],
        role="assistant")])
    report = project([live, dev], tmp_path / "history.db")
    with closing(sqlite3.connect(tmp_path / "history.db")) as conn:
        rows = conn.execute("SELECT target_native_id, payload_json, source_refs FROM message_events ORDER BY target_native_id").fetchall()
        assert len(rows) == expected_count and report["events"] == expected_count
        incomplete_ref = f"backfill/journal.jsonl#{len(records)}"
        if matched:
            (row,) = [row for row in rows if incomplete_ref in json.loads(row[2])]
            assert row[0] == "AC1" and json.loads(row[1])["emoji"] == "😂"
            assert report["review"]["unmatched_event_payloads"] == []
        else:
            (row,) = [row for row in rows if incomplete_ref in json.loads(row[2])]
            assert row[0] is None and json.loads(row[1])["emoji"] is None
            assert report["review"]["unmatched_event_payloads"] == [incomplete_ref]


def test_explicit_reaction_removal_is_not_treated_as_purged_payload(tmp_path):
    live, dev = tmp_path / "live", tmp_path / "dev"
    write_jsonl(live / "whatsapp/events.jsonl", [
        _raw("reaction", "reaction", {"chatJid": G, "targetMessageId": "AC1", "senderId": FRANK_LID,
                                      "emoji": "😂", "removed": False, "timestamp": T0 // 1000}),
        _raw("reaction", "reaction", {"chatJid": G, "targetMessageId": "AC1", "senderId": FRANK_LID,
                                      "emoji": None, "removed": True, "timestamp": T0 // 1000 + 1}),
    ])
    project([live, dev], tmp_path / "history.db")
    with closing(sqlite3.connect(tmp_path / "history.db")) as conn:
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
    with closing(sqlite3.connect(tmp_path / "history.db")) as conn:
        rows = conn.execute("SELECT source_refs FROM message_events").fetchall()
        assert report["events"] == 2
        assert {tuple(json.loads(row[0])) for row in rows} == {
            ("whatsapp/events.jsonl#1",), ("whatsapp/events.jsonl#2",)}


def test_telegram_attestations_stay_provenance_only(tmp_path):
    from yeoman_gateway.history.extract import extract
    from yeoman_gateway.history.layer1 import iter_layer1
    from yeoman_gateway.history.resolve import resolve
    from yeoman_gateway.history.schema import PROJECTOR_VERSION
    root = tmp_path / 'layer1'
    records = [
        make('identifier', 1, 'stored', anchor=FRANK_PN, identifier='telegram:123'),
        make('contact', 2, 'mixed', identifiers=[FRANK_PN, 'telegram:124'], name='Frank'),
        make('contact', 3, 'telegram only', identifiers=['telegram:125']),
        make('identifier_ended', 4, 'stored end', identifier='telegram:123', ended_ms=3),
    ]
    write_jsonl(root / 'owner/attestations.jsonl', records)
    write_jsonl(root / 'backfill/telegram.jsonl', [
        {**_bf('journal', 'message', {'senderId': 'telegram:125', 'messageId': 'T1'}, ms=T0),
         'channel': 'telegram'},
        {**_bf('journal', 'reaction', {'senderId': 'telegram:125', 'emoji': 'x'}, ms=T0),
         'channel': 'telegram'},
    ])
    ex = extract(iter_layer1([root]))
    assert len(ex.attestations) == len(ex.identity.attestations) == 4
    res = resolve(ex.identity)
    assert res.resolve(classify('telegram:125')) == (None, 'unknown')
    assert all(i.channel == 'whatsapp' for i in res.identifiers)
    db = tmp_path / 'history.db'
    project([root], db)
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute('SELECT count(*) FROM contacts WHERE merged_into IS NULL').fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM identifier_history WHERE channel != 'whatsapp' OR value LIKE 'telegram:%'").fetchone()[0] == 0
        assert conn.execute('SELECT count(*) FROM messages').fetchone()[0] == 0
        assert conn.execute('SELECT count(*) FROM message_events').fetchone()[0] == 0
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 2
        assert {r[0] for r in conn.execute('SELECT projector_version FROM projector_state')} == {PROJECTOR_VERSION}


def test_project_temporal_message_and_event_time_selection(tmp_path):
    a, b, phone = ('491100000001@s.whatsapp.net', '491100000002@s.whatsapp.net',
                   '491100000003@s.whatsapp.net')
    root = tmp_path / 'layer1'
    boundary = T0 + 2000
    write_jsonl(root / 'owner/attestations.jsonl', [
        make('identifier', 1, 'old owner', anchor=a, identifier=phone,
             valid_from_ms=T0, valid_until_ms=boundary),
        make('identifier', 2, 'new owner', anchor=b, identifier=phone, valid_from_ms=boundary),
    ])
    write_jsonl(root / 'whatsapp/events.jsonl', [
        _raw('message', 'message', {'chatJid': G, 'messageId': 'OLD', 'senderId': phone,
                                   'text': 'old', 'timestamp': T0 // 1000 + 1}),
        _raw('message', 'message', {'chatJid': G, 'messageId': 'NEW', 'senderId': phone,
                                   'text': 'new', 'timestamp': boundary // 1000}),
        _raw('reaction', 'reaction', {'chatJid': G, 'targetMessageId': 'OLD', 'senderId': phone,
                                      'emoji': 'x', 'timestamp': T0 // 1000 + 1}),
        _raw('reaction', 'reaction', {'chatJid': G, 'targetMessageId': 'OLD', 'senderId': phone,
                                      'emoji': 'x', 'timestamp': boundary // 1000}),
        _raw('message', 'message', {'chatJid': G, 'messageId': 'APPROX', 'senderId': phone,
                                   'text': 'approx'}, received=boundary + 1000),
    ])
    # Selected native time governs even when a higher-ranked sender copy has only capture time.
    write_jsonl(root / 'backfill/journal.jsonl', [
        {**_bf('journal', 'message', {'messageId': 'APPROX', 'senderId': phone, 'text': 'approx'}, ms=T0+1000),
         'time_certainty': 'provider_timestamp'},
    ])
    db = tmp_path / 'history.db'
    project([root], db)
    with closing(sqlite3.connect(db)) as conn:
        ids = dict(conn.execute('SELECT value, contact_id FROM identifier_history WHERE value IN (?, ?)', (a,b)))
        messages = dict(conn.execute('SELECT native_message_id, sender_contact_id FROM messages'))
        assert messages == {'OLD': ids[a], 'NEW': ids[b], 'APPROX': ids[a]}
        events = conn.execute('SELECT actor_contact_id, actor_identifier FROM message_events').fetchall()
        assert set(events) == {(ids[a], phone), (ids[b], phone)} and len(events) == 2
        assert set(conn.execute('SELECT valid_from_ms, valid_until_ms FROM identifier_history WHERE value = ?',
                                (phone,))) == {(T0, boundary), (boundary, None)}


@pytest.mark.parametrize('order', [(0, 1, 2), (2, 1, 0), (1, 2, 0)])
def test_project_multi_window_anchor_agrees_across_assertion_order(tmp_path, order):
    a, p, q = ('491100000001@s.whatsapp.net', '491100000003@s.whatsapp.net',
               '491100000004@s.whatsapp.net')
    fields = [dict(anchor=a, identifier=p, valid_from_ms=100, valid_until_ms=300),
              dict(anchor=a, identifier=p, valid_from_ms=200), dict(anchor=p, identifier=q)]
    root = tmp_path / 'layer1'
    write_jsonl(root / 'owner/attestations.jsonl', [
        make('identifier', n, 'ownership', **fields[index]) for n, index in enumerate(order, 1)])
    write_jsonl(root / 'backfill/journal.jsonl', [
        _bf('journal', 'message', {'messageId': 'Q', 'senderId': q, 'text': 'Q'}, ms=250,
            certainty='provider_timestamp')])
    db = tmp_path / 'history.db'
    report = project([root], db)
    with closing(sqlite3.connect(db)) as conn:
        owners = {r[0] for r in conn.execute('SELECT contact_id FROM identifier_history WHERE value IN (?, ?, ?)',
                                            (a, p, q))}
        assert len(owners) == 1
        assert conn.execute('SELECT sender_contact_id FROM messages').fetchone()[0] in owners
    assert not report['review']['temporal_links_ambiguous']


@pytest.mark.parametrize('case', ['unique', 'absent', 'multiple'])
def test_project_identifier_ended_applicability_and_provenance(tmp_path, case):
    a, b, p = ('491100000001@s.whatsapp.net', '491100000002@s.whatsapp.net',
               '491100000003@s.whatsapp.net')
    root = tmp_path / 'layer1'
    records = []
    if case != 'absent':
        records.append(make('identifier', 1, 'first owner', anchor=a, identifier=p))
    if case == 'multiple':
        records.append(make('identifier', 2, 'second owner', anchor=b, identifier=p, valid_from_ms=100))
    records.append(make('identifier_ended', 3, 'end', identifier=p, ended_ms=200))
    write_jsonl(root / 'owner/attestations.jsonl', records)
    write_jsonl(root / 'backfill/journal.jsonl', [
        _bf('journal', 'message', {'messageId': str(ms), 'senderId': p, 'text': 'source'},
            ms=ms, certainty='provider_timestamp') for ms in (199, 200)])
    db = tmp_path / 'history.db'
    report = project([root], db)
    with closing(sqlite3.connect(db)) as conn:
        rows = conn.execute('SELECT valid_until_ms, ended_ms, source_refs FROM identifier_history WHERE value = ?', (p,)).fetchall()
        authors = dict(conn.execute('SELECT native_message_id, sender_contact_id FROM messages'))
    if case == 'unique':
        assert rows[0][:2] == (200, 200) and len(rows) == 1
        assert set(json.loads(rows[0][2])) >= {'owner/attestations.jsonl#1', 'owner/attestations.jsonl#2'}
        assert authors['199'] is not None and authors['200'] is None
        assert not report['review']['identifier_ended_not_applied']
    else:
        assert all(row[:2] == (None, None) for row in rows)
        assert report['review']['identifier_ended_not_applied'][0]['resolution'] == case
