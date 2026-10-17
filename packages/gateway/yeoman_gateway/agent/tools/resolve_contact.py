"""Directory recognition followed by active endpoint and delivery authorization."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from loguru import logger

from yeoman_gateway.agent.tools.base import Tool
from yeoman_gateway.history.live import HistoryPaused

if TYPE_CHECKING:
    from yeoman_gateway.people.resolver import PeopleResolver, ReaderContext


@dataclass(frozen=True, slots=True)
class ContactResolution:
    """Minimal target resolution result safe to expose to the model."""

    display_name: str
    jid: str
    matched_identifier: str | None = None


def _pick_delivery_value(identifiers: list[str]) -> str | None:
    """One address, or nothing.

    Several equal addresses are ambiguous (spec 7.2), so a person with two proven phone
    JIDs is not resolved to the first of them.  The phone JID wins only when exactly one
    exists next to other kinds.
    """
    unique = list(dict.fromkeys(identifiers))
    if len(unique) == 1:
        return unique[0]
    phones = [item for item in unique if item.endswith("@s.whatsapp.net")]
    if len(phones) == 1:
        return phones[0]
    return None


def resolve_contact_reference(
    *,
    reference: str,
    channel: str,
    chat_id: str,
    knowledge: object | None = None,
    people: PeopleResolver | None = None,
    reader: ReaderContext | None = None,
) -> ContactResolution | None:
    """Recognize once, then require a proven, current and disclosable endpoint.

    Conversation-requested delivery retains its existing binding gate. Address release
    for out-of-context A2A delivery remains a separate permission.
    """
    from yeoman_gateway.people.bridges import history_person
    from yeoman_gateway.people.disclosure import render_person
    from yeoman_gateway.people.resolver import _identifiers, resolve_person

    if people is None or reader is None or reader.directory is not people:
        return None
    if (channel, chat_id) != (reader.channel, reader.chat_id) or not reference.strip():
        return None
    try:
        if reader.history is not None:
            reader.history.assert_current(reader.history.generation)
        found = resolve_person(reference, reader)
        if found.person_id is None:
            return None
        person = found.person_id
        if reader.kind == "group" and person not in people.scope(reader).member_ids:
            return None
        card = render_person(person, reader)
        values = set()
        if channel == "whatsapp" and reader.history is not None:
            bridge = history_person(person, reader)
            for ident in bridge.identifiers:
                if (ident.valid_from_ms is None or ident.valid_from_ms <= reader.at_ms) and (
                    ident.valid_until_ms is None or reader.at_ms < ident.valid_until_ms
                ):
                    values.add(ident.value)
        elif knowledge is not None:
            for subject in knowledge.directory_subject_ids(person, reader=reader):
                for ident in knowledge.person_identifiers(subject):
                    if ident.channel != channel:
                        continue
                    owners = knowledge.owners_of_identifier_value(ident.value, channel=channel)
                    if tuple(owners) != (subject,):
                        continue
                    query = ident.value.split("|", 1)[0] if channel == "telegram" else ident.value
                    if resolve_person(query, reader).person_id == person:
                        values.add(ident.value)
        chosen = _pick_delivery_value(sorted(values))
        if chosen is None:
            return None
        # Rendering controls whether an endpoint may enter the model's context.
        allowed = set(card.identifiers)
        if chosen.endswith("@s.whatsapp.net"):
            if ("phone", chosen.removesuffix("@s.whatsapp.net")) not in allowed:
                return None
        elif chosen.endswith("@lid"):
            if ("wa_lid", chosen.removesuffix("@lid")) not in allowed:
                return None
        elif channel == "telegram":
            digits = chosen.removeprefix("telegram:").split("|", 1)[0]
            if ("telegram_id", digits) not in allowed:
                return None
        else:
            return None
        matched = None
        if channel == "whatsapp" and reference.strip().startswith("@"):
            from yeoman_gateway.history.queries import HistoryQueries

            _, parsed = _identifiers(reference.strip(), reader)
            for kind, value in parsed:
                if kind not in ("phone", "wa_lid"):
                    continue
                candidate = value + ("@lid" if kind == "wa_lid" else "@s.whatsapp.net")
                if resolve_person(candidate, reader).person_id != person:
                    continue
                if (
                    reader.kind == "group"
                    and HistoryQueries(reader.history).mention(
                        candidate, chat_id=chat_id, at_ms=reader.at_ms
                    )
                    != chosen
                ):
                    continue
                if candidate in values:
                    matched = candidate if candidate != chosen else None
                    break
            else:
                return None
        if reader.history is not None:
            reader.history.assert_current(reader.history.generation)
        return ContactResolution(card.display_name, chosen, matched)
    except HistoryPaused:
        raise
    except Exception as exc:
        # No authority, no target; the failure stays visible instead of looking like
        # "no proven person".
        logger.warning(
            "contact resolution degraded: directory lookup failed ({})",
            getattr(exc, "code", type(exc).__name__),
        )
        return None


def contact_resolution_matches_reference(
    *,
    reference: str,
    resolution: ContactResolution,
    knowledge: object | None = None,
    people: PeopleResolver | None = None,
    reader: ReaderContext | None = None,
) -> bool:
    """Confirm the same unique directory identity and currently proven endpoint."""
    if reader is None:
        return False
    expected = resolve_contact_reference(
        reference=reference,
        channel=reader.channel,
        chat_id=reader.chat_id,
        knowledge=knowledge,
        people=people,
        reader=reader,
    )
    return expected is not None and expected.jid == resolution.jid


class ResolveContactTool(Tool):
    """Resolve a contact name or WhatsApp mention to one delivery JID."""

    def __init__(
        self, *, knowledge: object | None = None, people: PeopleResolver | None = None
    ) -> None:
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
        return "resolve_contact"

    @property
    def description(self) -> str:
        return (
            "Read-only lookup for resolving a named person or WhatsApp @mention "
            "to a single delivery JID in the current chat. Does not expose notes "
            "or modify contacts."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Contact name, alias, phone JID, or WhatsApp @mention to resolve.",
                }
            },
            "required": ["query"],
        }

    async def execute(self, query: str, **kwargs: Any) -> str:
        from yeoman_gateway.people.disclosure import reader_context
        from yeoman_gateway.people.resolver import PeopleUnavailable
        from yeoman_gateway.processing.tool_context import (
            current_tool_context,
            tool_history_snapshot,
        )

        snapshot = tool_history_snapshot()
        context = current_tool_context()
        del kwargs
        reader = None
        if self._people is not None and context is not None:
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
            except PeopleUnavailable:
                pass
        result = resolve_contact_reference(
            knowledge=self._knowledge,
            people=self._people,
            reader=reader,
            reference=query,
            channel=self._channel,
            chat_id=self._chat_id,
        )
        if result is None:
            return json.dumps(
                {
                    "ok": False,
                    "error_code": "contact_not_resolved",
                    "query": query,
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "ok": True,
                "contact": {
                    "display_name": result.display_name,
                    "jid": result.jid,
                    "matched_identifier": result.matched_identifier,
                },
            },
            ensure_ascii=False,
        )
