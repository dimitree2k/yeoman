"""Read-only contact resolution for delivery targets.

Person resolution uses Knowledge only. A target exists only for a person with an
active proven binding; no channel falls back to an in-memory ContactsService cache.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from loguru import logger

from yeoman_gateway.agent.tools.base import Tool
from yeoman_gateway.history.live import HistoryPaused

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

    if knowledge is None:
        return None
    try:
        return _resolve_through_knowledge(
            knowledge,
            ref,
            channel=channel,
            chat_id=chat_id,
        )
    except HistoryPaused:
        raise
    except Exception as exc:
        # Knowledge is optional for the turn: no authority, no target. The failure
        # stays visible instead of looking exactly like "no proven person".
        logger.warning(
            "contact resolution degraded: knowledge lookup failed ({})",
            getattr(exc, "code", type(exc).__name__),
        )
        return None


def _resolve_through_knowledge(
    knowledge: Any,
    ref: str,
    *,
    channel: str,
    chat_id: str,
) -> ContactResolution | None:
    """Resolve proven targets through the selected chat scope or Knowledge facade."""
    from yeoman_gateway.history.ids import classify
    from yeoman_gateway.knowledge._history_identity import current_history_scope

    scope = current_history_scope()
    ident = classify(chat_id) if channel == "whatsapp" else None
    whatsapp_group = ident is not None and ident.kind == "group"
    if whatsapp_group and scope is None:
        return None
    now_ms = int(__import__("time").time() * 1000)
    roster: set[str] | None = None
    if whatsapp_group:
        if scope is None:
            return None
        audience = scope.queries.members(chat_id=chat_id, at_ms=now_ms)
        if audience.status != "known":
            return None
        roster = {
            terminal
            for principal in audience.members
            if (contact := scope.queries.principal_contact(principal, now_ms))
            and (terminal := scope.queries.terminal(contact))
        }

    mention_candidates = _mention_candidates(ref)
    if mention_candidates:
        # In a group, a mention only resolves through the projected current roster;
        # the original token is never used as a fallback when History cannot prove it.
        for candidate in mention_candidates:
            values = (candidate,)
            if scope is not None and channel == "whatsapp":
                mapped = scope.queries.mention(candidate, chat_id=chat_id, at_ms=now_ms)
                if whatsapp_group and mapped is None:
                    continue
                values = (mapped,) if mapped else (candidate,)
            for value in values:
                proven = _proven_mention_target(knowledge, channel=channel, value=value)
                if proven is None:
                    continue
                person_id, proven_value = proven
                if roster is not None and scope.queries.terminal(person_id) not in roster:
                    continue
                display = knowledge.person_display_name(person_id)
                if display:
                    return ContactResolution(
                        display_name=display,
                        jid=proven_value,
                        matched_identifier=(candidate if candidate != proven_value else None),
                    )
        return None

    if scope is not None and channel == "whatsapp":
        found = scope.queries.resolve_person(ref, (chat_id,), now_ms)
        people = [(found.person_id, (scope.queries.contact(found.person_id) or {}).get("display_name"))] if found.person_id else []
        if roster is not None:
            people = [
                (person_id, display_name)
                for person_id, display_name in people
                if scope.queries.terminal(person_id) in roster
            ]
    else:
        people = [(person.person_id, person.display_name) for person in knowledge.search_people_with_policy(
            ref, channel=channel, chat_id=chat_id
        )]
    candidates: list[ContactResolution] = []
    for person_id, display_name in people:
        labels = [knowledge.person_display_name(person_id) or ""]
        labels.extend(knowledge.alias_names(person_id))
        if not _reference_matches_labels(ref, [label for label in labels if label]):
            continue
        identifiers = [
            item.value
            for item in knowledge.person_identifiers(person_id)
            if item.channel == channel
        ]
        chosen = _pick_delivery_value(identifiers)
        if not chosen:
            continue
        candidates.append(ContactResolution(display_name=display_name or chosen, jid=chosen))

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
    knowledge: object | None = None,
) -> bool:
    """Check that a resolved identifier belongs to one knowledge-backed name."""
    if knowledge is None:
        return False
    person_id = _proven_person_for_value(knowledge, resolution.jid)
    if person_id is None and resolution.matched_identifier:
        person_id = _proven_person_for_value(knowledge, resolution.matched_identifier)
    if person_id is None:
        return False
    labels = [knowledge.person_display_name(person_id) or ""]
    labels.extend(knowledge.alias_names(person_id))
    return _reference_matches_labels(reference, [label for label in labels if label])


class ResolveContactTool(Tool):
    """Resolve a contact name or WhatsApp mention to one delivery JID."""

    def __init__(self, *, knowledge: object | None = None) -> None:
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
        from yeoman_gateway.processing.tool_context import tool_history_snapshot
        snapshot = tool_history_snapshot()
        if snapshot is not None and self._knowledge is None:
            from yeoman_gateway.history.live import HistoryPaused
            raise HistoryPaused('history_knowledge_required')
        del kwargs
        result = resolve_contact_reference(
            knowledge=self._knowledge,
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
