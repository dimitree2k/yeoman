"""Tool for searching session conversation history on demand."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from yeoman_gateway.agent.tools.base import Tool

if TYPE_CHECKING:
    from yeoman_gateway.session.manager import SessionManager


class RecallConversationTool(Tool):
    """Search conversation history for messages matching a query."""

    def __init__(self, session_manager: "SessionManager") -> None:
        self._session_manager = session_manager
        self._channel = ""
        self._chat_id = ""

    def set_context(self, channel: str, chat_id: str) -> None:
        self._channel = channel
        self._chat_id = chat_id

    @property
    def name(self) -> str:
        return "recall_conversation"

    @property
    def description(self) -> str:
        return (
            "Search earlier conversation history for messages matching a query. "
            "Use when the user references something from a previous part of the "
            "conversation that is not in your visible context. "
            "Searches across session boundaries (including before /new)."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search term to match against message content.",
                    "minLength": 1,
                },
                "max_messages": {
                    "type": "integer",
                    "description": "Maximum number of matching messages to return.",
                    "minimum": 1,
                    "maximum": 50,
                },
            },
            "required": ["query"],
        }

    async def execute(self, **kwargs: Any) -> str:
        if not self._channel or not self._chat_id:
            return "Error: no chat context set."

        query = str(kwargs.get("query") or "").strip().lower()
        if not query:
            return "Error: query cannot be empty."

        max_messages = int(kwargs.get("max_messages") or 30)
        max_messages = max(1, min(50, max_messages))

        session_key = f"{self._channel}:{self._chat_id}"
        session = self._session_manager.get_or_create(session_key)

        # Search ALL messages (ignoring boundaries) for query matches.
        matches: list[str] = []
        for msg in session.messages:
            role = str(msg.get("role") or "")
            if role not in ("user", "assistant"):
                continue
            content = str(msg.get("content") or "")
            if query in content.lower():
                ts = msg.get("timestamp", "?")
                matches.append(f"[{ts}] [{role}]: {content[:300]}")
                if len(matches) >= max_messages:
                    break

        if not matches:
            return f"No matching messages found for '{query}'."

        header = f"Found {len(matches)} matching message(s):\n"
        return header + "\n".join(matches)
