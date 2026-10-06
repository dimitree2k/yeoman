from hist_fixtures import make_db
from yeoman_gateway.history.convert.media import convert_media_descriptions, convert_media_records
from yeoman_gateway.history.layer1 import row_sha256
from yeoman_shared.utils.helpers import get_operational_store_path

DDL = """
CREATE TABLE media_items (id INTEGER PRIMARY KEY, channel TEXT, chat_id TEXT, message_id TEXT,
  sender_id TEXT, sender_name TEXT, kind TEXT, mime_type TEXT, file_name TEXT, local_path TEXT,
  size_bytes INTEGER, timestamp INTEGER, expires_at INTEGER);
CREATE TABLE media_extractions (id INTEGER PRIMARY KEY, media_item_id INTEGER, mode TEXT, content TEXT,
  char_count INTEGER, page_count INTEGER, created_at INTEGER);
"""


def test_media(tmp_path):
    make_db(get_operational_store_path("document_cache", data_dir=tmp_path / "data"), DDL, {
        "media_items": [{"id": 65, "channel": "whatsapp", "chat_id": "1-2@g.us", "message_id": "AC06",
                         "sender_id": "x", "sender_name": "y", "kind": "image", "mime_type": "image/jpeg",
                         "file_name": None, "local_path": "/m/AC06.jpg", "size_bytes": 10,
                         "timestamp": 1779997800, "expires_at": 1}],
        "media_extractions": [{"id": 1, "media_item_id": 65, "mode": "ocr_image", "content": "25. November",
                               "char_count": 12, "page_count": 1, "created_at": 1779997872}],
    })
    (record,) = convert_media_records(tmp_path)
    assert record["kind"] == "media_record" and record["payload"]["messageId"] == "AC06"
    assert record["payload"]["media"] == {"kind": "image", "mimeType": "image/jpeg",
                                          "path": "/m/AC06.jpg", "bytes": 10}
    (desc,) = convert_media_descriptions(tmp_path)
    assert desc["kind"] == "media_description" and desc["mode"] == "ocr"
    assert (desc["chat_id"], desc["native_message_id"], desc["text"]) == ("1-2@g.us", "AC06", "25. November")
    extraction = {"id": 1, "media_item_id": 65, "mode": "ocr_image", "content": "25. November",
                  "char_count": 12, "page_count": 1, "created_at": 1779997872}
    assert desc["generated_ms"] == 1779997872000 and desc["origin"]["row_key"] == "1"
    assert desc["original"] == extraction
    assert desc["origin"]["row_sha256"] == row_sha256(extraction)
