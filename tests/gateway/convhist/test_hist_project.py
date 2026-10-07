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
    assert json.loads(rows[0]["source_refs"]) == ["whatsapp/2026-10.jsonl#4"]
    # The historical request/empty result is not proof of success; the provider echo is independent.
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
    assert raw["skipped:outbound_not_sent"] == 2
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


# Task 3 fixtures deliberately use synthetic people, chats and content.
_T3_OWNER, _T3_OTHER, _T3_CHAT = '91001@lid', '91002@lid', '91000@g.us'


def _t3_message(native_id, text, sender=_T3_OTHER):
    return _bf('memory', 'message', {'messageId': native_id, 'senderId': sender, 'text': text},
               chat=_T3_CHAT, provenance='verbatim_unverified')


def _t3_contacts():
    return [make('contact', 1, 'synthetic owner', identifiers=[_T3_OWNER], role='owner'),
            make('contact', 1, 'synthetic other', identifiers=[_T3_OTHER])]


def test_author_base_ref_selects_only_native_id_segment(tmp_path):
    from yeoman_gateway.history import attestations

    assert callable(getattr(attestations, "resolve_author_targets", None)), "Task 3 target resolver missing"
    resolve_author_targets = attestations.resolve_author_targets
    from yeoman_gateway.history.extract import extract
    from yeoman_gateway.history.layer1 import iter_layer1
    from yeoman_gateway.history.verify import verify

    root = tmp_path / 'layer1'
    segments = [{'senderId': f'{92000+i}@lid', 'text': f'synthetic speaker {i}',
                 'messageId': 'LAST' if i == 6 else None} for i in range(7)]
    row = _t3_message('LAST', None)
    row['payload']['segments'] = segments
    multiple = _t3_message('REPEATED', None)
    multiple['payload']['segments'] = [{'messageId': 'REPEATED', 'text': 'one'},
                                       {'messageId': 'REPEATED', 'text': 'two'}]
    no_match = _t3_message('ABSENT', None)
    no_match['payload']['segments'] = [{'text': 'no native id'}]
    write_jsonl(root / 'backfill/memory.jsonl', [row, multiple, no_match])
    # Content-free purged slot; Task 4 owns the concrete tombstone writer/grammar.
    write_jsonl(root / 'whatsapp/slots.jsonl', [{}, _raw('receipt', 'receipt', {})])
    targets = ['backfill/memory.jsonl#1', 'backfill/memory.jsonl#1/0',
               'backfill/memory.jsonl#2', 'backfill/memory.jsonl#3',
               'whatsapp/slots.jsonl#1', 'whatsapp/slots.jsonl#2', 'whatsapp/slots.jsonl#99']
    records = _t3_contacts() + [make('author', 10+i, 'synthetic correction', source_ref=ref,
                                    anchor=_T3_OWNER) for i, ref in enumerate(targets)]
    write_jsonl(root / 'owner/attestations.jsonl', records)
    ex = extract(iter_layer1([root]))
    winners, review = resolve_author_targets(ex.attestations, ex.messages, ex.events)
    assert set(winners) == {'backfill/memory.jsonl#1/6', 'backfill/memory.jsonl#1/0'}
    assert len(review) == 5
    assert {r['attestation_ref'] for r in review} == {
        f'owner/attestations.jsonl#{n}' for n in range(5, 10)}
    db = tmp_path / 'history.db'
    report = project([root], db)
    with closing(sqlite3.connect(db)) as conn:
        owner = conn.execute("SELECT contact_id FROM contacts WHERE role='owner'").fetchone()[0]
        rows = conn.execute('SELECT text, sender_contact_id, sender_identifier, sender_basis, source_refs FROM messages').fetchall()
        for i in range(7):
            (message,) = [r for r in rows if r[0] == f'synthetic speaker {i}']
            assert message[2] == f'{92000+i}@lid'
            if i in (0, 6):
                assert message[1] == owner and message[3] == 'owner_attested'
                assert f'owner/attestations.jsonl#{3 if i == 6 else 4}' in json.loads(message[4])
            else:
                assert message[1] != owner and message[3] == 'derived_claim'
                assert json.loads(message[4]) == [f'backfill/memory.jsonl#1/{i}']
    assert report['accounting_ok'] and len(report['review']['author_targets']) == 5
    verified = verify([root], db, scratch=None)
    assert verified['accounting_ok'] and verified['review']['author_targets'] == report['review']['author_targets']


