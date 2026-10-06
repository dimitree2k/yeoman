import json

from hist_fixtures import make_db
from yeoman_gateway.history.convert.journal import convert_journal

DDL = """
CREATE TABLE events (event_id TEXT PRIMARY KEY, kind TEXT, origin TEXT, channel TEXT, chat_id TEXT,
  principal TEXT, source_message_id TEXT, target_message_id TEXT, occurred_ms INTEGER,
  payload_json TEXT, payload_purged_ms INTEGER, created_ms INTEGER, account TEXT, direction TEXT,
  revision INTEGER);
CREATE TABLE effects (effect_id TEXT PRIMARY KEY, principal TEXT, capability TEXT, target_json TEXT,
  payload_kind TEXT, payload_json TEXT, payload_purged_ms INTEGER, state TEXT, created_ms INTEGER,
  updated_ms INTEGER, origin TEXT);
CREATE TABLE transport_receipts (receipt_id TEXT PRIMARY KEY, effect_id TEXT, channel TEXT,
  chat_id TEXT, provider_message_id TEXT, confirmed_ms INTEGER);
"""
G = "4917623568044-1542142755@g.us"


def _event(eid, kind, **kw):
    base = {"event_id": eid, "kind": kind, "origin": "whatsapp_canonical", "channel": "whatsapp",
            "chat_id": G, "principal": "", "source_message_id": None, "target_message_id": None,
            "occurred_ms": 1791038794000, "payload_json": None, "payload_purged_ms": None,
            "created_ms": 1791038795000, "account": "default", "direction": "in", "revision": 1}
    return {**base, **kw}


def _effect(eid, kind, payload, state="sent", principal="4915774497527"):
    return {"effect_id": eid, "principal": principal, "capability": f"send_{kind}",
            "target_json": json.dumps({"channel": "whatsapp", "chat_id": G}), "payload_kind": kind,
            "payload_json": json.dumps(payload) if payload else None, "payload_purged_ms": None,
            "state": state, "created_ms": 1, "updated_ms": 2, "origin": "legacy"}


def _home(tmp_path):
    make_db(tmp_path / "data/ops/processing.db", DDL, {
        "events": [
            _event("e1", "message", principal="143855651442872", source_message_id="AC06",
                   payload_json=json.dumps({"text": "hallo", "participant_jid": "143855651442872@lid",
                                            "sender_phone_jid": "4917632625469@s.whatsapp.net",
                                            "sender_name": "Frank Taeger", "reply_to_message_id": "AC01",
                                            "reply_to_participant": "262478487384124@lid",
                                            "media": {"kind": "image", "path": "/x.jpg"}})),
            _event("e2", "message", principal="4917623568044", source_message_id="AC07",
                   payload_purged_ms=5),
            _event("e3", "message", principal="491757070305", direction="out", source_message_id="3EB0",
                   payload_json=json.dumps({"text": "Antwort", "provider_message_id": "3EB0"})),
            _event("e4", "reaction", principal="4917623568044-1542142755", target_message_id="3EB0",
                   occurred_ms=None, payload_json=json.dumps({"emoji": "😂", "removed": False})),
            _event("e5", "edit", principal="34596062240904", target_message_id="AC06",
                   payload_json=json.dumps({"text": "hallo!"})),
            _event("e6", "delete", target_message_id="AC06"),
            _event("e7", "receipt"),
            _event("e8", "membership_snapshot", payload_json=json.dumps(
                {"participants": [{"lid": "1@lid", "phoneJid": "2@s.whatsapp.net"}], "complete": True})),
        ],
        "effects": [
            _effect("f1", "text", {"kind": "text", "text": "Weil heute", "reply_to": "ACCD"}),
            _effect("f2", "reaction", {"emoji": "👀", "kind": "reaction", "message_id": "3EB09548"}),
            _effect("f3", "media", {"kind": "media", "media": ["/tts/x.ogg"]}),
            _effect("f4", "text", {"kind": "text", "text": "nie gesendet"}, state="failed"),
            _effect("f5", "text", None, principal="service:speakup"),
        ],
        "transport_receipts": [
            {"receipt_id": "r1", "effect_id": "f1", "channel": "whatsapp", "chat_id": G,
             "provider_message_id": "3EB0CC89", "confirmed_ms": 1791038797370},
            {"receipt_id": "r3", "effect_id": "f3", "channel": "whatsapp", "chat_id": G,
             "provider_message_id": "3EB0MEDIA", "confirmed_ms": 1791038798000},
            {"receipt_id": "r5", "effect_id": "f5", "channel": "whatsapp", "chat_id": G,
             "provider_message_id": "3EB0SPEAK", "confirmed_ms": 1791038799000},
        ],
    })
    return list(convert_journal(tmp_path))


def test_events(tmp_path):
    lines = {x["origin"]["row_key"]: x for x in _home(tmp_path)}
    e1 = lines["e1"]
    assert e1["kind"] == "message" and e1["provenance"] == "native" and e1["direction"] == "in"
    assert e1["payload"]["participantJid"] == "143855651442872@lid"
    assert e1["payload"]["senderPhoneJid"] == "4917632625469@s.whatsapp.net"
    assert e1["payload"]["senderId"] == "143855651442872" and e1["payload"]["messageId"] == "AC06"
    assert e1["payload"]["media"] == {"kind": "image", "path": "/x.jpg"}
    assert "262478487384124@lid" not in json.dumps(e1["payload"])  # a reply's quoted participant is never the author
    assert lines["e2"]["payload"]["payloadPurged"] is True and "text" not in lines["e2"]["payload"]
    e3 = lines["e3"]["payload"]
    assert e3["fromAssistant"] is True and e3["addressee"] == "491757070305" and "senderId" not in e3
    e4 = lines["e4"]
    assert e4["time_certainty"] == "capture_time_approx" and e4["occurred_ms"] == 1791038795000
    assert e4["payload"]["senderId"] == "4917623568044-1542142755" and e4["payload"]["emoji"] == "😂"
    assert lines["e5"]["payload"]["targetMessageId"] == "AC06" and lines["e5"]["payload"]["text"] == "hallo!"
    assert lines["e6"]["kind"] == "delete"
    assert (lines["e7"]["kind"], lines["e7"]["skip_reason"]) == ("receipt", "receipt")
    assert lines["e8"]["payload"]["participants"][0]["lid"] == "1@lid"


def test_effects(tmp_path):
    lines = {x["origin"]["row_key"]: x for x in _home(tmp_path) if x["origin"]["table"] == "effects"}
    f1 = lines["f1"]
    assert (f1["kind"], f1["direction"]) == ("message", "out")
    assert f1["payload"]["messageId"] == "3EB0CC89" and f1["payload"]["text"] == "Weil heute"
    assert f1["payload"]["replyToMessageId"] == "ACCD" and f1["payload"]["addressee"] == "4915774497527"
    assert f1["occurred_ms"] == 1791038797370
    assert lines["f2"]["kind"] == "reaction" and lines["f2"]["payload"]["targetMessageId"] == "3EB09548"
    assert lines["f3"]["payload"]["media"] == {"kind": "audio", "path": "/tts/x.ogg"}
    assert (lines["f4"]["kind"], lines["f4"]["skip_reason"]) == ("effect", "effect:text:failed")
    assert lines["f5"]["payload"]["origin"] == "speakup" and "text" not in lines["f5"]["payload"]
