"""reply_context.db and inbound/archive.db: native inbound copies with participant JIDs."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

from ..layer1 import Origin, backfill_line
from .common import (
    clean_text,
    compact,
    epoch_or_iso_to_ms,
    media_kind,
    open_ro,
    row_dict,
    table_exists,
)


def convert_inbound_db(source_home: Path, db_rel: str, store: str) -> Iterator[dict[str, Any]]:
    path = source_home / db_rel
    if not path.is_file():
        return
    with closing(open_ro(path)) as conn:
        if not table_exists(conn, "inbound_messages"):
            return
        for row in conn.execute("SELECT rowid AS _rowid, * FROM inbound_messages ORDER BY rowid"):
            original = row_dict(row)
            rowid = original.pop("_rowid")
            cleaned = clean_text(original.get("text"))
            occurred_ms, certainty = epoch_or_iso_to_ms(original.get("timestamp"))
            if occurred_ms is None:
                occurred_ms, certainty = epoch_or_iso_to_ms(original.get("created_at"))
            sender = original.get("sender_id")
            payload = compact({
                "chatJid": original.get("chat_id"),
                "messageId": original.get("message_id"),
                "participantJid": original.get("participant"),
                "senderId": str(sender) if sender is not None else None,
                "senderName": original.get("sender_name"),
                "text": cleaned.text,
                "replyToMessageId": original.get("reply_to_message_id"),
                "generatedDescription": cleaned.description,
                "mediaKind": media_kind(cleaned.placeholder) if cleaned.placeholder else None,
            })
            yield backfill_line(
                channel=original.get("channel") or "whatsapp", kind="message", provenance="native",
                time_certainty=certainty, occurred_ms=occurred_ms, direction="in",
                chat_id=original.get("chat_id"), payload=payload,
                origin=Origin(store, db_rel, "inbound_messages", str(rowid)), original=original,
            )
