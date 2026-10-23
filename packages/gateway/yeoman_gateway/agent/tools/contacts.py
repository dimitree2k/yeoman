"""LLM tool for managing contacts."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from yeoman_gateway.knowledge.models import TrustedReadContext
    from yeoman_gateway.people.resolver import PeopleResolver, ReaderContext

from yeoman_gateway.agent.tools.base import Tool
from yeoman_gateway.history.live import HistoryPaused


class ContactsTool(Tool):
    """Person lookup and source-backed contact information."""

    def __init__(
        self, knowledge: object | None = None, *, people: PeopleResolver | None = None
    ) -> None:
        #: Public person-knowledge facade.  Identifier lookups go through it so this
        #: tool never reads the contacts cache directly.
        self._knowledge = knowledge
        self._people = people
        self._channel = ""
        self._chat_id = ""

    def set_context(self, channel: str, chat_id: str) -> None:
        self._channel = channel
        self._chat_id = chat_id

    @property
    def _channel(self) -> str:
        from yeoman_gateway.processing.tool_context import tool_target

        return tool_target(
            default_channel=self._default_channel, default_chat_id=self._default_chat_id
        )[0]

    @_channel.setter
    def _channel(self, value: str) -> None:
        self._default_channel = value

    @property
    def _chat_id(self) -> str:
        from yeoman_gateway.processing.tool_context import tool_target

        return tool_target(
            default_channel=self._default_channel, default_chat_id=self._default_chat_id
        )[1]

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
            "Actions: list, search, get, add_field, remove_field. "
            "For 'list my contacts', use action=list in the owner DM only; "
            "paginate with limit (default 25, max 50) and offset (default 0). "
            "`search` always needs `query` (a name, nickname or phone number); "
            "do not use repeated searches to list contacts."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "search", "get", "add_field", "remove_field"],
                    "description": "The operation to perform; list is owner DM only.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                    "default": 25,
                    "description": "Page size for action=list (1-50).",
                },
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "default": 0,
                    "description": "Number of contacts to skip for action=list; use Next offset.",
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
        from yeoman_gateway.people.disclosure import reader_context
        from yeoman_gateway.people.resolver import PeopleUnavailable
        from yeoman_gateway.processing.tool_context import (
            current_tool_context,
            tool_history_snapshot,
        )

        snapshot = tool_history_snapshot()
        context = current_tool_context()
        if self._people is None or context is None:
            return "Error: directory is unavailable"
        try:
            reader = reader_context(
                self._people,
                channel=context.channel,
                chat_id=context.chat_id,
                principal_id=context.canonical_user_id,
                is_owner=context.is_owner,
                at_ms=int(time.time() * 1000),
                history=snapshot,
            )
            action = kwargs.get("action", "")
            if action == "list":
                return self._list(
                    reader, limit=kwargs.get("limit", 25), offset=kwargs.get("offset", 0)
                )
            if action == "search":
                return self._search(kwargs.get("query", ""), reader=reader)
            if action == "get":
                return self._get(kwargs.get("name", ""), reader, context.knowledge_read_context)
            if action in ("add_field", "remove_field"):
                return self._mutate(reader, **kwargs)
            return f"Unknown action: {action}"
        except PeopleUnavailable:
            return "Error: directory is unavailable"

    def _list(self, reader: ReaderContext, *, limit: int = 25, offset: int = 0) -> str:
        from yeoman_gateway.people.disclosure import render_person

        if reader.kind != "owner_dm":
            return "Error: listing contacts is only available in the owner DM"
        if type(limit) is not int or not 1 <= limit <= 50 or type(offset) is not int or offset < 0:
            return "Error: list needs an integer limit (1-50) and integer offset (0 or greater)"
        # Owner scope already contains unique terminals at effective level contact or curated.
        cards = [
            render_person(person, reader) for person in reader.directory.scope(reader).eligible_ids
        ]
        cards.sort(key=lambda card: (card.display_name.casefold(), card.person_id))
        page = cards[offset : offset + limit]
        if not page:
            return "No contacts found."
        lines = [f"Contacts ({offset + 1}-{offset + len(page)} of {len(cards)}):"]
        for card in page:
            identifiers = ", ".join(f"{kind}:{value}" for kind, value in card.identifiers)
            lines.append(f"- {card.display_name} ({identifiers})")
        if offset + len(page) < len(cards):
            lines.append(f"Next offset: {offset + len(page)}")
        return "\n".join(lines)

    def _search(self, query: str, *, reader: ReaderContext | None = None) -> str:
        from yeoman_gateway.people.disclosure import render_person
        from yeoman_gateway.people.resolver import resolve_person

        if not query.strip():
            return (
                "Error: search needs a query (a name, nickname or phone number); "
                "use action=list to list contacts in the owner DM. Otherwise retry once with "
                "the person's name, or ask the user who they mean."
            )
        if reader is None:
            return "Error: directory is unavailable"
        found = resolve_person(query, reader)
        ids = (
            (found.person_id,)
            if found.person_id is not None
            else tuple(candidate.person_id for candidate in found.candidates)
        )
        if not ids:
            return f"No people found matching '{query}'"
        cards = [render_person(person, reader) for person in ids[:8]]
        cards.sort(key=lambda card: (card.display_name.casefold(), card.person_id))
        lines = []
        for card in cards:
            identifiers = ", ".join(f"{kind}:{value}" for kind, value in card.identifiers)
            groups = ", ".join(card.shared_chat_names)
            suffix = f"; groups: {groups}" if groups else ""
            lines.append(f"- {card.display_name} ({identifiers}{suffix})")
        if len(ids) > 8:
            lines.append("Candidate list truncated; use a more specific name.")
        return "Found people:\n" + "\n".join(lines)

    def _one_person(self, name: str, reader: ReaderContext) -> tuple[int | None, str]:
        from yeoman_gateway.people.resolver import resolve_person

        if not name:
            return None, "Error: name is required"
        found = resolve_person(name, reader)
        if found.status == "ambiguous":
            return None, f"Error: '{name}' matches {len(found.candidates)} people; be more specific"
        if found.person_id is None:
            return None, f"No person found with name '{name}'"
        return found.person_id, ""

    def _subjects(self, person: int, reader: ReaderContext) -> tuple[str, ...]:
        if self._knowledge is None:
            return ()
        return self._knowledge.directory_subject_ids(person, reader=reader)

    def _get(self, name: str, reader: ReaderContext, context: TrustedReadContext | None) -> str:
        from yeoman_gateway.people.disclosure import render_person

        person, error = self._one_person(name, reader)
        if error:
            return error
        assert person is not None
        card = render_person(person, reader)
        subjects = self._subjects(person, reader)
        lines = [f"Name: {card.display_name}"]
        if len(subjects) == 1:
            lines.append(f"Person: {subjects[0]}")
        if card.identifiers:
            lines.append(
                "Identifiers: " + ", ".join(f"{kind}={value}" for kind, value in card.identifiers)
            )
        if card.names:
            lines.append("Observed names: " + ", ".join(card.names))
        lines.extend(f"{key}: {value}" for key, value in card.owner_details)
        if not subjects or context is None:
            lines.append("Knowledge content unavailable.")
        else:
            seen = set()
            for subject in subjects:
                profile = self._knowledge.person_profile(subject, context=context)
                if profile.reason != "ok":
                    if "Knowledge content unavailable." not in seen:
                        lines.append("Knowledge content unavailable.")
                        seen.add("Knowledge content unavailable.")
                    continue
                for line in profile.card.splitlines():
                    if line.startswith(("name:", "aliases:", "contact:")) or line in seen:
                        continue
                    seen.add(line)
                    lines.append(line)
        return "\n".join(lines)

    def _mutate(self, reader: ReaderContext, *, action: str, **kwargs: Any) -> str:
        from yeoman_gateway.people.disclosure import render_person

        name, kind, value = (kwargs.get(key, "") for key in ("name", "kind", "value"))
        if not name or not kind or not value:
            return "Error: name, kind, and value are required"
        person, error = self._one_person(name, reader)
        if error:
            return error
        assert person is not None
        subjects = self._subjects(person, reader)
        if not subjects:
            return "Error: Knowledge content unavailable"
        if len(subjects) != 1:
            return "Error: ambiguous Knowledge subjects; mutation refused"
        display = render_person(person, reader).display_name
        subject = subjects[0]
        try:
            if action == "add_field":
                label = kwargs.get("label")
                receipt = self._knowledge.record_note(
                    subject,
                    content=value,
                    label=label or kind,
                    channel=reader.channel,
                    chat_id=reader.chat_id,
                )
                label_str = f" ({label})" if label else ""
                statement = receipt.changed_ids[0] if receipt.changed_ids else "n/a"
                return f"Added {kind}{label_str}: {value} to {display} [statement {statement}]"
            erased = self._knowledge.erase_matching_statements(
                subject, contains=value, reason="contacts_tool_remove_field"
            )
            return f"Removed {kind}: {value} from {display} ({erased} statements)"
        except HistoryPaused:
            raise
        except Exception as exc:  # domain errors are reported, never swallowed
            return f"Error: {exc}"
