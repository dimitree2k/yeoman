"""LLM tool for managing contacts."""

from __future__ import annotations

from typing import Any

from yeoman_gateway.agent.tools.base import Tool
from yeoman_gateway.history.live import HistoryPaused


class ContactsTool(Tool):
    """Person lookup and source-backed contact information."""

    def __init__(self, knowledge: object | None = None) -> None:
        #: Public person-knowledge facade.  Identifier lookups go through it so this
        #: tool never reads the contacts cache directly.
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
        return (
            "Search people, read profiles, and add or remove contact information. "
            "Actions: search, get, add_field, remove_field. "
            "`search` always needs `query` (a name, nickname or phone number); "
            "listing all contacts is not supported."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["search", "get", "add_field", "remove_field"],
                    "description": "The operation to perform.",
                },
                "name": {
                    "type": "string",
                    "description": "Contact display name to search or read.",
                },
                "query": {
                    "type": "string",
                    "description": "Required for action=search: a name, nickname or phone number.",
                },
                "kind": {
                    "type": "string",
                    "description": "Field kind: email, url, note, company, etc.",
                },
                "value": {
                    "type": "string",
                    "description": "Field value.",
                },
                "label": {
                    "type": "string",
                    "description": "Optional field label: work, personal, linkedin, etc.",
                },
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
            case "add_field":
                return self._add_field(
                    kwargs.get("name", ""),
                    kwargs.get("kind", ""),
                    kwargs.get("value", ""),
                    kwargs.get("label"),
                )
            case "remove_field":
                return self._remove_field(
                    kwargs.get("name", ""),
                    kwargs.get("kind", ""),
                    kwargs.get("value", ""),
                )
            case _:
                return f"Unknown action: {action}"

    def _search(self, query: str) -> str:
        if not query:
            return (
                "Error: search needs a query (a name, nickname or phone number); "
                "listing all contacts is not supported. Retry once with the person's name, "
                "or ask the user who they mean."
            )
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


    def _add_field(self, name: str, kind: str, value: str, label: str | None) -> str:
        if not name or not kind or not value:
            return "Error: name, kind, and value are required"
        if self._knowledge is None:
            return "Error: knowledge is unavailable"
        person, error = self._one_person(name)
        if error:
            return error
        assert person is not None
        try:
            receipt = self._knowledge.record_note(
                person.person_id,
                content=value,
                label=label or kind,
                channel=self._channel or "whatsapp",
                chat_id=self._chat_id or "cli",
            )
        except HistoryPaused:
            raise
        except Exception as exc:  # domain errors are reported, never swallowed
            return f"Error: {exc}"
        label_str = f" ({label})" if label else ""
        return (
            f"Added {kind}{label_str}: {value} to {person.display_name or person.person_id}"
            f" [statement {receipt.changed_ids[0] if receipt.changed_ids else 'n/a'}]"
        )

    def _remove_field(self, name: str, kind: str, value: str) -> str:
        if not name or not kind or not value:
            return "Error: name, kind, and value are required"
        if self._knowledge is None:
            return "Error: knowledge is unavailable"
        person, error = self._one_person(name)
        if error:
            return error
        assert person is not None
        # ID-based, bounded maintenance: the old broad LIKE erase is gone from this path,
        # because "delete every statement containing X" is not a correction of a fact.
        erased = self._knowledge.erase_matching_statements(
            person.person_id, contains=value, reason="contacts_tool_remove_field"
        )
        return (
            f"Removed {kind}: {value} from {person.display_name or person.person_id}"
            f" ({erased} statements)"
        )

    def _one_person(self, name: str):
        """Resolve a name to exactly one person, or an explicit error string."""
        if self._knowledge is None:
            return None, "Error: knowledge is unavailable"
        found = self._knowledge.search_people_with_policy(
            name, channel=self._channel or "whatsapp", chat_id=self._chat_id or "cli"
        )
        if not found:
            return None, f"Error: no person found with name '{name}'"
        if len(found) > 1:
            return None, (
                f"Error: '{name}' matches {len(found)} people; use an exact person id"
            )
        return found[0], ""

