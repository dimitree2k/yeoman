"""Deterministic continuity signals for thread routing (Plan 07, Aufgabe 2).

The spec allows exactly two signals, and both must be provable from sources that are
demonstrably bound to the candidate thread. Nothing here reads session history, the
ambient window or long-term memory: those may help compose an answer, but they must never
decide which thread a message belongs to.

* **answer_to_question** - a provably *sent* bot question that named the acceptable answers
  and has not been answered since, met by a message that fulfils exactly that expectation.
* **explicit_callback** - the message names the subject of the thread and an addition or
  change to it, both backed by concrete sources.

Everything else is ``none`` or ``unknown``, and neither allows automatic attachment. A
shared keyword alone is explicitly not enough - that is the trap the spec calls out.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

#: Verdicts. Only ``positive`` permits attaching to an existing thread.
VERDICT_POSITIVE = "positive"
VERDICT_NONE = "none"
VERDICT_UNKNOWN = "unknown"

SIGNAL_ANSWER = "answer_to_question"
SIGNAL_CALLBACK = "explicit_callback"
SIGNAL_NONE = "none"

#: Words that carry a change or addition, i.e. an explicit call-back to the subject.
_CHANGE_MARKERS: tuple[str, ...] = (
    "ergänz",
    "ergaenz",
    "ändere",
    "aendere",
    "anpass",
    "korrigier",
    "aktualisier",
    "zusätzlich",
    "zusaetzlich",
    "außerdem",
    "ausserdem",
    "nachtrag",
    "noch dazu",
    "dazu",
    "update",
    "add ",
    "change",
    "append",
    "revise",
)

#: Bare continuations. The spec names these explicitly as *not* a signal on their own.
_BARE_CONTINUATIONS: tuple[str, ...] = (
    "weiter",
    "mach weiter",
    "ja",
    "nein",
    "ok",
    "okay",
    "kurz",
    "genau",
    "passt",
    "go on",
    "continue",
    "yes",
    "no",
)

_STOPWORDS: frozenset[str] = frozenset(
    {
        "aber", "also", "auch", "beim", "bitte", "das", "dass", "der", "die", "dies",
        "doch", "dort", "eine", "einen", "einem", "einer", "eines", "einfach", "für",
        "fuer", "ganz", "habe", "haben", "hier", "ich", "immer", "kann", "kannst",
        "mal", "mein", "meine", "mich", "mit", "nach", "noch", "nur", "oder", "ohne",
        "schon", "sehr", "sich", "sie", "sind", "soll", "sollte", "über", "ueber",
        "und", "uns", "unser", "von", "wann", "warum", "was", "wenn", "wer", "wie",
        "wieder", "wir", "wirst", "würde", "wuerde", "zum", "zur", "the", "and",
        "for", "with", "you", "your", "please", "that", "this",
    }
)


@dataclass(frozen=True, slots=True)
class SourceView:
    """One user-side source of a candidate thread."""

    event_id: str
    text: str
    role: str = "context"
    principal: str = ""
    occurred_ms: int | None = None

    @property
    def is_trigger(self) -> bool:
        return self.role == "trigger"


@dataclass(frozen=True, slots=True)
class BotMessageView:
    """A bot message that is provably sent (a transport-confirmed effect)."""

    message_id: str
    text: str
    occurred_ms: int | None = None


@dataclass(frozen=True, slots=True)
class ContinuitySignal:
    """What a candidate's sources say about the current message."""

    kind: str = SIGNAL_NONE
    verdict: str = VERDICT_NONE
    evidence_ids: tuple[str, ...] = ()
    detail: str = ""

    @property
    def positive(self) -> bool:
        return self.verdict == VERDICT_POSITIVE


def _normalize(text: str) -> str:
    return " ".join(str(text or "").lower().split())


#: Enough characters to identify a German noun across its inflected forms.
_STEM_LENGTH = 7


def _content_terms(text: str) -> set[str]:
    words = re.findall(r"[a-zA-ZäöüÄÖÜß][a-zA-ZäöüÄÖÜß\-]{3,}", _normalize(text))
    return {word.strip("-") for word in words if word not in _STOPWORDS}


def _content_stems(text: str) -> set[str]:
    """Truncated stems, so "Mietvertrag" matches "Mietvertrags"."""
    return {term[:_STEM_LENGTH] for term in _content_terms(text)}


def _is_bare_continuation(text: str) -> bool:
    compact = _normalize(text).strip(" .!?,;:")
    if not compact:
        return True
    if compact in _BARE_CONTINUATIONS:
        return True
    return len(_content_terms(compact)) == 0


