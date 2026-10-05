from yeoman_gateway.history.extract import extract, rank_of
from yeoman_gateway.history.ids import classify
from yeoman_gateway.history.layer1 import Layer1Line

G = "4917623568044-1542142755@g.us"
LID, PN = "46918273106072@lid", "4917632625469@s.whatsapp.net"


def raw(n, kind, type_, payload, **extra):
    record = {"channel": "whatsapp", "chat_id": G, "direction": extra.pop("direction", "in"),
              "kind": kind, "native": {"type": type_, "payload": payload, **extra.pop("native", {})},
              "received_ms": 5000, **extra}
    return Layer1Line(f"whatsapp/2026-10.jsonl#{n}", record)


def bf(name, n, kind, payload, **extra):
    record = {"channel": extra.pop("channel", "whatsapp"), "kind": kind, "payload": payload,
              "provenance": extra.pop("provenance", "native"), "time_certainty": "provider_timestamp",
              "occurred_ms": 1000, "direction": extra.pop("direction", "in"), "chat_id": G,
              "skip_reason": extra.pop("skip_reason", None)}
    return Layer1Line(f"backfill/{name}.jsonl#{n}", record)


def test_rank_of():
    assert rank_of("whatsapp/2026-10.jsonl#3") == 0
    assert rank_of("backfill/journal.jsonl#1") == 1
    assert rank_of("backfill/memory_pre_rebackfill_2.jsonl#1") == 6
    assert rank_of("backfill/unknown.jsonl#1") == 7


def test_raw_message_links_and_names():
    ex = extract([raw(1, "message", "message", {"chatJid": G, "messageId": "AC1", "participantJid": LID,
                                                "senderPhoneJid": PN, "senderName": "Frank Taeger",
                                                "text": "[Image] hallo", "timestamp": 1790000000})])
    (copy,) = ex.messages
    assert copy.sender == classify(LID) and copy.sender_raw == LID and copy.rank == 0
    assert copy.text == "hallo" and copy.media == {"kind": "image"} and copy.occurred_ms == 1790000000000
    assert (LID, PN, "native_pair", "whatsapp/2026-10.jsonl#1") in ex.identity.links
    assert ex.identity.names[LID][0][1] == "Frank Taeger"
    assert ex.outcomes[("whatsapp/2026-10.jsonl", "message")] == 1


def test_lid_conflict_gives_no_link():
    ex = extract([raw(1, "message", "message", {"chatJid": G, "messageId": "AC1", "participantJid": LID,
                                                "senderPhoneJid": PN, "lidConflict": True})])
    assert ex.identity.links == []


def test_outbound_pairing_and_leftovers():
    ex = extract([
        raw(1, "outbound_request", "send_text", {"text": "Antwort", "to": G, "replyToMessageId": "AC1"},
            direction="out", correlation_id="c1"),
        raw(2, "outbound_result", "send_text", {}, direction="out", correlation_id="c1",
            native={"result": {"sent": {"messageId": "3EB0"}}}),
        raw(3, "outbound_request", "send_text", {"text": "nie bestätigt", "to": G}, direction="out",
            correlation_id="c2"),
        raw(4, "outbound_request", "react", {"chatJid": G, "messageId": "AC1", "emoji": "😂"},
            direction="out", correlation_id="c3"),
        raw(5, "outbound_result", "react", {}, direction="out", correlation_id="c3"),
    ])
    (message,) = ex.messages
    assert (message.native_id, message.text, message.from_assistant, message.direction) == ("3EB0", "Antwort", True, "out")
    assert message.reply_to == "AC1" and message.extra_refs == ("whatsapp/2026-10.jsonl#2",)
    (reaction,) = ex.events
    assert reaction.from_assistant and reaction.payload == {"emoji": "😂", "removed": False}
    out = ex.outcomes
    assert out[("whatsapp/2026-10.jsonl", "message")] == 1
    assert out[("whatsapp/2026-10.jsonl", "skipped:outbound_result_paired")] == 2
    assert out[("whatsapp/2026-10.jsonl", "skipped:outbound_without_result")] == 1
    assert out[("whatsapp/2026-10.jsonl", "event")] == 1


