"""processing.db: native journal events (from September) and Arvid's sent effects."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

from yeoman_shared.utils.helpers import get_operational_store_path

from ..ids import SPEAKUP
from ..layer1 import Origin, backfill_line
from .common import clean_text, compact, loads_object, media_kind, open_ro, row_dict, table_exists

DB_REL = get_operational_store_path("processing", data_dir=Path("data")).as_posix()
_EVENT_KINDS = frozenset({"message", "reaction", "edit", "delete", "membership_snapshot",
                          "membership_change"})
_EXTENSION_KIND = {".ogg": "audio", ".opus": "audio", ".mp3": "audio", ".m4a": "audio",
                   ".jpg": "image", ".jpeg": "image", ".png": "image", ".webp": "image",
                   ".mp4": "video", ".gif": "video"}


def convert_journal(source_home: Path) -> Iterator[dict[str, Any]]:
    path = get_operational_store_path("processing", data_dir=source_home / "data")
    if not path.is_file():
        return
    with closing(open_ro(path)) as conn:
        if table_exists(conn, "events"):
            for row in conn.execute("SELECT * FROM events ORDER BY created_ms, event_id"):
                yield _event(row_dict(row))
        if table_exists(conn, "effects"):
            seen: set[str] = set()
            query = (
                "SELECT e.*, r.provider_message_id AS r_provider_message_id, r.chat_id AS r_chat_id,"
                " r.confirmed_ms AS r_confirmed_ms FROM effects e"
                " LEFT JOIN transport_receipts r ON r.effect_id = e.effect_id"
                " ORDER BY e.created_ms, e.effect_id, r.confirmed_ms"
            )
            for row in conn.execute(query):
                effect = row_dict(row)
                if effect["effect_id"] in seen:
                    continue
                seen.add(effect["effect_id"])
                yield _effect(effect)


def _event(row: dict[str, Any]) -> dict[str, Any]:
    payload_in = loads_object(row.get("payload_json"))
    if row.get("occurred_ms"):
        occurred_ms, certainty = int(row["occurred_ms"]), "provider_timestamp"
    else:
        occurred_ms, certainty = row.get("created_ms"), "capture_time_approx"
    kind = row.get("kind") or "unknown"
    origin = Origin("journal", DB_REL, "events", str(row["event_id"]))
    common = dict(channel=row.get("channel") or "whatsapp", provenance="native",
                  time_certainty=certainty, occurred_ms=occurred_ms,
                  direction=row.get("direction") or "in", chat_id=row.get("chat_id"),
                  origin=origin, original=row)
    if kind not in _EVENT_KINDS:
        reason = "receipt" if kind == "receipt" else f"journal_kind:{kind}"
        return backfill_line(kind=kind, payload={}, skip_reason=reason, **common)
    outgoing = row.get("direction") == "out"
    payload: dict[str, Any] = {
        "chatJid": row.get("chat_id"),
        "journalEventId": row["event_id"],
        "payloadPurged": True if row.get("payload_purged_ms") else None,
        "fromAssistant": True if outgoing else None,
        "addressee": row.get("principal") if outgoing else None,
        "senderId": None if outgoing else row.get("principal"),
    }
    target = row.get("target_message_id") or payload_in.get("target_message_id")
    if kind == "message":
        cleaned = clean_text(payload_in.get("text"))
        media = payload_in.get("media")
        payload.update({
            "messageId": payload_in.get("provider_message_id") or payload_in.get("message_id")
                         or row.get("source_message_id"),
            "participantJid": payload_in.get("participant_jid"),
            "senderPhoneJid": payload_in.get("sender_phone_jid"),
            "senderName": payload_in.get("sender_name"),
            "text": cleaned.text,
            "replyToMessageId": payload_in.get("reply_to_message_id") or payload_in.get("reply_to"),
            "media": media if isinstance(media, dict) else None,
            "mediaKind": payload_in.get("media_kind")
                         or (media_kind(cleaned.placeholder) if cleaned.placeholder else None),
            "generatedDescription": cleaned.description,
        })
    elif kind == "reaction":
        payload.update({"targetMessageId": target, "emoji": payload_in.get("emoji"),
                        "removed": bool(payload_in.get("removed"))})
    elif kind == "edit":
        payload.update({"targetMessageId": target, "text": payload_in.get("text")})
    elif kind == "delete":
        payload.update({"targetMessageId": target})
    elif kind in {"membership_snapshot", "membership_change"}:
        membership = {k: v for k, v in payload_in.items() if k not in payload}
        participants = membership.get("participants")
        if isinstance(participants, list):
            membership["participants"] = [
                ({**item, "phoneJid": item["phone_jid"]} if isinstance(item, dict)
                 and "phone_jid" in item and "phoneJid" not in item else item)
                for item in participants
            ]
            for item in membership["participants"]:
                if isinstance(item, dict):
                    item.pop("phone_jid", None)
        payload.update(membership)
    else:
        payload.update({k: v for k, v in payload_in.items() if k not in payload})
    payload = compact(payload)
    if kind == "reaction":
        payload["removed"] = bool(payload_in.get("removed"))
    return backfill_line(kind=kind, payload=payload, **common)


def _media_from_paths(paths: Any) -> dict[str, Any] | None:
    if not isinstance(paths, list) or not paths:
        return None
    first = str(paths[0])
    suffix = first[first.rfind("."):].lower() if "." in first else ""
    return {"kind": _EXTENSION_KIND.get(suffix, "document"), "path": first}


def _effect(row: dict[str, Any]) -> dict[str, Any]:
    payload_in = loads_object(row.get("payload_json"))
    target = loads_object(row.get("target_json"))
    chat = row.get("r_chat_id") or target.get("chat_id")
    origin = Origin("journal", DB_REL, "effects", str(row["effect_id"]))
    occurred_ms = row.get("r_confirmed_ms") or row.get("updated_ms")
    kind_in, state = row.get("payload_kind"), row.get("state")
    common = dict(channel=target.get("channel") or "whatsapp", provenance="native",
                  time_certainty="capture_time_approx" if occurred_ms else "unknown",
                  occurred_ms=occurred_ms, direction="out", chat_id=chat, origin=origin, original=row)
    if state != "sent" or kind_in not in ("text", "media", "reaction"):
        return backfill_line(kind="effect", payload={}, skip_reason=f"effect:{kind_in}:{state}", **common)
    principal = row.get("principal")
    payload: dict[str, Any] = {
        "chatJid": chat, "fromAssistant": True, "effectId": row["effect_id"],
        "origin": "speakup" if principal == SPEAKUP else None,
        "addressee": None if principal == SPEAKUP else principal,
        "payloadPurged": True if row.get("payload_purged_ms") else None,
    }
    if kind_in == "reaction":
        payload.update({"targetMessageId": payload_in.get("message_id"), "emoji": payload_in.get("emoji")})
        payload = compact(payload)
        payload["removed"] = False
        return backfill_line(kind="reaction", payload=payload, **common)
    payload.update({
        "messageId": row.get("r_provider_message_id"),
        "text": payload_in.get("text"),
        "replyToMessageId": payload_in.get("reply_to"),
        "media": _media_from_paths(payload_in.get("media")) if kind_in == "media" else None,
    })
    return backfill_line(kind="message", payload=compact(payload), **common)
