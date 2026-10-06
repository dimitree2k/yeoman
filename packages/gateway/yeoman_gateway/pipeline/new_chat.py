"""Notify the owner when Yeoman encounters a new WhatsApp chat."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from yeoman_shared.utils.helpers import get_operational_store_path

from yeoman_gateway.core.intents import SendOutboundIntent
from yeoman_gateway.core.models import OutboundEvent
from yeoman_gateway.core.pipeline import NextFn, PipelineContext


def _timestamp(value: object) -> tuple[datetime, str] | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC), value


def _source_seen(data: object) -> tuple[set[str], dict[str, str]]:
    if not isinstance(data, dict):
        return set(), {}
    chats = data.get("chats", [])
    timestamps: dict[str, str] = {}
    if isinstance(chats, list):
        ids = {chat for chat in chats if isinstance(chat, str)}
    elif isinstance(chats, dict):
        ids = {chat for chat in chats if isinstance(chat, str)}
        for chat, value in chats.items():
            parsed = _timestamp(value)
            if isinstance(chat, str) and parsed:
                timestamps[chat] = parsed[1]
    else:
        ids = set()
    first_seen = data.get("first_seen", {})
    if isinstance(first_seen, dict):
        for chat, value in first_seen.items():
            parsed = _timestamp(value)
            if isinstance(chat, str) and chat in ids and parsed:
                timestamps[chat] = parsed[1]
    return ids, timestamps


def merge_seen_chat_files(sources: list[Path], destination: Path | None = None) -> Path:
    """Merge caller-selected legacy files without modifying them."""
    chats: set[str] = set()
    timestamps: dict[str, datetime] = {}
    timestamp_values: dict[str, str] = {}
    for source in sources:
        try:
            data = json.loads(source.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        source_chats, source_timestamps = _source_seen(data)
        chats.update(source_chats)
        for chat, value in source_timestamps.items():
            parsed = _timestamp(value)
            if parsed and (chat not in timestamps or parsed[0] < timestamps[chat]):
                timestamps[chat] = parsed[0]
                timestamp_values[chat] = value

    target = destination or get_operational_store_path("seen_chats")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {"chats": sorted(chats), "first_seen": dict(sorted(timestamp_values.items()))},
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    return target


class NewChatNotifyMiddleware:
    """Send owner notification when yeoman joins a new WhatsApp chat."""

    def __init__(
        self,
        *,
        owner_alert_resolver: Callable[[str], list[str]] | None = None,
    ) -> None:
        self._owner_resolver = owner_alert_resolver
        self._notified: set[str] = set()

    async def __call__(self, ctx: PipelineContext, next: NextFn) -> None:
        if ctx.event.channel == "whatsapp" and self._owner_resolver is not None:
            self._maybe_notify(ctx)
        await next(ctx)

    def _maybe_notify(self, ctx: PipelineContext) -> None:
        event = ctx.event
        owners = self._owner_resolver(event.channel) if self._owner_resolver else []
        if not owners:
            return

        full_key = f"{event.channel}:{event.chat_id}"
        if full_key in self._notified:
            return

        # Check persistent storage.
        seen_chats_path = get_operational_store_path("seen_chats")
        seen_chats: set[str] = set()
        first_seen: dict[str, str] = {}
        try:
            if seen_chats_path.exists():
                seen_chats, first_seen = _source_seen(json.loads(seen_chats_path.read_text()))
        except Exception:
            seen_chats = set()
            first_seen = {}

        if full_key in seen_chats:
            self._notified.add(full_key)
            return

        # Mark as seen immediately.
        self._notified.add(full_key)
        seen_chats.add(full_key)
        first_seen.setdefault(full_key, event.timestamp.astimezone(UTC).isoformat())
        try:
            seen_chats_path.parent.mkdir(parents=True, exist_ok=True)
            seen_chats_path.write_text(
                json.dumps(
                    {"chats": sorted(seen_chats), "first_seen": dict(sorted(first_seen.items()))},
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            )
        except Exception:
            pass

        # Fetch group info.
        group_name = None
        group_desc = None
        try:
            from yeoman_gateway.storage.chat_registry import ChatRegistry

            registry = ChatRegistry()
            try:
                chat_info = registry.get_chat(event.channel, event.chat_id)
                if chat_info:
                    group_name = chat_info.get("readable_name")
                    group_desc = chat_info.get("description")
            finally:
                registry.close()
        except Exception:
            pass

        if not group_name:
            group_name = event.raw_metadata.get("group_name") or event.raw_metadata.get("subject")
        if not group_desc:
            group_desc = event.raw_metadata.get("group_desc") or event.raw_metadata.get(
                "description"
            )

        is_group = event.chat_id.endswith("@g.us")
        chat_type = "group" if is_group else "chat"

        lines = [
            f"🔔 Arvid was added to a new WhatsApp {chat_type}",
        ]
        if group_name:
            lines.append(f"📛 Name: {group_name}")
        if group_desc:
            lines.append(f"📝 Description: {group_desc}")
        if is_group:
            lines.append(f"Group approval: `{event.chat_id}`")
            lines.append("")
            lines.append("Reply to this message with yes/ja or no/nein.")
            lines.append("Approved groups are mention-only with spontaneity disabled.")
        else:
            lines.append(f"🆔 ID: `{event.chat_id}`")

        message = "\n".join(lines)

        normalized_targets: list[str] = []
        for raw in owners:
            target = _normalize_owner_target(event.channel, raw)
            if target:
                normalized_targets.append(target)

        for target in sorted(set(normalized_targets)):
            ctx.intents.append(
                SendOutboundIntent(
                    event=OutboundEvent(
                        channel=event.channel,
                        chat_id=target,
                        content=message,
                    )
                )
            )


def _normalize_owner_target(channel: str, raw: str) -> str | None:
    """Normalize an owner target string to a valid channel address."""
    value = str(raw or "").strip()
    if not value:
        return None
    if channel != "whatsapp":
        return value
    if "@" in value:
        return value
    digits = "".join(ch for ch in value if ch.isdigit())
    if not digits:
        return None
    return f"{digits}@s.whatsapp.net"
