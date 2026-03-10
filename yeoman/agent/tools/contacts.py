"""LLM tool for managing contacts."""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from yeoman.agent.tools.base import Tool

if TYPE_CHECKING:
    from yeoman.contacts.service import ContactsService


class ContactsTool(Tool):
    """CRUD operations on the contacts CRM."""

    def __init__(self, contacts: "ContactsService") -> None:
        self._contacts = contacts
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
        results = self._contacts.store.search_by_display_name(query)
        if not results:
            results = self._contacts.store.search_by_alias(query)
        if not results:
            return f"No contacts found matching '{query}'"
        lines = []
        for c in results:
            idents = self._contacts.store.get_identifiers(c.id)
            ident_str = ", ".join(f"{i.channel}:{i.identifier}" for i in idents)
            lines.append(f"- {c.display_name} ({ident_str})")
        return "Found contacts:\n" + "\n".join(lines)

    def _get(self, name: str) -> str:
        if not name:
            return "Error: name is required"
        contacts = self._contacts.store.search_by_display_name(name)
        if not contacts:
            return f"No contact found with name '{name}'"
        c = contacts[0]
        idents = self._contacts.store.get_identifiers(c.id)
        aliases = self._contacts.store.get_aliases(c.id)
        fields = self._contacts.store.get_fields(c.id)
        lines = [
            f"Name: {c.display_name}",
            f"Phone: {c.phone_number or 'N/A'}",
            f"Owner: {'Yes' if c.is_owner else 'No'}",
        ]
        if idents:
            lines.append("Identifiers: " + ", ".join(f"{i.kind}={i.identifier}" for i in idents))
        if aliases:
            lines.append("Aliases: " + ", ".join(a.alias for a in aliases))
        if fields:
            for f in fields:
                label = f" ({f.label})" if f.label else ""
                lines.append(f"{f.kind}{label}: {f.value}")
        return "\n".join(lines)

    def _update_name(self, identifier: str, name: str) -> str:
        if not name:
            return "Error: name is required"
        contact_id: str | None = None
        if identifier:
            contact_id = self._contacts.known_jids.get(identifier)
        if not contact_id:
            return f"Error: no contact found for identifier '{identifier}'"
        self._contacts.update_display_name(contact_id, name)
        return f"Updated display name to '{name}'"

    def _add_field(self, name: str, kind: str, value: str, label: str | None) -> str:
        if not name or not kind or not value:
            return "Error: name, kind, and value are required"
        contacts = self._contacts.store.search_by_display_name(name)
        if not contacts:
            return f"Error: no contact found with name '{name}'"
        c = contacts[0]
        self._contacts.store.add_field(
            contact_id=c.id, kind=kind, value=value, label=label,
        )
        label_str = f" ({label})" if label else ""
        return f"Added {kind}{label_str}: {value} to {c.display_name}"

    def _remove_field(self, name: str, kind: str, value: str) -> str:
        if not name or not kind or not value:
            return "Error: name, kind, and value are required"
        contacts = self._contacts.store.search_by_display_name(name)
        if not contacts:
            return f"Error: no contact found with name '{name}'"
        self._contacts.store.delete_field(contacts[0].id, kind, value)
        return f"Removed {kind}: {value} from {contacts[0].display_name}"

    def _merge(self, target_name: str, source_name: str) -> str:
        if not target_name or not source_name:
            return "Error: target_name and source_name are required"
        targets = self._contacts.store.search_by_display_name(target_name)
        sources = self._contacts.store.search_by_display_name(source_name)
        if not targets:
            return f"Error: no contact found with name '{target_name}'"
        if not sources:
            return f"Error: no contact found with name '{source_name}'"
        if targets[0].id == sources[0].id:
            return "Error: target and source are the same contact"
        self._contacts.store.merge_contacts(
            target_id=targets[0].id, source_id=sources[0].id,
        )
        self._contacts.reload_cache()
        return f"Merged '{source_name}' into '{target_name}'"
