"""Tool for fetching raw chat history for LLM summarization."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from yeoman_gateway.agent.tools.base import Tool

if TYPE_CHECKING:
    from yeoman_gateway.contacts.service import ContactsService
    from yeoman_gateway.storage.inbound_archive import InboundArchive

_MENTION_RE = re.compile(r"@(\d{10,})")
_LOCAL_TZ = datetime.now(timezone.utc).astimezone().tzinfo


class SummarizeHistoryTool(Tool):
    """Fetch raw chat history for summarization."""

    def __init__(
        self,
        archive: "InboundArchive",
        contacts: "ContactsService | None" = None,
    ) -> None:
        self._archive = archive
        self._contacts = contacts
        self._channel = ""
        self._chat_id = ""

    def set_context(self, channel: str, chat_id: str) -> None:
        self._channel = channel
        self._chat_id = chat_id

    @property
    def name(self) -> str:
        return "summarize_history"

    @property
    def description(self) -> str:
        return (
            "Fetch raw chat message history for summarization. "
            "Use when users ask to summarize, recap, or catch up on recent conversation. "
            "Returns timestamped messages from the current chat, oldest first."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "hours_back": {
                    "type": "integer",
                    "description": (
                        "Hours of history to fetch. "
                        "Omit for today (since midnight local time)."
                    ),
                    "minimum": 1,
                    "maximum": 48,
                },
            },
            "required": [],
        }

    async def execute(self, **kwargs: Any) -> str:
        if not self._channel or not self._chat_id:
            return "Error: no chat context set."

        hours_back = kwargs.get("hours_back")
        since = self._compute_since(hours_back)

        rows = self._archive.lookup_messages_in_range(
            self._channel, self._chat_id, since, limit=300
        )
        if not rows:
            return "No messages found in the requested time range."

        name_map = self._build_name_map(rows)
        lines: list[str] = []
        for row in rows:
            ts = self._format_timestamp(row)
            speaker = name_map.get(row["sender_id"] or "", row.get("sender_name") or "?")
            text = self._resolve_mentions(row["text"] or "", name_map)
            lines.append(f"[{ts} {speaker}] {text}")

        return "\n".join(lines)

    # ── helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _compute_since(hours_back: int | None) -> datetime:
        if hours_back is not None:
            return datetime.now(UTC) - timedelta(hours=int(hours_back))
        # Default: midnight today in local time, converted to UTC
        local_now = datetime.now(_LOCAL_TZ)
        local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        return local_midnight.astimezone(UTC)

    @staticmethod
    def _format_timestamp(row: dict[str, Any]) -> str:
        ts = row.get("timestamp")
        if isinstance(ts, int):
            dt = datetime.fromtimestamp(ts, tz=UTC).astimezone(_LOCAL_TZ)
            return dt.strftime("%H:%M")
        return "??:??"

    def _build_name_map(self, rows: list[dict[str, Any]]) -> dict[str, str]:
        """Build sender_id -> display_name map from rows + contacts."""
        name_map: dict[str, str] = {}
        for row in rows:
            sid = row.get("sender_id") or ""
            if sid and sid not in name_map:
                resolved = None
                if self._contacts is not None:
                    resolved = self._contacts.resolve_jid_to_name(sid)
                name_map[sid] = resolved or row.get("sender_name") or sid
        return name_map

    def _resolve_mentions(self, text: str, name_map: dict[str, str]) -> str:
        """Replace @<token> with @Name where possible."""
        def _replace(match: re.Match) -> str:
            token = match.group(1)
            # Direct lookup in name_map (sender_id might be bare token)
            if token in name_map:
                return f"@{name_map[token]}"
            # Try as phone JID
            if self._contacts is not None:
                phone_jid = f"{token}@s.whatsapp.net"
                name = self._contacts.resolve_jid_to_name(phone_jid)
                if name:
                    name_map[token] = name  # cache for next hit
                    return f"@{name}"
                # Try as LID
                lid_jid = f"{token}@lid"
                name = self._contacts.resolve_jid_to_name(lid_jid)
                if name:
                    name_map[token] = name
                    return f"@{name}"
            return match.group(0)  # leave as-is

        return _MENTION_RE.sub(_replace, text)
