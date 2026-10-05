import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from yeoman_gateway.history.convert.bridge_refs import convert_bridge_refs, node_batch_decoder

G = "4917623568044-1542142755@g.us"


def _ref(folder: Path, name: str, encoded: str | None = "QUJD") -> None:
    folder.mkdir(parents=True, exist_ok=True)
    record = {"chatJid": G, "messageId": name, "storedAtMs": 1790527886765, "expiresAtMs": 1, "encoded": encoded}
    (folder / f"{name}.json").write_text(json.dumps(record), encoding="utf-8")


def _fake(values):
    def decode(items):
        return {name: values[Path(name).stem] for name, _ in items}
    return decode


def test_decoded_records(tmp_path):
    cold, daily = tmp_path / "cold", tmp_path / "daily"
    for name in ("text", "react", "revoke", "edit", "mine", "broken"):
        _ref(cold, name)
    _ref(daily, "text")
    _ref(daily, "empty", encoded=None)
    values = {
        "text": {"value": {"key": {"remoteJid": G, "id": "AC91", "participant": "157646925647975@lid",
                                   "participantAlt": "4917600000000@s.whatsapp.net"},
                           "message": {"extendedTextMessage": {"text": "Und ja, zu teuer",
                                                               "contextInfo": {"stanzaId": "AC90"}}},
                           "messageTimestamp": "1790527886", "pushName": "Moe"}},
        "react": {"value": {"key": {"remoteJid": G, "id": "R1", "participant": "1@lid"},
                            "message": {"reactionMessage": {"key": {"id": "AC91"}, "text": "😂"}}}},
        "revoke": {"value": {"key": {"remoteJid": G, "id": "P1", "participant": "1@lid"},
                             "message": {"protocolMessage": {"type": "REVOKE", "key": {"id": "AC91"}}}}},
        "edit": {"value": {"key": {"remoteJid": G, "id": "P2", "participant": "1@lid"},
                           "message": {"protocolMessage": {"type": "MESSAGE_EDIT", "key": {"id": "AC91"},
                                       "editedMessage": {"conversation": "teuer!"}}}}},
        "mine": {"value": {"key": {"remoteJid": G, "id": "3EB0", "fromMe": True},
                           "message": {"imageMessage": {"caption": "Bestes Gemälde"}}}},
        "broken": {"error": "invalid wire type"},
    }
    lines = {x["origin"]["row_key"]: x for x in convert_bridge_refs([cold, daily], _fake(values))}
    text = lines["text.json"]
    assert text["kind"] == "message" and text["provenance"] == "native"
    assert text["payload"] == {"chatJid": G, "messageId": "AC91", "participantJid": "157646925647975@lid",
                               "senderPhoneJid": "4917600000000@s.whatsapp.net",
                               "senderId": "157646925647975@lid", "senderName": "Moe",
                               "text": "Und ja, zu teuer", "replyToMessageId": "AC90"}
    assert text["occurred_ms"] == 1790527886000 and text["time_certainty"] == "provider_timestamp"
    assert text["origin"]["path"].endswith("cold/text.json")
    assert lines["react.json"]["payload"]["emoji"] == "😂" and lines["react.json"]["payload"]["removed"] is False
    assert lines["react.json"]["payload"]["nativeEventId"] == "R1"
    assert lines["react.json"]["payload"]["targetMessageId"] == "AC91"
    assert lines["react.json"]["time_certainty"] == "capture_time_approx"
    assert lines["revoke.json"]["kind"] == "delete"
    assert lines["revoke.json"]["payload"]["nativeEventId"] == "P1"
    assert lines["edit.json"]["kind"] == "edit" and lines["edit.json"]["payload"]["text"] == "teuer!"
    assert lines["edit.json"]["payload"]["nativeEventId"] == "P2"
    assert lines["edit.json"]["payload"]["targetMessageId"] == "AC91"
    mine = lines["mine.json"]
    assert mine["direction"] == "out" and mine["payload"]["fromAssistant"] is True
    assert mine["payload"]["text"] == "Bestes Gemälde" and mine["payload"]["mediaKind"] == "image"
    assert lines["broken.json"]["skip_reason"] == "decode_failed"
    assert lines["empty.json"]["skip_reason"] == "encoded_missing"
    assert len(lines) == 7


def test_without_decoder(tmp_path):
    _ref(tmp_path / "refs", "x")
    (line,) = convert_bridge_refs([tmp_path / "refs"], None)
    assert (line["kind"], line["skip_reason"]) == ("bridge_reference", "decoder_not_supplied")


BRIDGE = Path(os.environ.get("YEOMAN_BRIDGE_DIR", "~/.yeoman/var/cache/bridge")).expanduser()


@pytest.mark.skipif(shutil.which("node") is None or not (BRIDGE / "node_modules").is_dir(),
                    reason="needs node and the bridge runtime")
def test_node_batch_decoder_roundtrip():
    script = (
        "import { proto } from '@whiskeysockets/baileys/WAProto/index.js';"
        "const m = proto.WebMessageInfo.encode({key:{remoteJid:'1-2@g.us',id:'X1',participant:'5@lid'},"
        "message:{conversation:'hi'},messageTimestamp:5}).finish();"
        "process.stdout.write(Buffer.from(m).toString('base64'));"
    )
    encoded = subprocess.run(["node", "--input-type=module", "-e", script], cwd=BRIDGE,
                             capture_output=True, check=True, text=True).stdout
    result = node_batch_decoder(BRIDGE)([("a.json", encoded), ("b.json", "!!notbase64")])
    assert result["a.json"]["value"]["key"]["participant"] == "5@lid"
    assert result["a.json"]["value"]["message"]["conversation"] == "hi"
    assert "error" in result["b.json"]
