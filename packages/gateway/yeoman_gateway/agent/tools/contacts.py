"""LLM tool for managing contacts."""

from __future__ import annotations

from typing import Any

from yeoman_gateway.agent.tools.base import Tool


class ContactsTool(Tool):
    """CRUD operations on the contacts CRM."""

    def __init__(
        self,
        knowledge: object | None = None,
    ) -> None:
        self._knowledge = knowledge
        self._channel = ""
        self._chat_id = ""

    def set_context(self, channel: str, chat_id: str) -> None:
        self._channel = channel
        self._chat_id = chat_id

    @property
    def _channel(self) -> str:
        from yeoman_gateway.processing.tool_context import tool_target
        return tool_target(default_channel=self._default_channel, default_chat_id=self._default_chat_id)[0]

    @_channel.setter
    def _channel(self, value: str) -> None:
        self._default_channel = value

    @property
    def _chat_id(self) -> str:
        from yeoman_gateway.processing.tool_context import tool_target
        return tool_target(default_channel=self._default_channel, default_chat_id=self._default_chat_id)[1]

    @_chat_id.setter
    def _chat_id(self, value: str) -> None:
        self._default_chat_id = value

    @property
    def name(self) -> str:
        return "contacts"

    @property
    def description(self) -> str:
        return "Look up people and their approved contact details. Actions: search, get."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["search", "get"],
                    "description": "The read-only operation to perform.",
                },
                "name": {"type": "string", "description": "Contact display name to get."},
                "query": {"type": "string", "description": "Search query (name or alias)."},
            },
            "required": ["action"],
        }

    async def execute(self, **kwargs: Any) -> str:
        from yeoman_gateway.processing.tool_context import tool_history_snapshot
        snapshot = tool_history_snapshot()
        if snapshot is not None and self._knowledge is None:
            from yeoman_gateway.history.live import HistoryPaused
            raise HistoryPaused('history_knowledge_required')
        action = kwargs.get("action", "")
        match action:
            case "search":
                return self._search(kwargs.get("query", ""))
            case "get":
                return self._get(kwargs.get("name", ""))
            case _:
                return f"Unknown action: {action}"

    def _search(self, query: str) -> str:
        if not query:
            return "Error: query is required for search"
        if self._knowledge is None:
            return "Error: knowledge is unavailable"
        found = self._knowledge.search_people_with_policy(
            query, channel=self._channel or "whatsapp", chat_id=self._chat_id or "cli"
        )
        if not found:
            return f"No people found matching '{query}'"
        lines = []
        for person in found:
            identifiers = self._knowledge.person_identifiers(person.person_id)
            ident_str = ", ".join(f"{item.channel}:{item.value}" for item in identifiers)
            lines.append(f"- {person.display_name or person.person_id} ({ident_str})")
        return "Found people:\n" + "\n".join(lines)

    def _get(self, name: str) -> str:
        if not name:
            return "Error: name is required"
        if self._knowledge is None:
            return "Error: knowledge is unavailable"
        found = self._knowledge.search_people_with_policy(
            name, channel=self._channel or "whatsapp", chat_id=self._chat_id or "cli"
        )
        if not found:
            return f"No person found with name '{name}'"
        if len(found) > 1:
            # Two people with one name must never resolve to "the first one".
            return (
                f"Error: '{name}' matches {len(found)} people; use an exact person id"
            )
        person = found[0]
        # Every content read uses the one shared read contract through an owner-issued
        # context; a tool argument never becomes a read authorization.
        context = self._knowledge.owner_read_context(
            channel=self._channel or "whatsapp", chat_id=self._chat_id or "cli"
        )
        identifiers = self._knowledge.person_identifiers(person.person_id)
        aliases = self._knowledge.alias_names(person.person_id)
        profile = self._knowledge.person_profile(person.person_id, context=context)
        lines = [
            f"Name: {person.display_name or person.person_id}",
            f"Person: {person.person_id}",
        ]
        if identifiers:
            lines.append(
                "Identifiers: " + ", ".join(f"{item.kind}={item.value}" for item in identifiers)
            )
        if aliases:
            lines.append("Observed names: " + ", ".join(aliases))
        for line in profile.card.splitlines():
            if line.startswith(("name:", "aliases:", "contact:")):
                continue
            lines.append(line)
        return "\n".join(lines)

