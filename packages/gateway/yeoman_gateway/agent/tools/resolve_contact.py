"""Read-only contact resolution for delivery targets.

Person resolution has one authority at a time.  With a knowledge facade wired, a target
exists only for a person with an *active proven* binding: an unproven legacy identifier
never names a delivery target, and two candidates stay "unresolved" instead of becoming a
first match.  The legacy contacts cache answers only for callers that run without
knowledge at all - it is never a second opinion after knowledge already answered.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from loguru import logger

from yeoman_gateway.agent.tools.base import Tool

if TYPE_CHECKING:
    from yeoman_gateway.knowledge._contacts.models import Contact
    from yeoman_gateway.knowledge._contacts.service import ContactsService
    from yeoman_gateway.storage.chat_registry import ChatRegistry

_MENTION_TOKEN_RE = re.compile(
    r"(?<![\w@\x80-\U0010ffff])"
    r"@?([0-9]{5,})(?:@(lid|s\.whatsapp\.net))?"
    r"(?![\w@.\x80-\U0010ffff])"
)


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


def chat_participant_identifiers(
    chat_registry: "ChatRegistry | None",
    *,
    channel: str,
    chat_id: str,
) -> set[str]:
    """Return the proven LID and phone JIDs recorded for a chat."""
    mapped = _participant_phone_map(chat_registry, channel=channel, chat_id=chat_id)
    return set(mapped) | set(mapped.values())


def _reference_matches_labels(
    reference: str,
    labels: list[str],
) -> bool:
    reference_tokens = re.findall(r"\w+", reference.casefold())
    if not reference_tokens:
        return False
    width = len(reference_tokens)
    for label in labels:
        label_tokens = re.findall(r"\w+", str(label).casefold())
        if any(
            label_tokens[index:index + width] == reference_tokens
            for index in range(0, len(label_tokens) - width + 1)
        ):
            return True
    return False


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


def _proven_mention_target(
    knowledge: Any, *, channel: str, value: str
) -> tuple[str, str] | None:
    """The one person a mention value is *proven* for on this channel, if any.

    A mention is text until a proven binding makes it an identifier.  The value must
    belong to exactly one person - two owners are a conflict - and it must be one of that
    person's own proven identifiers *on this channel*, so a mention can never synthesise
    an address the person does not have.
    """
    owners = knowledge.owners_of_identifier_value(value, channel=channel)
    if len(owners) != 1:
        return None
    person_id = str(owners[0])
    proven = {
        item.value
        for item in knowledge.person_identifiers(person_id)
        if item.channel == channel
    }
    if value not in proven:
        return None
    return person_id, value


def resolve_contact_reference(
    *,
    reference: str,
    channel: str,
    chat_id: str,
    chat_registry: "ChatRegistry | None" = None,
    contacts: "ContactsService | None" = None,
    knowledge: object | None = None,
) -> ContactResolution | None:
    """Resolve a human name, phone JID, or WhatsApp LID mention to one delivery target.

    The current chat's explicit request is the addressing decision; the *proven binding*
    is the technical question.  So this path requires an active verified identifier and
    refuses ambiguity, but it does not require a separately released address alias -
    that gate governs addressing that no conversation asked for (out-of-context service
    delivery, see ``delivery_identifiers_for_alias``).

    A knowledge outage resolves nothing and raises nothing: a missing person is a
    degradation of this turn, not a failed turn.
    """
    ref = str(reference or "").strip()
    if not ref:
        return None

    participant_map = _participant_phone_map(chat_registry, channel=channel, chat_id=chat_id)
    participant_ids = set(participant_map.keys()) | set(participant_map.values())

    if knowledge is not None:
        try:
            return _resolve_through_knowledge(
                knowledge,
                ref,
                channel=channel,
                chat_id=chat_id,
                participant_map=participant_map,
                participant_ids=participant_ids,
            )
        except Exception as exc:
            # Knowledge is optional for the turn: no authority, no target.  The failure
            # stays visible instead of looking exactly like "no proven person".
            logger.warning(
                "contact resolution degraded: knowledge lookup failed ({})",
                getattr(exc, "code", type(exc).__name__),
            )
            return None
    if contacts is None:
        return None
    return _resolve_through_legacy(
        contacts,
        ref,
        channel=channel,
        participant_map=participant_map,
        participant_ids=participant_ids,
    )


def _resolve_through_knowledge(
    knowledge: Any,
    ref: str,
    *,
    channel: str,
    chat_id: str,
    participant_map: dict[str, str],
    participant_ids: set[str],
) -> ContactResolution | None:
    """Resolve a reference through the public knowledge facade only."""
    mention_candidates = _mention_candidates(ref)
    if mention_candidates:
        # A mention is an identifier, not a name: only a proven binding on this channel
        # names a person, and only with an address that person actually has.
        for candidate in mention_candidates:
            mapped = participant_map.get(candidate, candidate)
            for value in dict.fromkeys((mapped, candidate)):
                proven = _proven_mention_target(knowledge, channel=channel, value=value)
                if proven is None:
                    continue
                person_id, proven_value = proven
                display = knowledge.person_display_name(person_id)
                if display:
                    return ContactResolution(
                        display_name=display,
                        jid=proven_value,
                        matched_identifier=(
                            candidate if candidate != proven_value else None
                        ),
                    )
        return None

    candidates: list[ContactResolution] = []
    for person in knowledge.search_people_with_policy(
        ref, channel=channel, chat_id=chat_id
    ):
        labels = [knowledge.person_display_name(person.person_id) or ""]
        labels.extend(knowledge.alias_names(person.person_id))
        if not _reference_matches_labels(ref, [label for label in labels if label]):
            continue
        identifiers = [
            item.value
            for item in knowledge.person_identifiers(person.person_id)
            if item.channel == channel
        ]
        if participant_ids:
            identifiers = [item for item in identifiers if item in participant_ids]
        chosen = _pick_delivery_value(identifiers)
        if not chosen:
            continue
        candidates.append(
            ContactResolution(
                display_name=person.display_name or chosen,
                jid=chosen,
            )
        )

    if len(candidates) == 1:
        return candidates[0]
    return None


def _resolve_through_legacy(
    contacts: "ContactsService",
    ref: str,
    *,
    channel: str,
    participant_map: dict[str, str],
    participant_ids: set[str],
) -> ContactResolution | None:
    """Transitional resolver for callers that run without a knowledge facade.

    It answers from the legacy contacts cache it was built with and disappears together
    with that cache.  It never widens knowledge authority - knowledge is not wired here.
    """
    mention_candidates = _mention_candidates(ref)
    if mention_candidates:
        for candidate in mention_candidates:
            mapped = participant_map.get(candidate, candidate)
            for identifier in (mapped, candidate):
                contact_id = getattr(contacts, "known_jids", {}).get(identifier)
                display = contacts.get_display_name(contact_id) if contact_id else None
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
        labels = [
            contact.display_name,
            *[
                alias.alias
                for alias in contacts.store.get_aliases(contact.id)
            ],
        ]
        if not _reference_matches_labels(ref, labels):
            continue
        identifiers = [
            ident.identifier
            for ident in contacts.store.get_identifiers(contact.id)
            if ident.channel == channel
        ]
        if participant_ids:
            identifiers = [
                ident for ident in identifiers
                if ident in participant_ids or ident in participant_map.values()
            ]
        chosen = _pick_delivery_value(identifiers)
        if not chosen:
            continue
        candidates.append(ContactResolution(display_name=contact.display_name, jid=chosen))

    if len(candidates) == 1:
        return candidates[0]
    return None


def _proven_person_for_value(knowledge: Any, value: str) -> str | None:
    """The one person a value is proven for, or nothing - never the first owner."""
    owners = knowledge.owners_of_identifier_value(value)
    if len(owners) != 1:
        return None
    return str(owners[0])


def contact_resolution_matches_reference(
    *,
    reference: str,
    resolution: ContactResolution,
    contacts: "ContactsService | None" = None,
    knowledge: object | None = None,
) -> bool:
    """Check that an exact mention resolves to one candidate for a name/alias."""
    if knowledge is not None:
        person_id = _proven_person_for_value(knowledge, resolution.jid)
        if person_id is None and resolution.matched_identifier:
            person_id = _proven_person_for_value(knowledge, resolution.matched_identifier)
        if person_id is None:
            # An unproven person has no labels to confirm against, and the legacy cache
            # is not a second opinion once knowledge is the authority.
            return False
        labels = [knowledge.person_display_name(person_id) or ""]
        labels.extend(knowledge.alias_names(person_id))
        return _reference_matches_labels(reference, [label for label in labels if label])

    if contacts is None:
        return False
    contact_id = getattr(contacts, "known_jids", {}).get(resolution.jid)
    if not contact_id and resolution.matched_identifier:
        contact_id = getattr(contacts, "known_jids", {}).get(resolution.matched_identifier)
    if not contact_id:
        return False
    contact = contacts.store.get_contact(contact_id)
    if contact is None:
        return False
    labels = [contact.display_name]
    labels.extend(alias.alias for alias in contacts.store.get_aliases(contact_id))
    return _reference_matches_labels(reference, [label for label in labels if label])


class ResolveContactTool(Tool):
    """Resolve a contact name or WhatsApp mention to one delivery JID."""

    def __init__(
        self,
        *,
        contacts: "ContactsService | None" = None,
        knowledge: object | None = None,
        chat_registry: "ChatRegistry | None" = None,
    ) -> None:
        self._contacts = contacts
        self._knowledge = knowledge
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
            knowledge=self._knowledge,
            reference=query,
            channel=self._channel,
            chat_id=self._chat_id,
            chat_registry=self._chat_registry,
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