def question_options(text: str) -> tuple[str, ...] | None:
    """The acceptable answers a bot question named, or ``None``.

    Recognises the two shapes the spec requires: an explicit either/or and a bracketed or
    slashed set of options. A question without named options is *not* an expectation.
    """
    body = str(text or "").strip()
    if not body:
        return None
    singular = body.rstrip("?!. ")

    bracketed = re.findall(r"\(([^)]{2,40})\)", singular)
    for group in bracketed:
        parts = [part.strip(" ?!.") for part in re.split(r"[/|]", group)]
        parts = [part for part in parts if part and len(part) <= 24]
        if len(parts) >= 2:
            return tuple(parts)

    either_or = re.search(
        r"\b([\wäöüÄÖÜß\-]{2,24})\s+oder\s+([\wäöüÄÖÜß\-]{2,24})\b", singular, re.IGNORECASE
    )
    if either_or:
        left = either_or.group(1).strip("?!.,;:")
        right = either_or.group(2).strip("?!.,;:")
        if left and right and left.lower() not in _STOPWORDS:
            return (left, right)

    slash = re.search(r"\b([\wäöüÄÖÜß\-]{2,24})/([\wäöüÄÖÜß\-]{2,24})\b", singular)
    if slash:
        return (slash.group(1), slash.group(2))
    return None


def _looks_like_question(text: str) -> bool:
    body = str(text or "").strip()
    if not body:
        return False
    if body.endswith("?"):
        return True
    return bool(re.search(r"\b(wie|was|wann|wo|welche[rs]?|soll ich|möchtest du)\b", body, re.I))


def _fulfils(text: str, options: Sequence[str]) -> bool:
    normalized = _normalize(text).strip(" .!?,;:")
    if not normalized:
        return False
    words = set(re.findall(r"[\wäöüÄÖÜß\-]+", normalized))
    for option in options:
        candidate = _normalize(option).strip(" .!?,;:")
        if not candidate:
            continue
        if normalized == candidate:
            return True
        if len(candidate) >= 4 and candidate in normalized:
            return True
        if candidate in words:
            return True
    return False


def answer_to_question(
    *,
    current_text: str,
    thread_sources: Sequence[SourceView],
    bot_messages: Sequence[BotMessageView],
) -> ContinuitySignal:
    """The first signal: the message answers a concrete, still-open bot question."""
    if not bot_messages:
        return ContinuitySignal(detail="no_proven_bot_message")

    newest: BotMessageView | None = None
    for message in bot_messages:
        if _looks_like_question(message.text) and question_options(message.text):
            newest = message
    if newest is None:
        return ContinuitySignal(detail="no_open_question_with_named_options")

    options = question_options(newest.text) or ()
    # A later source that already answered the question lifts the expectation.
    for source in thread_sources:
        if source.event_id == newest.message_id:
            continue
        if newest.occurred_ms is not None and source.occurred_ms is not None:
            if source.occurred_ms <= newest.occurred_ms:
                continue
        if _fulfils(source.text, options):
            return ContinuitySignal(detail="question_already_answered")
    if not _fulfils(current_text, options):
        return ContinuitySignal(detail="expectation_not_met")
    return ContinuitySignal(
        kind=SIGNAL_ANSWER,
        verdict=VERDICT_POSITIVE,
        evidence_ids=(newest.message_id,),
        detail="answered_open_question",
    )


def explicit_callback(
    *, current_text: str, thread_sources: Sequence[SourceView]
) -> ContinuitySignal:
    """The second signal: the message names the subject and an addition to it."""
    if not thread_sources:
        return ContinuitySignal(detail="no_thread_source")
    if _is_bare_continuation(current_text):
        # "weiter", "ja" or "kurz" carry no subject of their own.
        return ContinuitySignal(detail="bare_continuation")

    normalized = _normalize(current_text)
    marker = next((item for item in _CHANGE_MARKERS if item in normalized), "")
    if not marker:
        return ContinuitySignal(detail="no_change_marker")

    triggers = [source for source in thread_sources if source.is_trigger] or list(thread_sources)
    evidence: list[str] = []
    for source in triggers:
        overlap = _content_stems(source.text) & _content_stems(current_text)
        if overlap:
            evidence.append(source.event_id)
    if not evidence:
        # A common keyword is not a call-back: the subject itself must be named.
        return ContinuitySignal(detail="subject_not_named")
    return ContinuitySignal(
        kind=SIGNAL_CALLBACK,
        verdict=VERDICT_POSITIVE,
        evidence_ids=tuple(evidence),
        detail=f"explicit_change:{marker.strip()}",
    )


def continuity_signal(
    *,
    current_text: str,
    thread_sources: Sequence[SourceView],
    bot_messages: Sequence[BotMessageView] = (),
) -> ContinuitySignal:
    """Both signals in the spec's order. Anything unclear stays ``none``."""
    answered = answer_to_question(
        current_text=current_text, thread_sources=thread_sources, bot_messages=bot_messages
    )
    if answered.positive:
        return answered
    callback = explicit_callback(current_text=current_text, thread_sources=thread_sources)
    if callback.positive:
        return callback
    # A question-specific refusal is more informative than "bare continuation".
    if answered.detail in ("question_already_answered", "expectation_not_met"):
        return ContinuitySignal(detail=answered.detail)
    return ContinuitySignal(detail=callback.detail or answered.detail)


__all__ = [
    "SIGNAL_ANSWER",
    "SIGNAL_CALLBACK",
    "SIGNAL_NONE",
    "VERDICT_NONE",
    "VERDICT_POSITIVE",
    "VERDICT_UNKNOWN",
    "BotMessageView",
    "ContinuitySignal",
    "SourceView",
    "answer_to_question",
    "continuity_signal",
    "explicit_callback",
    "question_options",
]
