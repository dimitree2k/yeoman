from yeoman_gateway.history.extract import EventCopy, extract, rank_of
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


def test_group_identifier_is_not_a_message_sender_but_other_ids_are_observed():
    group_only = [
        raw(1, "message", "message", {"chatJid": G, "messageId": "AC-group-raw", "senderId": G}),
        bf("bridge_refs", 1, "message", {"chatJid": G, "messageId": "AC-group-bridge", "senderId": G}),
        bf("session_jsonl", 1, "message", {"chatJid": G, "messageId": "AC-group-derived", "senderId": G},
           provenance="derived_only"),
    ]
    ex = extract(group_only + [raw(4, "message", "message", {
        "chatJid": G, "messageId": "AC-person", "participantJid": G, "senderPhoneJid": PN,
    })])

    assert len(ex.messages) == 4
    assert all(copy.sender is None and copy.sender_raw is None for copy in ex.messages[:3])
    assert ex.messages[3].sender == classify(PN) and ex.messages[3].sender_raw == PN
    assert classify(G) in ex.identity.groups


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
        raw(5, "outbound_result", "react", {}, direction="out", correlation_id="c3",
            native={"result": {"reacted": {"chatJid": G, "messageId": "AC1",
                                            "providerMessageId": "REACTION"}}}),
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


def test_membership_actor_pair_and_journal_phone_key_are_normalized():
    actor = {"lid": LID, "phoneJid": PN}
    ex = extract([
        raw(1, "membership_change", "membership_change", {
            "chatJid": G, "action": "add", "participants": [actor], "actor": actor,
        }),
        bf("journal", 1, "membership_change", {
            "chatJid": G, "action": "add",
            "participants": [{"lid": LID, "phone_jid": PN}], "actor": actor,
        }),
    ])

    assert len(ex.events) == 2
    assert all(event.actor == classify(LID) for event in ex.events)
    assert all(event.actor_raw == LID for event in ex.events)
    assert sum(link[:3] == (LID, PN, "native_pair") for link in ex.identity.links) == 4


def test_event_native_id_is_separate_from_target_and_positional_api_remains_compatible():
    ex = extract([bf("bridge_refs", 1, "edit", {"targetMessageId": "AC1", "nativeEventId": "P2",
                                                   "text": "neu"})])
    (event,) = ex.events
    assert event.target_native_id == "AC1"
    assert event.native_event_id == "P2"

    legacy = EventCopy("ref", 0, "delete", "whatsapp", G, "AC1", None, None, False,
                       1000, "provider_timestamp", {}, "native")
    assert legacy.native_event_id is None

    (raw_event,) = extract([raw(2, "edit", "edit", {"messageId": "AC1", "text": "alt"},
                                 native={"eventId": "yeoman-hash"})]).events
    assert raw_event.native_event_id is None


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


def test_batch_segments_become_ordered_claim_copies_with_internal_stable_keys():
    first = bf("memory", 4, "message", {
        "chatJid": G, "messageId": "LAST-ID", "senderId": "4915550000003",
        "segments": [
            {"text": "unmarked"},
            {"senderId": "4915550000001", "text": "first"},
            {"senderId": "4915550000002", "text": "last", "messageId": "LAST-ID"},
        ],
    }, provenance="verbatim_unverified")
    second = bf("knowledge_memory", 8, "message", {
        "chatJid": G, "messageId": "LAST-ID", "senderId": "4915550000003",
        "segments": first.record["payload"]["segments"],
    }, provenance="verbatim_unverified")
    ex = extract([first, second])
    assert [(m.ref, m.text, m.native_id, m.sender_raw, m.batch_key) for m in ex.messages] == [
        ("backfill/memory.jsonl#4/0", "unmarked", None, "4915550000003", f'["{G}","LAST-ID",2]'),
        ("backfill/memory.jsonl#4/1", "first", None, "4915550000001", f'["{G}","LAST-ID",1]'),
        ("backfill/memory.jsonl#4/2", "last", "LAST-ID", "4915550000002", None),
        ("backfill/knowledge_memory.jsonl#8/0", "unmarked", None, "4915550000003", f'["{G}","LAST-ID",2]'),
        ("backfill/knowledge_memory.jsonl#8/1", "first", None, "4915550000001", f'["{G}","LAST-ID",1]'),
        ("backfill/knowledge_memory.jsonl#8/2", "last", "LAST-ID", "4915550000002", None),
    ]
    assert all(m.provenance == "verbatim_unverified" for m in ex.messages)
    assert ex.outcomes[("backfill/memory.jsonl", "message")] == 1
    assert ex.outcomes[("backfill/knowledge_memory.jsonl", "message")] == 1


