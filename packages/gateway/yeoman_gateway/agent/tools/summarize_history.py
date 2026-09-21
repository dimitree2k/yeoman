"""Tool for fetching raw chat history for LLM summarization."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Callable

from yeoman_gateway.agent.tools.base import Tool

if TYPE_CHECKING:
    from yeoman_gateway.knowledge._contacts.service import ContactsService
    from yeoman_gateway.storage.inbound_archive import InboundArchive

_MENTION_RE = re.compile(r"@(\d{10,})")
_LOCAL_TZ = datetime.now(timezone.utc).astimezone().tzinfo


class SummarizeHistoryTool(Tool):
    """Fetch raw chat history for summarization."""

    def __init__(
        self,
        archive: "InboundArchive",
        contacts: "ContactsService | None" = None,
        *,
        knowledge: object | None = None,
        group_resolver: "Callable[[str], tuple[str | None, str | None]] | None" = None,
    ) -> None:
        self._archive = archive
        self._contacts = contacts
        #: Public knowledge facade: a sender name comes from a proven binding, never
        #: from the transitional contacts cache, whenever knowledge is wired.
        self._knowledge = knowledge
        self._group_resolver = group_resolver
        self._channel = ""
        self._chat_id = ""
        self._is_owner = False

    def set_context(
        self, channel: str, chat_id: str, *, is_owner: bool = False
    ) -> None:
        self._channel = channel
        self._chat_id = chat_id
        self._is_owner = is_owner

    @property
    def name(self) -> str:
        return "summarize_history"

    @property
    def description(self) -> str:
        if self._can_use_group_parameter:
            return (
                "Fetch raw chat message history for summarization. "
                "Use when users ask to summarize, recap, or catch up on recent conversation. "
                "Returns timestamped messages, oldest first. In an owner DM, an optional group "
                "parameter may fetch history from a different chat."
            )
        return (
            "Fetch raw chat message history for summarization. "
            "Use when users ask to summarize, recap, or catch up on recent conversation. "
            "Returns timestamped messages from the current chat, oldest first."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        properties: dict[str, Any] = {
            "hours_back": {
                "type": "integer",
                "description": (
                    "Hours of history to fetch. "
                    "Omit for today (since midnight local time)."
                ),
                "minimum": 1,
                "maximum": 48,
            },
        }
        if self._can_use_group_parameter:
            properties["group"] = {
                "type": "string",
                "description": (
                    "Optional owner DM only WhatsApp group alias/name/chat id "
                    "to fetch history from a different chat."
                ),
            }
        return {
            "type": "object",
            "properties": properties,
            "required": [],
        }

    @property
    def _can_use_group_parameter(self) -> bool:
        return self._channel == "whatsapp" and self._is_owner and not self._chat_id.endswith("@g.us")

    async def execute(self, **kwargs: Any) -> str:
        if not self._channel or not self._chat_id:
            return "Error: no chat context set."

        group_ref = str(kwargs.get("group") or "").strip()
        target_channel = self._channel
        target_chat_id = self._chat_id

        if group_ref:
            if not self._is_owner:
                return "Error: cross-chat access is owner-only."
            if self._chat_id.endswith("@g.us"):
                return "Error: cross-chat reads are only available from DMs."
            if self._group_resolver is None:
                return "Error: WhatsApp group resolver is not configured."
            resolved_chat_id, err = self._group_resolver(group_ref)
            if err is not None or not resolved_chat_id:
                return f"Error: {err or 'failed to resolve group'}"
            target_channel = "whatsapp"
            target_chat_id = resolved_chat_id

        hours_back = kwargs.get("hours_back")
        since = self._compute_since(hours_back)

        rows = self._archive.lookup_messages_in_range(
            target_channel, target_chat_id, since, limit=300
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
        """Build sender_id -> display_name map from rows + proven person bindings."""
        name_map: dict[str, str] = {}
        for row in rows:
            sid = row.get("sender_id") or ""
            if sid and sid not in name_map:
                name_map[sid] = self._resolve_name(sid) or row.get("sender_name") or sid
        return name_map

    def _resolve_name(self, identifier: str) -> str | None:
        """One released name for one identifier, or nothing.

        Knowledge is the authority whenever it is wired: a sender without a proven
        binding keeps the archive's own name.  The legacy cache answers only for the
        transitional callers that run without knowledge at all.  An outage resolves
        nothing and raises nothing - a summary is still worth printing with the names
        the archive already carries.
        """
        if self._knowledge is not None:
            try:
                person_id = self._knowledge.person_id_for_value(identifier)
                if person_id is None:
                    return None
                return self._knowledge.person_display_name(person_id)
            except Exception:
                return None
        if self._contacts is not None:
            return self._contacts.resolve_jid_to_name(identifier)
        return None

    def _resolve_mentions(self, text: str, name_map: dict[str, str]) -> str:
        """Replace @<token> with @Name where possible."""
        def _replace(match: re.Match) -> str:
            token = match.group(1)
            # Direct lookup in name_map (sender_id might be bare token)
            if token in name_map:
                return f"@{name_map[token]}"
            # Try as phone JID, then as LID.
            for candidate in (f"{token}@s.whatsapp.net", f"{token}@lid"):
                name = self._resolve_name(candidate)
                if name:
                    name_map[token] = name  # cache for next hit
                    return f"@{name}"
            return match.group(0)  # leave as-is

        return _MENTION_RE.sub(_replace, text)