def test_events_membership_and_receipts():
    ex = extract([
        raw(1, "reaction", "reaction", {"chatJid": G, "emoji": "😂", "removed": False, "senderId": G,
                                        "targetMessageId": "AC1"}),
        raw(2, "edit", "edit", {"chatJid": G, "messageId": "AC1", "participantJid": LID, "text": "neu"}),
        raw(3, "delete", "delete", {"chatJid": G, "messageId": "AC1"}),
        raw(4, "membership_snapshot", "membership_snapshot",
            {"chatJid": G, "complete": True, "participants": [{"lid": LID, "phoneJid": PN, "admin": True}]}),
        raw(5, "receipt", "receipt", {"chatJid": G, "messageId": "AC1"}),
    ])
    reaction, edit, delete, snapshot = ex.events
    assert reaction.actor == classify(G) and reaction.target_native_id == "AC1"
    assert edit.payload == {"text": "neu"} and edit.target_native_id == "AC1" and edit.actor == classify(LID)
    assert delete.payload == {} and delete.actor is None
    assert snapshot.kind == "member_snapshot" and snapshot.payload == {"participants": [[LID, PN]], "complete": True}
    assert (LID, PN, "native_pair", "whatsapp/2026-10.jsonl#4") in ex.identity.links
    assert ex.outcomes[("whatsapp/2026-10.jsonl", "skipped:receipt")] == 1


def test_backfill_kinds_and_identity():
    ex = extract([
        bf("memory", 1, "message", {"chatJid": G, "senderId": "4917632625469", "text": "Feb",
                                    "generatedDescription": "Ein Hund", "mediaKind": "image"},
           provenance="verbatim_unverified"),
        bf("session_jsonl", 1, "message", {"chatJid": "1", "text": "privet"}, channel="telegram"),
        bf("session_jsonl", 2, "session_meta", {}, skip_reason="tool_trace"),
        bf("knowledge", 1, "contact_record", {"contactRef": "945ae43e", "displayName": "Frank", "createdMs": 1},
           channel="any"),
        bf("knowledge", 2, "identifier_record", {"contactRef": "945ae43e", "identifier": PN}),
        bf("knowledge", 3, "identifier_record", {"contactRef": "945ae43e", "identifier": LID, "status": "retracted"}),
        bf("knowledge", 4, "pair_record", {"lid": LID, "pnJid": PN}),
        bf("knowledge", 5, "name_record", {"contactRef": "945ae43e", "name": "Frank"}, channel="any"),
        Layer1Line("owner/attestations.jsonl#1", {"type": "merge", "at_ms": 1, "a": LID}),
        Layer1Line("derived/media-descriptions.jsonl#1", {"kind": "media_description", "channel": "whatsapp",
                                                          "chat_id": G, "native_message_id": "AC1",
                                                          "mode": "ocr", "text": "25. November"}),
        Layer1Line("backfill/memory.jsonl#2", None),
    ])
    (message,) = ex.messages
    assert message.description == "Ein Hund" and message.media == {"kind": "image"} and message.rank == 5
    assert message.provenance == "verbatim_unverified" and message.native_id is None
    assert "945ae43e" in ex.identity.contact_records
    assert ("ref:945ae43e", PN, "knowledge_binding", "backfill/knowledge.jsonl#2") in ex.identity.links
    assert (LID, PN, "native_pair", "backfill/knowledge.jsonl#4") in ex.identity.links
    assert ex.identity.names["ref:945ae43e"][0][1] == "Frank"
    assert ex.descriptions[0].mode == "ocr"
    o = ex.outcomes
    assert o[("backfill/session_jsonl.jsonl", "out_of_scope_channel")] == 1
    assert o[("backfill/session_jsonl.jsonl", "skipped:tool_trace")] == 1
    assert o[("backfill/knowledge.jsonl", "skipped:binding_status:retracted")] == 1
    assert o[("owner/attestations.jsonl", "invalid_attestation")] == 1
    assert o[("backfill/memory.jsonl", "invalid_json")] == 1
    assert sum(o.values()) == 11
