"""memory2_nodes (legacy memory): the only copy of most February-to-June messages."""

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
    loads_object,
    media_kind,
    open_ro,
    row_dict,
    table_exists,
)


def convert_memory_nodes(source_home: Path, db_rel: str, store: str) -> Iterator[dict[str, Any]]:
    path = source_home / db_rel
    if not path.is_file():
        return
    with closing(open_ro(path)) as conn:
        if not table_exists(conn, "memory2_nodes"):
            return
        for row in conn.execute("SELECT * FROM memory2_nodes ORDER BY created_at, id"):
            yield _line(row_dict(row), Origin(store, db_rel, "memory2_nodes", ""))


def _line(node: dict[str, Any], origin: Origin) -> dict[str, Any]:
    origin = Origin(origin.store, origin.path, origin.table, str(node.get("id")))
    occurred_ms, _ = epoch_or_iso_to_ms(node.get("created_at"))
    common = dict(
        channel=node.get("channel") or "unknown",
        time_certainty="capture_time_approx" if occurred_ms is not None else "unknown",
        occurred_ms=occurred_ms,
        chat_id=node.get("chat_id"),
        origin=origin,
        original=node,
    )
    if node.get("kind") != "utterance":
        return backfill_line(
            kind="memory_fact",
            provenance="derived_only",
            direction=None,
            payload={},
            skip_reason="derived_memory_fact",
            **common,
        )
    meta = loads_object(node.get("meta_json"))
    outgoing = meta.get("direction") == "out" or node.get("source_role") == "assistant"
    cleaned = clean_text(node.get("content"))
    sender = node.get("sender_id")
    payload = compact(
        {
            "chatJid": node.get("chat_id"),
            "messageId": node.get("source_message_id"),
            "senderId": None if outgoing or sender is None else str(sender),
            "fromAssistant": True if outgoing else None,
            "contactRef": node.get("contact_id"),
            "text": cleaned.text,
            "generatedDescription": cleaned.description,
            "mediaKind": media_kind(cleaned.placeholder) if cleaned.placeholder else None,
        }
    )
    return backfill_line(
        kind="message",
        provenance="derived_only" if cleaned.changed else "verbatim_unverified",
        direction="out" if outgoing else "in",
        payload=payload,
        skip_reason="deleted_in_source" if node.get("is_deleted") else None,
        **common,
    )
