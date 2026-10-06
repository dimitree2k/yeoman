"""memory2_nodes (legacy memory): the only copy of most February-to-June messages."""

from __future__ import annotations

import re
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

_SENDER_MARKER = re.compile(r"\[(\+?\d+)\]\s*")
_BATCH_TAG = re.compile(r"^\s*\[group_notes_batch\]\s*")


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
    if node.get("source") == "auto_semantic_v2":
        segments = _batch_segments(node.get("content"), node.get("sender_id"),
                                   node.get("source_message_id"))
        payload = compact({
            "chatJid": node.get("chat_id"), "messageId": node.get("source_message_id"),
            "senderId": None if outgoing else node.get("sender_id"),
            "fromAssistant": True if outgoing else None, "contactRef": node.get("contact_id"),
            "segments": segments,
        })
        return backfill_line(
            kind="message", provenance="verbatim_unverified",
            direction="out" if outgoing else "in", payload=payload,
            skip_reason="deleted_in_source" if node.get("is_deleted") else None, **common,
        )
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


def _batch_segments(content: Any, fallback_sender: Any, last_message_id: Any) -> list[dict[str, Any]]:
    text = _BATCH_TAG.sub("", str(content or ""), count=1)
    markers = list(_SENDER_MARKER.finditer(text))
    parts: list[tuple[Any, str]] = []
    if markers:
        leading = text[:markers[0].start()].strip()
        if leading:
            parts.append((fallback_sender, leading))
        parts.extend((marker.group(1), text[marker.end():markers[i + 1].start() if i + 1 < len(markers)
                      else len(text)].strip()) for i, marker in enumerate(markers))
    else:
        parts.append((fallback_sender, text.strip()))
    result: list[dict[str, Any]] = []
    for index, (sender, value) in enumerate(parts):
        cleaned = clean_text(value)
        segment = {"senderId": str(sender) if sender is not None else None, "text": cleaned.text}
        if cleaned.description:
            segment["description"] = cleaned.description
        if cleaned.placeholder:
            segment["mediaKind"] = media_kind(cleaned.placeholder)
        if index == len(parts) - 1 and last_message_id:
            segment["messageId"] = last_message_id
        if cleaned.changed:
            segment["provenance"] = "derived_only"
        result.append(segment)
    return result