def test_event_author_applied_before_clustering(tmp_path):
    from yeoman_gateway.history.verify import verify

    root = tmp_path / 'layer1'
    edits = [_raw('edit', 'edit', {'chatJid': _T3_CHAT, 'messageId': 'TARGET',
                                  'nativeEventId': 'EDIT', 'text': 'synthetic revision',
                                  'timestamp': T0 // 1000 + i}) for i in (0, 1)]
    edits += [_raw('edit', 'edit', {'chatJid': _T3_CHAT, 'messageId': 'TARGET',
                                   'nativeEventId': 'EDIT', 'senderId': _T3_OTHER,
                                   'text': 'synthetic revision', 'timestamp': T0 // 1000})]
    edits.append(_raw('reaction', 'reaction', {'chatJid': _T3_CHAT, 'senderId': _T3_CHAT,
                                              'targetMessageId': 'TARGET', 'emoji': 'x'}))
    edits.append(_raw('membership_change', 'membership_change',
                      {'chatJid': _T3_CHAT, 'actor': _T3_CHAT, 'action': 'add', 'participants': []}))
    write_jsonl(root / 'whatsapp/events.jsonl', edits)
    write_jsonl(root / 'owner/attestations.jsonl', _t3_contacts())
    db = tmp_path / 'history.db'
    project([root], db)
    with closing(sqlite3.connect(db)) as conn:
        before = {r[0] for r in conn.execute("SELECT event_id FROM message_events WHERE kind='edit'")}
    corrections = [make('author', 10, 'synthetic event correction',
                        source_ref=f'whatsapp/events.jsonl#{i}', anchor=_T3_OWNER if i < 3 else _T3_OTHER)
                   for i in (1, 2, 3)]
    write_jsonl(root / 'owner/attestations.jsonl', _t3_contacts() + corrections)
    report = project([root], db)
    with closing(sqlite3.connect(db)) as conn:
        owner = conn.execute("SELECT contact_id FROM contacts WHERE role='owner'").fetchone()[0]
        rows = conn.execute("SELECT event_id, actor_contact_id, actor_identifier, actor_basis, source_refs FROM message_events WHERE kind='edit'").fetchall()
        assert len(rows) == 2
        (corrected,) = [r for r in rows if r[1] == owner]
        assert corrected[0] not in before
        assert corrected[2:4] == (None, 'owner_attested')
        assert set(json.loads(corrected[4])) == {'whatsapp/events.jsonl#1', 'whatsapp/events.jsonl#2',
                                                'owner/attestations.jsonl#3', 'owner/attestations.jsonl#4'}
        (other,) = [r for r in rows if r[1] != owner]
        assert other[2:4] == (_T3_OTHER, 'owner_attested')
        assert conn.execute("SELECT actor_contact_id FROM message_events WHERE kind='reaction'").fetchone()[0] is None
        assert conn.execute("SELECT actor_contact_id, actor_identifier, actor_basis FROM message_events WHERE kind='member_add'").fetchone() == (None, _T3_CHAT, 'native_identifier')
    conflicts = report['review']['author_targets']
    assert any(r['reason'] == 'conflicting_entity_authors' and len(r['claims']) == 3 for r in conflicts)
    assert report['accounting_ok']
    assert verify([root], db, scratch=None)['review']['author_targets'] == conflicts
    project([root], tmp_path / 'again.db')
    with closing(sqlite3.connect(tmp_path / 'again.db')) as conn:
        assert {r[0] for r in conn.execute("SELECT event_id FROM message_events WHERE kind='edit'")} == {r[0] for r in rows}


def test_distinct_owner_rows_with_repeated_native_id_are_reviewed(tmp_path):
    root = tmp_path / 'layer1'
    records = []
    for text in ('synthetic request', 'synthetic follow-up'):
        row = _t3_message('COLLISION', None)
        row['payload']['segments'] = [{'senderId': _T3_OTHER, 'text': 'surrounding speaker'},
                                       {'senderId': _T3_OTHER, 'text': text, 'messageId': 'COLLISION'}]
        records.append(row)
    write_jsonl(root / 'backfill/memory.jsonl', records)
    write_jsonl(root / 'owner/attestations.jsonl', _t3_contacts() + [
        make('author', 10, 'synthetic exact-row correction', source_ref=f'backfill/memory.jsonl#{i}/1',
             anchor=_T3_OWNER) for i in (1, 2)])
    db = tmp_path / 'history.db'
    report = project([root], db)
    collision = report['review']['message_id_collisions']
    assert collision == [{'message_id': f'whatsapp:{_T3_CHAT}:COLLISION',
                          'copies': [{'source_ref': f'backfill/memory.jsonl#{i}/1', 'text': text,
                                      'attestation_ref': f'owner/attestations.jsonl#{i+2}'}
                                     for i, text in enumerate(('synthetic request', 'synthetic follow-up'), 1)]}]
    with closing(sqlite3.connect(db)) as conn:
        owner = conn.execute("SELECT contact_id FROM contacts WHERE role='owner'").fetchone()[0]
        row = conn.execute("SELECT text, sender_contact_id, sender_identifier, source_refs FROM messages WHERE native_message_id='COLLISION'").fetchone()
        assert row[:3] == ('synthetic request', owner, _T3_OTHER)
        assert set(json.loads(row[3])) == {'backfill/memory.jsonl#1/1', 'backfill/memory.jsonl#2/1',
                                         'owner/attestations.jsonl#3', 'owner/attestations.jsonl#4'}
        assert conn.execute("SELECT count(*) FROM messages WHERE text='surrounding speaker' AND sender_contact_id=?", (owner,)).fetchone()[0] == 0
    assert report['accounting_ok']


@pytest.mark.parametrize('case,offset,time_basis,transfer,expected', [
    ('within', 50, 'provider_timestamp', False, 'A'),
    ('expired', 200, 'provider_timestamp', False, None),
    ('transfer_exact', 50, 'provider_timestamp', True, 'A'),
    ('transfer_approximate_before', 50, 'capture_time_approx', True, 'A'),
    ('transfer_approximate', 200, 'capture_time_approx', True, None),
    ('transfer_unknown', None, 'unknown', True, None),
])
def test_author_correction_uses_target_copy_time(tmp_path, case, offset, time_basis, transfer, expected):
    from yeoman_gateway.history.verify import verify

    a, b, phone = '95001@lid', '95002@lid', '95003@s.whatsapp.net'
    root = tmp_path / 'layer1'
    occurred = T0 + offset if offset is not None else None
    contacts = [make('contact', 1, 'synthetic A', identifiers=[a]),
                make('contact', 1, 'synthetic B', identifiers=[b]),
                make('identifier', 2, 'synthetic old window', anchor=a, identifier=phone,
                     valid_from_ms=T0, valid_until_ms=T0 + 100)]
    if transfer:
        contacts.append(make('identifier', 3, 'synthetic transfer', anchor=b, identifier=phone,
                             valid_from_ms=T0 + 100))
    message = _bf('memory', 'message', {'messageId': 'TIMED', 'text': 'synthetic timed message'},
                  chat=_T3_CHAT, ms=occurred, certainty=time_basis)
    event = _bf('memory', 'edit', {'targetMessageId': 'TIMED', 'nativeEventId': 'TIMED-EDIT',
                                 'text': 'synthetic timed revision'},
                chat=_T3_CHAT, ms=occurred, certainty=time_basis)
    write_jsonl(root / 'backfill/memory.jsonl', [message, event])
    # One legacy decision targets two message copies: unresolved accounting/review is per decision.
    write_jsonl(root / 'backfill/journal.jsonl', [message])
    write_jsonl(root / 'owner/attestations.jsonl', contacts)
    db = tmp_path / 'history.db'
    project([root], db)
    with closing(sqlite3.connect(db)) as conn:
        before = conn.execute('SELECT event_id FROM message_events').fetchone()[0]
    corrections = [make('message_author', T0 + 500, 'synthetic timed legacy author',
                        message_id=f'whatsapp:{_T3_CHAT}:TIMED', anchor=phone),
                   make('author', T0 + 500, 'synthetic timed event author',
                        source_ref='backfill/memory.jsonl#2', anchor=phone)]
    write_jsonl(root / 'owner/attestations.jsonl', contacts + corrections)
    report = project([root], db)
    with closing(sqlite3.connect(db)) as conn:
        ids = dict(conn.execute('SELECT value, contact_id FROM identifier_history WHERE value IN (?,?)', (a, b)))
        contact = ids[a] if expected == 'A' else None
        basis = 'owner_attested' if expected else 'unknown'
        assert conn.execute('SELECT sender_contact_id, sender_basis, sender_identifier FROM messages').fetchone() == (contact, basis, None)
        edit = conn.execute('SELECT actor_contact_id, actor_basis, actor_identifier, event_id FROM message_events').fetchone()
        assert edit[:3] == (contact, basis, None)
        assert (edit[3] != before) == bool(expected)
    reviews = report['review']['author_targets']
    if expected:
        assert reviews == []
        assert report['outcomes']['owner/attestations.jsonl']['attestation'] == len(contacts) + 2
    else:
        assert len(reviews) == 2 and all(r['reason'] == 'unresolved_author_anchor' for r in reviews)
        assert {r['attestation_ref'] for r in reviews} == {
            f'owner/attestations.jsonl#{len(contacts)+i}' for i in (1, 2)}
        assert report['outcomes']['owner/attestations.jsonl']['skipped:invalid_author_target'] == 2
        assert report['outcomes']['owner/attestations.jsonl']['attestation'] == len(contacts)
    verified = verify([root], db, scratch=None)
    assert report['accounting_ok'] and verified['accounting_ok']
    assert report['accounting'] == verified['accounting']
    assert reviews == verified['review']['author_targets']


def test_event_author_contradiction_retains_divergent_payloads(tmp_path):
    from yeoman_gateway.history.verify import verify

    root = tmp_path / 'layer1'
    write_jsonl(root / 'backfill/memory.jsonl', [
        _bf('memory', 'edit', {'nativeEventId': 'SHARED-EDIT', 'targetMessageId': target,
                             'text': text, 'senderId': _T3_OTHER}, chat=_T3_CHAT, ms=T0+i)
        for i, (target, text) in enumerate([('TARGET-A', 'synthetic revision one'),
                                           ('TARGET-B', 'synthetic revision two')])])
    write_jsonl(root / 'owner/attestations.jsonl', _t3_contacts() + [
        make('author', 10, 'synthetic divergent event correction',
             source_ref=f'backfill/memory.jsonl#{i}', anchor=anchor)
        for i, anchor in enumerate((_T3_OWNER, _T3_OTHER), 1)])
    db = tmp_path / 'history.db'
    report = project([root], db)
    (conflict,) = [r for r in report['review']['author_targets']
                   if r['reason'] == 'conflicting_entity_authors']
    assert conflict['entity'] == ['edit', 'whatsapp', _T3_CHAT, 'SHARED-EDIT']
    assert [(c['source_ref'], c['target_native_id'], c['payload']) for c in conflict['claims']] == [
        ('backfill/memory.jsonl#1', 'TARGET-A', {'text': 'synthetic revision one'}),
        ('backfill/memory.jsonl#2', 'TARGET-B', {'text': 'synthetic revision two'})]
    assert {c['attestation_ref'] for c in conflict['claims']} == {
        'owner/attestations.jsonl#3', 'owner/attestations.jsonl#4'}
    with closing(sqlite3.connect(db)) as conn:
        rows = conn.execute('SELECT native_event_id, target_native_id, payload_json, actor_contact_id, actor_identifier, source_refs FROM message_events').fetchall()
        assert len(rows) == 2 and len({r[3] for r in rows}) == 2
        assert {r[0] for r in rows} == {'SHARED-EDIT'}
        assert {r[1] for r in rows} == {'TARGET-A', 'TARGET-B'}
        assert {json.loads(r[2])['text'] for r in rows} == {'synthetic revision one', 'synthetic revision two'}
        assert {r[4] for r in rows} == {_T3_OTHER}
        assert set().union(*(set(json.loads(r[5])) for r in rows)) == {
            'backfill/memory.jsonl#1', 'backfill/memory.jsonl#2',
            'owner/attestations.jsonl#3', 'owner/attestations.jsonl#4'}
    assert verify([root], db, scratch=None)['review']['author_targets'] == report['review']['author_targets']


def test_author_purged_segment_keeps_surviving_refs_and_accounting(tmp_path):
    from yeoman_gateway.history.attestations import resolve_author_targets
    from yeoman_gateway.history.extract import extract
    from yeoman_gateway.history.layer1 import iter_layer1
    from yeoman_gateway.history.verify import verify

    root = tmp_path / 'layer1'
    parent = _t3_message('SURVIVING-LAST', 'synthetic parent text')
    parent['payload']['segments'] = [
        {'senderId': '96001@lid', 'text': 'synthetic first', 'messageId': None},
        {},  # Grammar-neutral content-free purged slot; Task 4 owns the marker.
        {'senderId': '96002@lid', 'text': 'synthetic last', 'messageId': 'SURVIVING-LAST'}]
    write_jsonl(root / 'backfill/memory.jsonl', [parent])
    write_jsonl(root / 'owner/attestations.jsonl', _t3_contacts() + [
        make('author', 10, 'synthetic purged segment target', source_ref='backfill/memory.jsonl#1/1',
             anchor=_T3_OWNER)])
    ex = extract(iter_layer1([root]))
    assert [(c.ref, c.sender_raw, c.native_id) for c in ex.messages] == [
        ('backfill/memory.jsonl#1/0', '96001@lid', None),
        ('backfill/memory.jsonl#1/2', '96002@lid', 'SURVIVING-LAST')]
    winners, review = resolve_author_targets(ex.attestations, ex.messages, ex.events)
    assert winners == {} and len(review) == 1
    assert review[0]['reason'] == 'missing_or_non_content_target'
    assert review[0]['target'] == 'backfill/memory.jsonl#1/1'
    db = tmp_path / 'history.db'
    report = project([root], db)
    with closing(sqlite3.connect(db)) as conn:
        rows = conn.execute('SELECT text, sender_identifier, native_message_id, source_refs, sender_basis FROM messages ORDER BY text').fetchall()
        assert rows == [('synthetic first', '96001@lid', None, '["backfill/memory.jsonl#1/0"]', 'derived_claim'),
                        ('synthetic last', '96002@lid', 'SURVIVING-LAST', '["backfill/memory.jsonl#1/2"]', 'derived_claim')]
    verified = verify([root], db, scratch=None)
    for result in (report, verified):
        assert result['accounting_ok']
        assert result['accounting']['backfill/memory.jsonl'] == {'lines': 1, 'accounted': 1}
        assert len(result['review']['author_targets']) == 1
    assert report['review']['author_targets'] == verified['review']['author_targets']
    assert report['outcomes']['owner/attestations.jsonl']['skipped:invalid_author_target'] == 1


def test_outbound_pair_crosses_month_and_deduplicates_echo(tmp_path):
    from yeoman_gateway.history.extract import extract
    from yeoman_gateway.history.layer1 import Layer1Line

    chat = "4915550000000-1@g.us"
    poll = {"name": "Lunch?", "values": ["One", "Two"], "selectableCount": 1}
    request = {**_raw("outbound_request", "send_poll", {"to": chat, "question": " Lunch? ",
                "options": [" One ", " Two "]}, direction="out", corr="month"), "chat_id": chat}
    result = {**_raw("outbound_result", "send_poll", {}, direction="out", corr="month"), "chat_id": chat}
    result["native"]["result"] = {"sent": {"providerMessageId": "POLL", "messageId": "POLL",
                                             "options": 2, "poll": poll}}
    echo = {**_raw("message", "message", {"chatJid": chat, "messageId": "POLL", "fromAssistant": True,
                    "media": {"kind": "poll", "poll": poll}}), "chat_id": chat}
    request["received_ms"] = 1769903999000  # 2026-01-31 23:59:59 UTC
    result["received_ms"] = echo["received_ms"] = 1769904000000  # February first line
    jan = [request, request]
    feb = [result, result, echo, _raw("receipt", "receipt", {"messageId": "POLL"})]
    # Conflicting requests and results must not choose a winner.
    bad_request = {**request, "correlation_id": "conflict"}
    bad_other = {**bad_request, "native": {"type": "send_poll", "payload": {
        **request["native"]["payload"], "question": "different"}}}
    bad_result = {**result, "correlation_id": "conflict"}
    bad_results = [{**request, "correlation_id": "result-conflict"},
                   {**result, "correlation_id": "result-conflict"},
                   {**result, "correlation_id": "result-conflict", "native": {"type": "send_poll",
                    "result": {"sent": {"messageId": "OTHER", "poll": poll}}}}]
    feb.extend([bad_request, bad_other, bad_result, *bad_results])

    def pair(type_, payload, returned, correlation):
        req = {**_raw("outbound_request", type_, payload, direction="out", corr=correlation), "chat_id": chat}
        ret = {**_raw("outbound_result", type_, {}, direction="out", corr=correlation), "chat_id": chat}
        ret["native"]["result"] = returned
        feb.extend([req, ret, ret])

    forward = {"text": "sent body", "caption": None, "media": None, "forwarded": True,
               "sourceChatJid": chat, "sourceMessageId": "SOURCE", "provenance": "sent"}
    pair("forward_message", {"to": chat, "sourceChatJid": chat, "sourceMessageId": "SOURCE"},
         {"forwarded": {"providerMessageId": "FORWARD", "content": forward}}, "forward")
    pair("delete_message", {"chatJid": chat, "messageId": "FORWARD"},
         {"deleted": {"chatJid": chat, "messageId": "FORWARD"}}, "delete")
    pair("react", {"chatJid": chat, "messageId": "FORWARD", "emoji": "x"},
         {"reacted": {"chatJid": chat, "messageId": "FORWARD", "providerMessageId": "REACTION"}}, "reaction")
    pair("forward_message", {"to": chat, "sourceChatJid": chat, "sourceMessageId": "OLD"},
         {"forwarded": {"messageId": "OLD-FORWARD"}}, "historical-forward")
    feb.append({**_raw("reaction", "reaction", {"chatJid": chat, "targetMessageId": "FORWARD",
               "senderId": chat, "emoji": "x", "nativeEventId": "REACTION"}), "chat_id": chat})
    lines = [Layer1Line(f"whatsapp/{month}.jsonl#{i}", r)
             for month, records in [("2026-01", jan), ("2026-02", feb)]
             for i, r in enumerate(records, 1)]
    for ordered in (lines, list(reversed(lines))):
        ex = extract(ordered)
        assert len([m for m in ex.messages if m.native_id == "POLL"]) == 2  # pair plus native echo
        assert {r["correlation_id"] for r in ex.review["outbound_correlations"]} == {
            "conflict", "result-conflict"}
        assert sum(ex.outcomes.values()) == len(lines)
    root = tmp_path / "layer1"
    write_jsonl(root / "whatsapp/2026-01.jsonl", jan)
    write_jsonl(root / "whatsapp/2026-02.jsonl", feb)
    write_jsonl(root / "owner/attestations.jsonl", [make("contact", 1, "synthetic assistant",
               identifiers=["4915550000009@s.whatsapp.net"], role="assistant")])
    write_jsonl(root / "backfill/journal.jsonl", [_bf("journal", "message", {
        "messageId": "FORWARD", "fromAssistant": True, "principal": "4915550000001",
        "addressee": "4915550000001", "text": "sent body"}, direction="out", chat=chat)])
    originals = {p: p.read_bytes() for p in root.rglob("*.jsonl")}
    db = tmp_path / "history.db"
    report = project([root], db)
    assert report["accounting_ok"]
    assert len(report["review"]["outbound_correlations"]) == 2
    assert report["review"]["outbound_content_gaps"][0]["native_message_id"] == "OLD-FORWARD"
    with closing(sqlite3.connect(db)) as conn:
        conn.row_factory = sqlite3.Row
        assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 3
        row = conn.execute("SELECT * FROM messages WHERE native_message_id='POLL'").fetchone()
        assert row["native_message_id"] == "POLL" and row["direction"] == "out"
        assert json.loads(row["media_json"]) == {"kind": "poll", "poll": poll}
        assert row["sent_ms"] == result["received_ms"]
        assert row["sender_contact_id"] == conn.execute(
            "SELECT contact_id FROM contacts WHERE role='assistant'").fetchone()[0]
        assert json.loads(row["source_refs"]) == [f"whatsapp/{month}.jsonl#{i}"
            for month, indices in [("2026-01", [1, 2]), ("2026-02", [1, 2, 3])] for i in indices]
        events = conn.execute("SELECT * FROM message_events ORDER BY kind").fetchall()
        assert [(e["kind"], e["target_native_id"]) for e in events] == [("delete", "FORWARD"), ("reaction", "FORWARD")]
        assert all(e["actor_contact_id"] == row["sender_contact_id"] for e in events)
        assert all(len(json.loads(e["source_refs"])) >= 3 for e in events)
        assert events[1]["native_event_id"] == "REACTION"
        forwarded = conn.execute("SELECT * FROM messages WHERE native_message_id='FORWARD'").fetchone()
        assert forwarded["text"] == "sent body" and forwarded["sender_contact_id"] == row["sender_contact_id"]
        assert forwarded["sender_identifier"] is None and forwarded["direction"] == "out"
        assert json.loads(forwarded["media_json"])["forward"]["provenance"] == "sent"
    assert all(p.read_bytes() == data for p, data in originals.items())


@pytest.mark.parametrize("kind", ["reaction", "delete"])
@pytest.mark.parametrize("alias", [1, 2, 3])
def test_outbound_event_extra_refs_receive_timed_author_before_clustering(tmp_path, kind, alias):
    from yeoman_gateway.history.attestations import resolve_author_targets
    from yeoman_gateway.history.extract import extract
    from yeoman_gateway.history.layer1 import iter_layer1

    chat, old, new, phone = "4915550000000-1@g.us", "97001@lid", "97002@lid", "4915550000001@s.whatsapp.net"
    command = "react" if kind == "reaction" else "delete_message"
    payload = {"chatJid": chat, "messageId": "TARGET", **({"emoji": "x"} if kind == "reaction" else {})}
    returned = {"chatJid": chat, "messageId": "TARGET", **({"providerMessageId": "REACTION"} if kind == "reaction" else {})}
    request = {**_raw("outbound_request", command, payload, direction="out", received=T0+50, corr="alias"), "chat_id": chat}
    result = {**_raw("outbound_result", command, {}, direction="out", received=T0+50, corr="alias"), "chat_id": chat}
    result["native"]["result"] = {"reacted" if kind == "reaction" else "deleted": returned}
    echo = {**_raw(kind, kind, {"chatJid": chat, "targetMessageId": "TARGET", "senderId": old,
                              **({"emoji": "x", "nativeEventId": "REACTION"} if kind == "reaction" else {})},
                  received=T0+50), "chat_id": chat}
    root = tmp_path / "layer1"
    source = root / "whatsapp/aliases.jsonl"
    write_jsonl(source, [request, request, result, echo])
    contacts = [make("contact", 1, "synthetic old", identifiers=[old]),
                make("contact", 1, "synthetic new", identifiers=[new]),
                make("contact", 1, "synthetic assistant", identifiers=["4915550000009@s.whatsapp.net"], role="assistant"),
                make("identifier", 2, "synthetic old ownership", anchor=old, identifier=phone, valid_from_ms=T0, valid_until_ms=T0+100),
                make("identifier", 3, "synthetic new ownership", anchor=new, identifier=phone, valid_from_ms=T0+100)]
    correction = make("author", T0+500, "synthetic alias correction", source_ref=f"whatsapp/aliases.jsonl#{alias}", anchor=phone)
    owners = root / "owner/attestations.jsonl"
    write_jsonl(owners, contacts + [correction])
    original = source.read_bytes()
    ex = extract(iter_layer1([root]))
    assert len(ex.events) == 2  # one paired copy and one independent provider echo
    winners, review = resolve_author_targets(ex.attestations, ex.messages, ex.events)
    assert set(winners) == {"whatsapp/aliases.jsonl#1"} and review == []
    db = tmp_path / "history.db"
    report = project([root], db)
    assert report["accounting_ok"] and report["review"]["author_targets"] == []
    with closing(sqlite3.connect(db)) as conn:
        rows = conn.execute("SELECT actor_contact_id, actor_basis, source_refs FROM message_events").fetchall()
        assert len(rows) == 1  # corrected actor must match echo before grouping
        old_contact = conn.execute("SELECT contact_id FROM identifier_history WHERE value=?", (old,)).fetchone()[0]
        assert rows[0][:2] == (old_contact, "owner_attested")  # target time, not correction time
        assert json.loads(rows[0][2]) == ["owner/attestations.jsonl#6", *[f"whatsapp/aliases.jsonl#{i}" for i in range(1, 5)]]
    # Contradictory alias claims meet on the same canonical event, latest wins, both remain reviewed.
    write_jsonl(owners, contacts + [correction, make("author", T0+501, "synthetic contradictory alias",
                source_ref=f"whatsapp/aliases.jsonl#{2 if alias == 3 else 3}", anchor=new)])
    conflict = project([root], db)
    (claim,) = [item for item in conflict["review"]["author_targets"] if item["reason"] == "conflicting_author_claims"]
    assert claim["source_ref"] == "whatsapp/aliases.jsonl#1"
    assert len(claim["claims"]) == 2 and claim["winner_ref"] == "owner/attestations.jsonl#7"
    assert source.read_bytes() == original


def test_outbound_identifiers_follow_bridge_normalization(tmp_path):
    import copy

    chat, device = "4915550000001@s.whatsapp.net", "4915550000001:7@s.whatsapp.net"
    source_chat = "4915550000002@s.whatsapp.net"
    poll = {"name": "Lunch?", "values": ["one", "two"], "selectableCount": 1}
    content = {"text": "forwarded", "caption": None, "media": None, "forwarded": True,
               "sourceChatJid": source_chat, "sourceMessageId": "SOURCE", "provenance": "sent"}
    records = []

    def pair(command, payload, returned, corr):
        raw_chat = payload.get("to") or payload["chatJid"]
        request = {**_raw("outbound_request", command, payload, direction="out", corr=corr), "chat_id": raw_chat}
        result = {**_raw("outbound_result", command, {}, direction="out", corr=corr), "chat_id": raw_chat}
        result["native"]["result"] = returned
        records.extend([request, result])
        return request

    pair("send_text", {"to": f" {chat} ", "text": "text"}, {"sent": {"to": chat, "providerMessageId": "TEXT"}}, "text")
    pair("send_poll", {"to": f" {chat} ", "question": "Lunch?", "options": ["one", "two"]},
         {"sent": {"to": chat, "providerMessageId": "POLL", "options": 2, "poll": poll}}, "poll")
    forward = pair("forward_message", {"to": f" {device} ", "sourceChatJid": " 4915550000002:8@s.whatsapp.net ", "sourceMessageId": " SOURCE "},
                   {"forwarded": {"to": chat, "providerMessageId": "FORWARD", "content": content}}, "forward")
    # Equivalent normalized duplicate must retain its ref rather than conflict.
    records.append({**forward, "chat_id": chat, "native": {"type": "forward_message", "payload": {
        "to": chat, "sourceChatJid": source_chat, "sourceMessageId": "SOURCE"}}})
    pair("delete_message", {"chatJid": f" {device} ", "messageId": " FORWARD "},
         {"deleted": {"chatJid": chat, "messageId": "FORWARD"}}, "delete")
    pair("react", {"chatJid": f" {chat} ", "messageId": " FORWARD ", "emoji": "x"},
         {"reacted": {"chatJid": chat, "messageId": "FORWARD", "providerMessageId": "REACTION"}}, "react")
    different = pair("forward_message", {"to": chat, "sourceChatJid": source_chat, "sourceMessageId": "SOURCE"},
                     {"forwarded": {"to": chat, "providerMessageId": "CONFLICT", "content": content}}, "different")
    records.append({**different, "native": {"type": "forward_message", "payload": {
        **different["native"]["payload"], "sourceMessageId": "ANOTHER"}}})
    original = copy.deepcopy(records)
    root, db = tmp_path / "layer1", tmp_path / "history.db"
    path = write_jsonl(root / "whatsapp/normalized.jsonl", records)
    raw_bytes = path.read_bytes()
    report = project([root], db)
    assert report["accounting_ok"] and report["review"]["outbound_content_gaps"] == []
    assert [item["correlation_id"] for item in report["review"]["outbound_correlations"]] == ["different"]
    with closing(sqlite3.connect(db)) as conn:
        conn.row_factory = sqlite3.Row
        messages = conn.execute("SELECT * FROM messages ORDER BY native_message_id").fetchall()
        assert [row["native_message_id"] for row in messages] == ["FORWARD", "POLL", "TEXT"]
        assert all(row["chat_id"] == chat for row in messages)
        assert messages[0]["text"] == "forwarded"
        assert json.loads(messages[0]["media_json"])["forward"]["sourceChatJid"] == source_chat
        assert json.loads(messages[0]["media_json"])["forward"]["sourceMessageId"] == "SOURCE"
        events = conn.execute("SELECT * FROM message_events ORDER BY kind").fetchall()
        assert [(row["kind"], row["chat_id"], row["target_native_id"], row["target_message_id"]) for row in events] == [
            (kind, chat, "FORWARD", f"whatsapp:{chat}:FORWARD") for kind in ("delete", "reaction")]
    assert records == original and path.read_bytes() == raw_bytes
