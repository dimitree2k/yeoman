"""New-chat notification middleware (WhatsApp only).

Corresponds to orchestrator stage 8: notify the owner when the bot encounters
a new WhatsApp chat for the first time, with quick approval shortcuts.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from yeoman.core.intents import SendOutboundIntent
from yeoman.core.models import OutboundEvent
from yeoman.core.pipeline import NextFn, PipelineContext


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
        seen_chats_path = Path.home() / ".yeoman" / "seen_chats.json"
        seen_chats: set[str] = set()
        try:
            if seen_chats_path.exists():
                data = json.loads(seen_chats_path.read_text())
                seen_chats = set(data.get("chats", []))
        except Exception:
            seen_chats = set()

        if full_key in seen_chats:
            self._notified.add(full_key)
            return

        # Mark as seen immediately.
        self._notified.add(full_key)
        seen_chats.add(full_key)
        try:
            seen_chats_path.parent.mkdir(parents=True, exist_ok=True)
            seen_chats_path.write_text(json.dumps({"chats": list(seen_chats)}))
        except Exception:
            pass

        # Fetch group info.
        group_name = None
        group_desc = None
        try:
            from yeoman.storage.chat_registry import ChatRegistry

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
            f"🔔 Nano was added to a new WhatsApp {chat_type}",
        ]
        if group_name:
            lines.append(f"📛 Name: {group_name}")
        if group_desc:
            lines.append(f"📝 Description: {group_desc}")
        lines.append(f"🆔 ID: `{event.chat_id}`")
        lines.append("")
        lines.append("⚡ Quick commands:")
        lines.append(f"  /approve {event.chat_id}  → allow + reply all")
        lines.append(f"  /approve-mention {event.chat_id}  → allow + mention only")
        lines.append(f"  /deny {event.chat_id}  → block")
        lines.append("")
        lines.append("Or use full commands:")
        lines.append(f"  /policy allow-group {event.chat_id}")
        lines.append(f"  /policy set-when {event.chat_id} all|mention_only")
        lines.append(f"  /policy block-group {event.chat_id}")

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