def test_malformed_attestation_types_are_counted_invalid():
    lines = [
        Layer1Line(f"owner/attestations.jsonl#{i}", {"type": value, "at_ms": 1})
        for i, value in enumerate(([], {}, 1, True), 1)
    ]
    ex = extract(lines)
    assert ex.outcomes[("owner/attestations.jsonl", "invalid_attestation")] == len(lines)
    assert sum(ex.outcomes.values()) == len(lines)


def test_conflicting_outbound_correlation_is_reviewed():
    lines = [
        raw(1, "outbound_request", "send_text", {"text": "erste", "to": G}, direction="out",
            correlation_id="same"),
        raw(2, "outbound_request", "send_text", {"text": "zweite", "to": G}, direction="out",
            correlation_id="same"),
        raw(3, "outbound_result", "send_text", {}, direction="out", correlation_id="same",
            native={"result": {"sent": {"messageId": "3EB0"}}}),
    ]
    ex = extract(lines)
    assert ex.messages == []
    assert ex.review["outbound_correlations"][0]["source_refs"] == [line.ref for line in lines]
    assert ex.outcomes[("whatsapp/2026-10.jsonl", "skipped:conflicting_outbound_correlation")] == 3
    assert sum(ex.outcomes.values()) == len(lines)


def test_outbound_content_requires_success_and_native_result():
    import copy

    lines = []

    def pair(type_, payload, result=None, *, error=None):
        corr = f"task6-{len(lines)}"
        lines.append(raw(len(lines) + 1, "outbound_request", type_, payload,
                         direction="out", correlation_id=corr))
        if result is not None or error:
            lines.append(raw(len(lines) + 1, "outbound_result", type_, {}, direction="out",
                             correlation_id=corr, native={"error": error} if error else {"result": result}))

    poll = {"name": "Question", "values": ["One", "Two"], "selectableCount": 1}
    pair("send_poll", {"to": G, "question": "untrimmed", "options": [" One ", "Two", ""]},
         {"sent": {"messageId": "POLL", "providerMessageId": "POLL", "options": 2, "poll": poll}})
    content = {"text": None, "caption": "Picture", "media": {"kind": "image", "mimeType": "image/jpeg"},
               "forwarded": True, "sourceChatJid": "4915550000001@s.whatsapp.net",
               "sourceMessageId": "SOURCE", "provenance": "source"}
    pair("forward_message", {"to": G, "sourceChatJid": content["sourceChatJid"], "sourceMessageId": "SOURCE"},
         {"forwarded": {"providerMessageId": "FORWARD", "messageId": "FORWARD", "content": content}})
    pair("delete_message", {"chatJid": G, "messageId": "TARGET"},
         {"deleted": {"chatJid": G, "messageId": "TARGET"}})
    pair("react", {"chatJid": G, "messageId": "TARGET", "emoji": "x"},
         {"reacted": {"chatJid": G, "messageId": "TARGET", "providerMessageId": "REACTION"}})
    pair("forward_message", {"to": G, "sourceChatJid": G, "sourceMessageId": "OLD"},
         {"forwarded": {"messageId": "OLD-FORWARD"}})
    for type_, payload in [("send_text", {"to": G, "text": "failed"}),
                           ("send_poll", {"to": G, "question": "failed", "options": ["a", "b"]}),
                           ("forward_message", {"to": G, "sourceChatJid": G, "sourceMessageId": "SOURCE"}),
                           ("delete_message", {"chatJid": G, "messageId": "FAILED"}),
                           ("react", {"chatJid": G, "messageId": "FAILED", "emoji": "x"})]:
        pair(type_, payload, error="provider failed")
        pair(type_, payload)  # pending
    pair("send_text", {"to": G, "text": "client-only"},
         {"sent": {"messageId": "CLIENT", "clientMessageId": "CLIENT"}})
    pair("react", {"chatJid": G, "messageId": "FAILED", "emoji": "x"}, {})
    original = copy.deepcopy(lines)
    ex = extract(lines)
    assert {m.native_id for m in ex.messages} == {"POLL", "FORWARD", "OLD-FORWARD"}
    by_id = {m.native_id: m for m in ex.messages}
    assert by_id["POLL"].text is None and by_id["POLL"].media == {"kind": "poll", "poll": poll}
    assert by_id["FORWARD"].text == "Picture"
    assert by_id["FORWARD"].media == {**content["media"], "forward": {
        "forwarded": True, "sourceChatJid": content["sourceChatJid"], "sourceMessageId": "SOURCE",
        "provenance": "source"}}
    assert by_id["OLD-FORWARD"].text is None
    assert all(m.direction == "out" and m.from_assistant and m.sender is None for m in ex.messages)
    assert [(e.kind, e.target_native_id) for e in ex.events] == [("delete", "TARGET"), ("reaction", "TARGET")]
    assert ex.events[1].native_event_id == "REACTION"
    assert all(e.from_assistant and e.extra_refs for e in ex.events)
    assert ex.review["outbound_content_gaps"][0]["native_message_id"] == "OLD-FORWARD"
    assert sum(ex.outcomes.values()) == len(lines)
    assert lines == original
    # A single request/result with different command types is a correlation conflict too.
    mismatched = [raw(100, "outbound_request", "send_poll", {"to": G}, correlation_id="mismatch"),
                  raw(101, "outbound_result", "send_text", {}, correlation_id="mismatch",
                      native={"result": {"sent": {"messageId": "WRONG"}}})]
    mismatch = extract(mismatched)
    assert not mismatch.messages and not mismatch.events
    assert mismatch.review["outbound_correlations"][0]["correlation_id"] == "mismatch"
    assert sum(mismatch.outcomes.values()) == 2


