"""LLM tool for managing contacts."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from yeoman_gateway.agent.tools.base import Tool

if TYPE_CHECKING:
    from yeoman_gateway.knowledge._contacts.service import ContactsService


class ContactsTool(Tool):
    """CRUD operations on the contacts CRM."""

    def __init__(
        self, contacts: "ContactsService", knowledge: object | None = None
    ) -> None:
        self._contacts = contacts
        #: Public person-knowledge facade.  Identifier lookups go through it so this
        #: tool never reads the contacts cache directly.
        self._knowledge = knowledge
        self._channel = ""
        self._chat_id = ""

    def set_context(self, channel: str, chat_id: str) -> None:
        self._channel = channel
        self._chat_id = chat_id

    @property
    def name(self) -> str:
        return "contacts"

    @property
    def description(self) -> str:
        return (
            "Manage the contacts CRM. Actions: search, get, update_name, "
            "add_field, remove_field, merge. Use this to look up or update "
            "information about people."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["search", "get", "update_name", "add_field", "remove_field", "merge"],
                    "description": "The operation to perform.",
                },
                "name": {
                    "type": "string",
                    "description": "Contact display name to search/get/update.",
                },
                "query": {
                    "type": "string",
                    "description": "Search query (name or alias).",
                },
                "identifier": {
                    "type": "string",
                    "description": "JID or platform identifier (for update_name on stubs).",
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
                "target_name": {
                    "type": "string",
                    "description": "Target contact name (for merge).",
                },
                "source_name": {
                    "type": "string",
                    "description": "Source contact name to merge into target.",
                },
            },
            "required": ["action"],
        }

    async def execute(self, **kwargs: Any) -> str:
        action = kwargs.get("action", "")
        match action:
            case "search":
                return self._search(kwargs.get("query", ""))
            case "get":
                return self._get(kwargs.get("name", ""))
            case "update_name":
                return self._update_name(
                    kwargs.get("identifier", ""),
                    kwargs.get("name", ""),
                )
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
            case "merge":
                return self._merge(
                    kwargs.get("target_name", ""),
                    kwargs.get("source_name", ""),
                )
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

    def _update_name(self, identifier: str, name: str) -> str:
        if not name:
            return "Error: name is required"
        if not identifier:
            return "Error: identifier is required"
        if self._knowledge is None:
            return "Error: knowledge is unavailable"
        # Identifier -> person, never identifier -> name: a tool argument must not be
        # able to name a person it cannot prove an identifier for.
        person_id = self._knowledge.person_id_for_value(identifier)
        if person_id is None:
            for channel in ("whatsapp", "telegram", "signal"):
                person_id = self._knowledge.person_id_for_value(f"{channel}:{identifier}")
                if person_id is not None:
                    break
        if person_id is None:
            return f"Error: no person found for identifier '{identifier}'"
        # A name change is an admin action: Policy decides the authority, not this tool.
        self._knowledge.set_preferred_name_with_policy(
            person_id, name, reason="contacts_tool"
        )
        return f"Updated preferred name to '{name}'"

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

    def _merge(self, target_name: str, source_name: str) -> str:
        if not target_name or not source_name:
            return "Error: target_name and source_name are required"
        if self._knowledge is None:
            return "Error: knowledge is unavailable"
        targets = self._knowledge.search_people_with_policy(
            target_name, channel=self._channel or "whatsapp", chat_id=self._chat_id or "cli"
        )
        sources = self._knowledge.search_people_with_policy(
            source_name, channel=self._channel or "whatsapp", chat_id=self._chat_id or "cli"
        )
        if not targets:
            return f"Error: no person found with name '{target_name}'"
        if not sources:
            return f"Error: no person found with name '{source_name}'"
        if len(targets) > 1 or len(sources) > 1:
            # Two matching names must never trigger an automatic merge.
            return (
                "Error: the name is ambiguous; use an exact person id"
                f" (targets={len(targets)}, sources={len(sources)})"
            )
        target_id, source_id = targets[0].person_id, sources[0].person_id
        if target_id == source_id:
            return "Error: target and source are the same person"
        receipt = self._knowledge.merge_people_with_policy(
            target_id, source_id, reason="contacts_tool_merge"
        )
        return (
            f"Merged '{source_name}' into '{target_name}' (operation {receipt.operation_id})"
        )
