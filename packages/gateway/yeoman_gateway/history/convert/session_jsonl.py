"""Session JSONL (data/inbound/*.jsonl): the gateway's per-chat transcripts since February."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ..ids import classify
from ..layer1 import Origin, backfill_line
from .common import clean_text, compact, epoch_or_iso_to_ms, media_kind

_FILE = re.compile(r"^(?P<channel>[a-z0-9]+)_(?P<chat>.+?)(?:_thread_.+)?\.jsonl$")
_SKIP_ROLES = {"tool_trace": "tool_trace", "session_boundary": "session_boundary", "system": "system_prompt"}


def convert_session_jsonl(source_home: Path) -> Iterator[dict[str, Any]]:
    folder = source_home / "data" / "inbound"
    if not folder.is_dir():
        return
    for path in sorted(folder.glob("*.jsonl")):
        match = _FILE.match(path.name)
        channel, chat = (match["channel"], match["chat"]) if match else ("unknown", path.stem)
        rel = path.relative_to(source_home).as_posix()
        with path.open(encoding="utf-8", errors="replace") as handle:
            for number, text in enumerate(handle, start=1):
                if not text.strip():
                    continue
                try:
                    record = json.loads(text)
                except json.JSONDecodeError:
                    record = None
                if not isinstance(record, dict):
                    record = {"_unparsed": text.rstrip("\n")}
                yield _line(channel, chat, Origin("session_jsonl", rel, "jsonl", str(number)), record)


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() == "true"


def _line(channel: str, chat: str, origin: Origin, record: dict[str, Any]) -> dict[str, Any]:
    occurred_ms, certainty = epoch_or_iso_to_ms(record.get("timestamp"))
    role = record.get("role")

    def meta(reason: str) -> dict[str, Any]:
        return backfill_line(
            channel=channel, kind="session_meta", provenance="derived_only", time_certainty=certainty,
            occurred_ms=occurred_ms, direction=None, chat_id=chat, payload={}, origin=origin,
            original=record, skip_reason=reason,
        )

    if "_unparsed" in record:
        return meta("invalid_json")
    if record.get("_type") == "metadata":
        return meta("metadata")
    if role in _SKIP_ROLES:
        return meta(_SKIP_ROLES[role])
    if role not in ("user", "assistant"):
        return meta(f"role:{role}")
    if _truthy(record.get("hidden")) or _truthy(record.get("synthetic")):
        return meta("synthetic")

    cleaned = clean_text(record.get("content"))
    payload: dict[str, Any] = {"chatJid": chat, "messageId": record.get("message_id")}
    if role == "assistant":
        direction = "out"
        payload["fromAssistant"] = True
    else:
        direction = "in"
        sender = record.get("sender_id")
        chat_ident = classify(chat)
        if sender:
            payload["senderId"] = str(sender)
        elif chat_ident is not None and chat_ident.kind in ("lid", "pn_jid"):
            payload["senderId"] = chat
            payload["senderInferredFromChat"] = True
        payload["senderName"] = record.get("sender_name")
        payload["replyToMessageId"] = record.get("reply_to_message_id")
    payload["text"] = cleaned.text
    payload["generatedDescription"] = cleaned.description
    payload["mediaKind"] = media_kind(cleaned.placeholder) if cleaned.placeholder else None
    return backfill_line(
        channel=channel, kind="message", provenance="verbatim_unverified", time_certainty=certainty,
        occurred_ms=occurred_ms, direction=direction, chat_id=chat, payload=compact(payload),
        origin=origin, original=record,
    )