def test_outbound_result_validator_parity():
    from yeoman_shared.whatsapp_protocol import valid_forward_content, valid_poll_result

    poll = {"name": "Lunch?", "values": ["one", "two"], "selectableCount": 1}
    content = {"text": "sent", "caption": None, "media": None, "forwarded": True,
               "sourceChatJid": G, "sourceMessageId": "SOURCE", "provenance": "sent"}
    cases = [
        ("supplementary-name", valid_poll_result, {**poll, "name": "😀" * 300}, False),
        ("blank-source", valid_forward_content, {**content, "sourceChatJid": " "}, False),
        ("unsafe-bytes", valid_forward_content, {**content, "media": {"kind": "image", "bytes": 9007199254740992}}, False),
        ("array-provenance", valid_forward_content, {**content, "provenance": ["sent"]}, False),
        ("utf16-limit", valid_poll_result, {**poll, "name": "😀" * 256}, True),
        ("utf16-over-limit", valid_poll_result, {**poll, "name": "😀" * 257}, False),
        ("blank-name", valid_poll_result, {**poll, "name": " \ufeff "}, False),
        ("blank-option", valid_poll_result, {**poll, "values": ["one", "\ufeff"]}, False),
        ("safe-bytes", valid_forward_content, {**content, "media": {"kind": "image", "bytes": 9007199254740991}}, True),
        ("padded-source", valid_forward_content, {**content, "sourceChatJid": f" {G} "}, True),
        ("blank-message", valid_forward_content, {**content, "sourceMessageId": "\ufeff"}, False),
        ("js-nonwhitespace", valid_forward_content, {**content, "sourceMessageId": "\u0085"}, True),
        ("integral-number", valid_poll_result, {**poll, "selectableCount": 1.0}, True),
        ("fractional-number", valid_poll_result, {**poll, "selectableCount": 1.5}, False),
        ("boolean-number", valid_poll_result, {**poll, "selectableCount": True}, False),
    ]
    results = [(name, validate(value), expected) for name, validate, value, expected in cases]
    assert all(actual is expected for _, actual, expected in results), results
