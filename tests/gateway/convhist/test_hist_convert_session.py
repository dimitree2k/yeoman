
from hist_fixtures import write_jsonl
from yeoman_gateway.history.convert.session_jsonl import convert_session_jsonl

GROUP = "491786127564-1611913127@g.us"


def test_group_session_lines(tmp_path):
    write_jsonl(tmp_path / f"data/inbound/whatsapp_{GROUP}_thread_th_761d.jsonl", [
        {"_type": "metadata", "created_at": "2026-02-22T22:18:34", "metadata": {}},
        {"role": "user", "content": "[Image] @203075365150770 wie belastbar", "timestamp": "1789828958",
         "sender_id": "491757070305", "sender_name": "D.", "message_id": "3EB0DB35",
         "reply_to_message_id": "AC84"},
        {"role": "assistant", "content": "Ja, prinzipiell.", "timestamp": "2026-09-15T11:08:54.099666"},
        {"role": "tool_trace", "tool_name": "web_search", "timestamp": "2026-09-15T11:07:51"},
        {"role": "session_boundary", "timestamp": "2026-04-29T19:16:35"},
        {"role": "assistant", "content": "Voice message delivered", "timestamp": "2026-09-22T12:35:58",
         "hidden": "True", "synthetic": "True"},
        {"role": "user", "content": "ohne Absender", "timestamp": "2026-05-25T00:10:17"},
        '{"role": "user", "content": "kaputt',
    ])
    lines = list(convert_session_jsonl(tmp_path))
    kinds = [(x["kind"], x["skip_reason"]) for x in lines]
    assert kinds == [
        ("session_meta", "metadata"), ("message", None), ("message", None),
        ("session_meta", "tool_trace"), ("session_meta", "session_boundary"),
        ("session_meta", "synthetic"), ("message", None), ("session_meta", "invalid_json"),
    ]
    user, assistant = lines[1], lines[2]
    assert user["channel"] == "whatsapp" and user["chat_id"] == GROUP and user["direction"] == "in"
    assert user["payload"] == {"chatJid": GROUP, "messageId": "3EB0DB35", "senderId": "491757070305",
                               "senderName": "D.", "replyToMessageId": "AC84",
                               "text": "@203075365150770 wie belastbar", "mediaKind": "image"}
    assert user["time_certainty"] == "provider_timestamp"
    assert assistant["direction"] == "out" and assistant["payload"]["fromAssistant"] is True
    assert assistant["time_certainty"] == "capture_time_approx"
    assert "senderId" not in lines[6]["payload"]
    assert lines[1]["origin"]["path"] == f"data/inbound/whatsapp_{GROUP}_thread_th_761d.jsonl"
    assert lines[1]["origin"]["row_key"] == "2"


def test_one_to_one_sender_inferred_from_chat_and_telegram(tmp_path):
    write_jsonl(tmp_path / "data/inbound/whatsapp_34596062240904@lid.jsonl", [
        {"role": "user", "content": "hi", "timestamp": "2026-03-18T20:12:16"},
    ])
    write_jsonl(tmp_path / "data/inbound/telegram_453897507.jsonl", [
        {"role": "user", "content": "privet", "timestamp": "2026-03-18T20:12:16"},
    ])
    telegram, whatsapp = list(convert_session_jsonl(tmp_path))
    assert telegram["channel"] == "telegram" and telegram["chat_id"] == "453897507"
    assert whatsapp["payload"]["senderId"] == "34596062240904@lid"
    assert whatsapp["payload"]["senderInferredFromChat"] is True
