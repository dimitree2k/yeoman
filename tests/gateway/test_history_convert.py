from __future__ import annotations

import sqlite3

from yeoman_gateway.history.convert.journal import convert_journal
from yeoman_gateway.history.convert.media import convert_media_records


def test_journal_converter_reads_processing_store_under_ops(tmp_path):
    db_path = tmp_path / "data" / "ops" / "processing.db"
    db_path.parent.mkdir(parents=True)
    with sqlite3.connect(db_path) as db:
        db.execute(
            "CREATE TABLE events (event_id TEXT, kind TEXT, created_ms INTEGER, "
            "payload_json TEXT, channel TEXT, chat_id TEXT, direction TEXT, "
            "trace_id TEXT, event_key TEXT, occurred_ms INTEGER)"
        )
        db.execute(
            "INSERT INTO events VALUES "
            "('e1', 'message', 1, '{\"text\":\"hi\"}', 'whatsapp', 'chat', 'in', 't1', 'k1', 1)"
        )

    lines = list(convert_journal(tmp_path))

    assert len(lines) == 1
    assert lines[0]["origin"]["path"] == "data/ops/processing.db"


def test_media_converter_reads_document_cache_under_ops(tmp_path):
    db_path = tmp_path / "data" / "ops" / "document-cache.db"
    db_path.parent.mkdir(parents=True)
    with sqlite3.connect(db_path) as db:
        db.execute(
            "CREATE TABLE media_items (id INTEGER, channel TEXT, chat_id TEXT, "
            "message_id TEXT, timestamp TEXT, kind TEXT, mime_type TEXT, "
            "file_name TEXT, local_path TEXT, size_bytes INTEGER)"
        )
        db.execute(
            "INSERT INTO media_items VALUES (1, 'whatsapp', 'chat', 'm1', '1', "
            "'image', 'image/png', 'x.png', '/tmp/x.png', 1)"
        )

    lines = list(convert_media_records(tmp_path))

    assert len(lines) == 1
    assert lines[0]["origin"]["path"] == "data/ops/document-cache.db"
