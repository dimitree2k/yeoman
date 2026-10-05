from hist_fixtures import INBOUND_DDL, make_db
from yeoman_gateway.history.convert.inbound_db import convert_inbound_db


def test_reply_context_rows(tmp_path):
 make_db(tmp_path / "data/inbound/reply_context.db", INBOUND_DDL, {"inbound_messages": [
  {"channel":"whatsapp","chat_id":"491786127564-1611913127@g.us","message_id":"3B0DF74F","participant":"4917632625469@s.whatsapp.net","sender_id":"4917632625469","text":"nein, das ist das Gegenteil","timestamp":1788945095,"created_at":"2026-09-09T09:11:35.634512+00:00","sender_name":"Frank Taeger","reply_to_message_id":None},
  {"channel":"whatsapp","chat_id":"1-2@g.us","message_id":"M2","participant":None,"sender_id":"211978110906443","text":"[Image]","timestamp":None,"created_at":"2026-09-09T09:11:35+00:00","sender_name":None,"reply_to_message_id":"M1"},
 ]})
 first, second = list(convert_inbound_db(tmp_path, "data/inbound/reply_context.db", "reply_context"))
 assert first["kind"] == "message" and first["provenance"] == "native"
 assert first["time_certainty"] == "provider_timestamp" and first["occurred_ms"] == 1788945095000
 assert first["payload"] == {"chatJid":"491786127564-1611913127@g.us","messageId":"3B0DF74F","participantJid":"4917632625469@s.whatsapp.net","senderId":"4917632625469","senderName":"Frank Taeger","text":"nein, das ist das Gegenteil"}
 assert first["origin"]["store"] == "reply_context" and first["origin"]["row_key"] == "1"
 assert first["original"]["sender_name"] == "Frank Taeger"
 assert second["time_certainty"] == "capture_time_approx"
 assert second["payload"]["mediaKind"] == "image" and "text" not in second["payload"]
 assert second["payload"]["replyToMessageId"] == "M1"

def test_missing_db_yields_nothing(tmp_path):
 assert list(convert_inbound_db(tmp_path, "data/inbound/archive.db", "inbound_archive")) == []
