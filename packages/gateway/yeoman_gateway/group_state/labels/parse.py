"""Pure validator for one conversation-label answer.

Every text field is defanged and capped before it can be stored. A ref the input never
carried drops that entry only, never the whole answer.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from yeoman_gateway.agent.untrusted import neutralize_markers
from yeoman_gateway.reception.labeler import strip_identifiers
from yeoman_gateway.trends.scorer import _json_object

from .prompt import (
    MAX_PLANS,
    MAX_QUESTIONS,
    MAX_TEXT_CHARS,
    MAX_TOPIC_CHARS,
    MAX_WHEN_CHARS,
    MOODS,
    LabelInput,
)


class LabelParseError(ValueError):
    """The answer is not a usable labels object."""


@dataclass(frozen=True, slots=True)
class ParsedLabels:
    topic: str | None
    mood: str | None
    #: ``{text, asked_by (contact id), source_id}``
    open_questions: tuple[dict[str, str], ...]
    #: ``{text, who: [contact ids or "arvid"], when, source_id}``
    plans: tuple[dict[str, Any], ...]
    dropped: int


def parse_labels(raw: str, label_input: LabelInput) -> ParsedLabels:
    """Validate one answer; raises :class:`LabelParseError` for an unusable object."""
    payload = _json_object(str(raw or ""))
    if not isinstance(payload, Mapping):
        raise LabelParseError("not_json_object")
    if any(key not in payload for key in ("topic", "mood", "open_questions", "plans")):
        raise LabelParseError("missing_keys")
    raw_questions = payload["open_questions"]
    raw_plans = payload["plans"]
    if not isinstance(raw_questions, list) or not isinstance(raw_plans, list):
        raise LabelParseError("invalid_shape")

    topic = _text(payload.get("topic"), MAX_TOPIC_CHARS) or None
    mood = _mood(payload.get("mood"))

    questions: list[dict[str, str]] = []
    dropped = 0
    for entry in raw_questions:
        if len(questions) >= MAX_QUESTIONS:
            break
        question, rejected = _question(entry, label_input)
        if rejected:
            dropped += 1
            continue
        questions.append(question)

    plans: list[dict[str, Any]] = []
    for entry in raw_plans:
        if len(plans) >= MAX_PLANS:
            break
        plan, rejected = _plan(entry, label_input)
        if rejected:
            dropped += 1
            continue
        plans.append(plan)

    return ParsedLabels(
        topic=topic,
        mood=mood,
        open_questions=tuple(questions),
        plans=tuple(plans),
        dropped=dropped,
    )


def _question(entry: Any, label_input: LabelInput) -> tuple[dict[str, str], bool]:
    if not isinstance(entry, Mapping):
        return {}, True
    text = _text(entry.get("text"), MAX_TEXT_CHARS)
    if not text:
        return {}, True
    source_ref = str(entry.get("source") or "")
    source_id = label_input.message_refs.get(source_ref)
    if source_id is None:
        return {}, True
    speaker = label_input.speaker_of.get(source_ref)
    if not speaker or speaker == "arvid":
        return {}, True
    if str(entry.get("asked_by") or "") != speaker:
        return {}, True
    contact_id = label_input.speaker_refs.get(speaker)
    if not contact_id:
        return {}, True
    return {"text": text, "asked_by": contact_id, "source_id": source_id}, False


def _plan(entry: Any, label_input: LabelInput) -> tuple[dict[str, Any], bool]:
    if not isinstance(entry, Mapping):
        return {}, True
    text = _text(entry.get("text"), MAX_TEXT_CHARS)
    if not text:
        return {}, True
    source_ref = str(entry.get("source") or "")
    source_id = label_input.message_refs.get(source_ref)
    if source_id is None:
        return {}, True
    raw_who = entry.get("who")
    who: list[str] = []
    if raw_who is not None:
        if not isinstance(raw_who, list):
            return {}, True
        for ref in raw_who:
            if str(ref) == "arvid":
                if "arvid" not in who:
                    who.append("arvid")
                continue
            contact_id = label_input.speaker_refs.get(str(ref))
            if contact_id is None:
                return {}, True
            if contact_id not in who:
                who.append(contact_id)
    when = _text(entry.get("when"), MAX_WHEN_CHARS)
    return {"text": text, "who": who, "when": when, "source_id": source_id}, False


def _mood(value: Any) -> str | None:
    mood = value.strip() if isinstance(value, str) else ""
    return mood if mood in MOODS else None


def _text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    clean = neutralize_markers(value)
    clean = strip_identifiers(clean)
    clean = " ".join(clean.split())
    return clean[: max(0, int(limit))].strip()
