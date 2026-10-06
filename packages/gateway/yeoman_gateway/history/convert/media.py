"""document_cache.db: media metadata (backfill) and generated OCR/description text (derived)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

from yeoman_shared.utils.helpers import get_operational_store_path

from ..layer1 import Origin, backfill_line, row_sha256
from .common import compact, epoch_or_iso_to_ms, open_ro, row_dict, table_exists

DB_REL = get_operational_store_path("document_cache", data_dir=Path("data")).as_posix()


def convert_media_records(source_home: Path) -> Iterator[dict[str, Any]]:
    path = get_operational_store_path("document_cache", data_dir=source_home / "data")
    if not path.is_file():
        return
    with closing(open_ro(path)) as conn:
        if not table_exists(conn, "media_items"):
            return
        for row in conn.execute("SELECT * FROM media_items ORDER BY id"):
            item = row_dict(row)
            occurred_ms, certainty = epoch_or_iso_to_ms(item.get("timestamp"))
            media = compact({"kind": item.get("kind"), "mimeType": item.get("mime_type"),
                             "fileName": item.get("file_name"), "path": item.get("local_path"),
                             "bytes": item.get("size_bytes")})
            yield backfill_line(
                channel=item.get("channel") or "whatsapp", kind="media_record", provenance="native",
                time_certainty=certainty, occurred_ms=occurred_ms, direction=None,
                chat_id=item.get("chat_id"),
                payload=compact({"chatJid": item.get("chat_id"), "messageId": item.get("message_id"),
                                 "media": media}),
                origin=Origin("document_cache", DB_REL, "media_items", str(item["id"])), original=item,
            )


def convert_media_descriptions(source_home: Path) -> Iterator[dict[str, Any]]:
    path = get_operational_store_path("document_cache", data_dir=source_home / "data")
    if not path.is_file():
        return
    with closing(open_ro(path)) as conn:
        if not (table_exists(conn, "media_items") and table_exists(conn, "media_extractions")):
            return
        query = ("SELECT x.*, i.channel AS i_channel, i.chat_id AS i_chat_id, i.message_id AS i_message_id"
                 " FROM media_extractions x JOIN media_items i ON i.id = x.media_item_id ORDER BY x.id")
        for row in conn.execute(query):
            joined = row_dict(row)
            linkage = {key: joined.pop(f"i_{key}") for key in ("channel", "chat_id", "message_id")}
            original = joined
            generated_ms, _ = epoch_or_iso_to_ms(original.get("created_at"))
            yield {
                "derived_version": 1,
                "kind": "media_description",
                "channel": linkage["channel"] or "whatsapp",
                "chat_id": linkage["chat_id"],
                "native_message_id": linkage["message_id"],
                "mode": "ocr" if str(original.get("mode", "")).startswith("ocr") else "description",
                "generator": None,
                "generated_ms": generated_ms,
                "text": original.get("content"),
                "origin": {"store": "document_cache", "path": DB_REL, "table": "media_extractions",
                           "row_key": str(original["id"]), "row_sha256": row_sha256(original)},
                "original": original,
            }
