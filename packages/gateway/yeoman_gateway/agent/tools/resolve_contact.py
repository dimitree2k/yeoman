"""Read-only contact resolution for delivery targets."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from yeoman_gateway.agent.tools.base import Tool

if TYPE_CHECKING:
    from yeoman_gateway.contacts.models import Contact
    from yeoman_gateway.contacts.service import ContactsService
    from yeoman_gateway.storage.chat_registry import ChatRegistry

_MENTION_TOKEN_RE = re.compile(r"@?([0-9]{5,})(?:@(lid|s\.whatsapp\.net))?")


@dataclass(frozen=True, slots=True)
class ContactResolution:
    """Minimal target resolution result safe to expose to the model."""

    display_name: str
    jid: str
    matched_identifier: str | None = None


def _normalise_whatsapp_identifier(value: str) -> str:
    token = str(value or "").strip()
    if not token:
        return ""
    if token.startswith("@"):
        token = token[1:]
    if "@" in token:
        return token
    if token.isdigit():
        return f"{token}@s.whatsapp.net"
    return token


def _mention_candidates(text: str) -> list[str]:
    candidates: list[str] = []
    for match in _MENTION_TOKEN_RE.finditer(str(text or "")):
        digits = match.group(1)
        suffix = match.group(2)
        if suffix == "lid":
            candidates.append(f"{digits}@lid")
        elif suffix == "s.whatsapp.net":
            candidates.append(f"{digits}@s.whatsapp.net")
        else:
            candidates.append(f"{digits}@lid")
            candidates.append(f"{digits}@s.whatsapp.net")
    return candidates


def _participant_phone_map(
    chat_registry: "ChatRegistry | None",
    *,
    channel: str,
    chat_id: str,
) -> dict[str, str]:
    if chat_registry is None or channel != "whatsapp" or not chat_id:
        return {}
    try:
        chat = chat_registry.get_chat(channel, chat_id)
    except Exception:
        return {}
    if not chat:
        return {}
    metadata = chat.get("metadata")
    if not isinstance(metadata, dict):
        return {}
    raw_participants = metadata.get("participants")
    if not isinstance(raw_participants, list):
        return {}
    mapped: dict[str, str] = {}
    for item in raw_participants:
        if not isinstance(item, dict):
            continue
        lid = str(item.get("id") or "").strip()
        phone = str(item.get("phoneNumber") or "").strip()
        if lid and phone:
            mapped[lid] = phone
    return mapped


def _display_for_identifier(
    contacts: "ContactsService",
    identifier: str,
) -> str | None:
    contact_id = contacts.known_jids.get(identifier)
    if not contact_id:
        return None
    return contacts.get_display_name(contact_id)


def _contact_identifiers(
    contacts: "ContactsService",
    contact: "Contact",
    *,
    channel: str,
) -> list[str]:
    return [
        ident.identifier
        for ident in contacts.store.get_identifiers(contact.id)
        if ident.channel == channel
    ]


def resolve_contact_reference(
    *,
    contacts: "ContactsService",
    reference: str,
    channel: str,
    chat_id: str,
    chat_registry: "ChatRegistry | None" = None,
) -> ContactResolution | None:
    """Resolve a human name, phone JID, or WhatsApp LID mention to one contact."""
    ref = str(reference or "").strip()
    if not ref:
        return None

    participant_map = _participant_phone_map(chat_registry, channel=channel, chat_id=chat_id)
    participant_ids = set(participant_map.keys()) | set(participant_map.values())

    mention_candidates = _mention_candidates(ref)
    if mention_candidates:
        for candidate in mention_candidates:
            mapped = participant_map.get(candidate, candidate)
            for identifier in (mapped, candidate):
                display = _display_for_identifier(contacts, identifier)
                if display:
                    return ContactResolution(
                        display_name=display,
                        jid=mapped,
                        matched_identifier=candidate if candidate != mapped else None,
                    )
        return None

    matches: dict[str, "Contact"] = {}
    for contact in contacts.store.search_by_display_name(ref):
        matches[contact.id] = contact
    for contact in contacts.store.search_by_alias(ref):
        matches[contact.id] = contact

    candidates: list[ContactResolution] = []
    for contact in matches.values():
        identifiers = _contact_identifiers(contacts, contact, channel=channel)
        if not identifiers:
            continue
        if participant_ids:
            identifiers = [
                ident for ident in identifiers
                if ident in participant_ids or ident in participant_map.values()
            ]
            if not identifiers:
                continue
        phone_jid = next((ident for ident in identifiers if ident.endswith("@s.whatsapp.net")), None)
        jid = phone_jid or identifiers[0]
        candidates.append(ContactResolution(display_name=contact.display_name, jid=jid))

    if len(candidates) == 1:
        return candidates[0]
    return None


class ResolveContactTool(Tool):
    """Resolve a contact name or WhatsApp mention to one delivery JID."""

    def __init__(
        self,
        *,
        contacts: "ContactsService",
        chat_registry: "ChatRegistry | None" = None,
    ) -> None:
        self._contacts = contacts
        self._chat_registry = chat_registry
        self._channel = ""
        self._chat_id = ""

    def set_context(self, channel: str, chat_id: str) -> None:
        self._channel = channel
        self._chat_id = chat_id

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
        del kwargs
        result = resolve_contact_reference(
            contacts=self._contacts,
            reference=query,
            channel=self._channel,
            chat_id=self._chat_id,
            chat_registry=self._chat_registry,
        )
        if result is None:
            return f"No single contact found matching '{query}'"
        extra = (
            f" (matched {result.matched_identifier})"
            if result.matched_identifier
            else ""
        )
        return f"Resolved contact: {result.display_name} -> whatsapp:{result.jid}{extra}"
