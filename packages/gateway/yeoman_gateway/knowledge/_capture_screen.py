"""Deterministic input and candidate screens for live statement capture."""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final

REJECT_BASES: tuple[str, ...] = (
    "opinion",
    "speculation",
    "inference",
    "person_speculation",
    "delivery_claim",
)

_MIN_INPUT_CHARS: Final[int] = 3

_CONTROL_CHARS_INPUT: Final[re.Pattern[str]] = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

_PLACEHOLDER_INPUT: Final[re.Pattern[str]] = re.compile(
    r"^(?:[\.\u2026\-\*_\s]+|<?(?:media|image|video|audio|document|sticker|attachment)>?"
    r"|\[(?:media|image|video|audio|document|sticker|attachment)\])$",
    re.IGNORECASE,
)

@dataclass(frozen=True, slots=True)
class ScreenDecision:
    """The outcome of one deterministic screen: a verdict plus a stable reason code."""

    accepted: bool
    reason: str = "ok"

    @property
    def rejected(self) -> bool:
        return not self.accepted

@dataclass(frozen=True, slots=True)
class CandidateVerdict:
    accepted: bool
    reason: str

    @property
    def rejected(self) -> bool:
        return not self.accepted

META_STATEMENT_MARKERS: tuple[str, ...] = (
    "der autor sagt",
    "die autorin sagt",
    "der nutzer sagt",
    "the author says",
    "the user says",
    "die nachricht besagt",
    "the message says",
    "laut nachricht",
    "sagt:",
    "says:",
)

def is_meta_statement(content: str) -> bool:
    """True when a candidate only reports what was said instead of stating a fact."""
    lowered = content.strip().lower()
    return any(marker in lowered for marker in META_STATEMENT_MARKERS)

_CONVERSATION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\bich habe\b.{0,40}\b(erklärt|gesagt|gefragt|geschrieben|erzählt|gezeigt|"
        r"empfohlen|berichtet|geantwortet)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bwir haben\b.{0,40}\b(besprochen|geredet|diskutiert|geklärt|ausgemacht)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(wie besprochen|im gespräch|in der unterhaltung|in diesem chat|chatverlauf|"
        r"as discussed|we discussed|i explained|i asked|i said)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(der autor|die autorin|der fragesteller|der nutzer|der gesprächspartner|"
        r"the author|the user)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(claude|chatgpt|yeoman|der bot|der assistent|the assistant|the bot)\b",
        re.IGNORECASE,
    ),
)

_HEDGE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(würde|würden|könnte|könnten|dürfte|vielleicht|eventuell|möglicherweise|"
        r"vermutlich|angeblich|probably|maybe|might|would)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(ich glaube|ich denke|ich vermute|meiner meinung|i think|i guess)\b",
        re.IGNORECASE,
    ),
)

_REPORTED_SPEECH_VERBS = (
    "gefragt",
    "nachgefragt",
    "gesagt",
    "erwähnt",
    "erwaehnt",
    "genannt",
    "berichtet",
    "geantwortet",
    "diskutiert",
    "besprochen",
    "erzählt",
    "erzaehlt",
    "empfohlen",
    "mitgeteilt",
    "behauptet",
    "angemerkt",
    "geäußert",
    "geaeussert",
)

_REPORTED_SPEECH_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Auxiliary before participle: "es wird gefragt", "wurde ein Angebot genannt".  The
    # bounded gap may not cross sentence punctuation, so an unrelated clause in the same
    # sentence cannot be pulled in.
    re.compile(
        r"\b(wird|werden|wurde|wurden)\s+[^.,;:!?]{0,25}?\b("
        + "|".join(_REPORTED_SPEECH_VERBS)
        + r")\b",
        re.IGNORECASE,
    ),
    # Participle before auxiliary: "Nachgefragt wird, ...", "Erwähnt werden muss ...".
    re.compile(
        r"\b("
        + "|".join(_REPORTED_SPEECH_VERBS)
        + r")\s+(wird|werden|wurde|wurden|ist|sind)\b",
        re.IGNORECASE,
    ),
    # Named third-person reporting: "Er erwähnte, dass ...", "Man sagt, dass ...".  The
    # same junk class as the impersonal forms, and the extractor prompt forbids it too:
    # a statement says what is true, not who reported it.
    re.compile(
        r"\b(er|sie|es|man|der|die|das)\s+(sagt|sagte|fragt|fragte|erwähnt|erwähnte|"
        r"erwaehnt|erwaehnte|berichtet|berichtete|erzählt|erzählte|erzaehlt|erzaehlte|"
        r"meint|meinte|antwortet|antwortete|schreibt|schrieb)\b",
        re.IGNORECASE,
    ),
    # A question or a statement as the subject of the sentence.
    re.compile(
        r"\b(die|eine|der)\s+(frage|fragen)\s+(wurde|wird|wurden|werden|ist|sind)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(es\s+ist\s+die\s+rede|die\s+rede\s+ist|zur\s+sprache\s+kam|"
        r"es\s+wird\s+darauf\s+hingewiesen)\b",
        re.IGNORECASE,
    ),
    # English equivalents.
    re.compile(
        r"\b(it\s+(is|was)\s+(said|asked|mentioned|reported|noted)|"
        r"(was|were)\s+(said|asked|mentioned|reported|discussed))\b",
        re.IGNORECASE,
    ),
)

def screen_capture_input(text: str, source_context: Mapping[str, Any] | None = None) -> "ScreenDecision":
    """The *input* screen: what may be sent to the model at all.

    Deliberately weaker than the candidate screen.  It refuses only what is unusable as
    input - an empty section, control characters, a section that is nothing but a
    placeholder - and it must **never** refuse a section merely because it contains
    reported speech.  "Tom erwähnte, dass Alex nach Köln gezogen ist" is a perfectly good
    input: the sentence carries a reportable proposition, and whether it survives as a
    statement is the candidate screen's decision, taken with the roles and sources in
    front of it.

    A rule that rejects input on the vocabulary of reporting would silently destroy the
    proposition, and no later check could recover it.
    """
    context: Mapping[str, Any] = source_context or {}
    if not isinstance(text, str):
        return ScreenDecision(False, "invalid_input")
    if not text.strip():
        return ScreenDecision(False, "empty")
    if _CONTROL_CHARS_INPUT.search(text):
        return ScreenDecision(False, "control_characters")
    stripped = text.strip()
    if len(stripped) < _MIN_INPUT_CHARS:
        return ScreenDecision(False, "too_short")
    if _PLACEHOLDER_INPUT.match(stripped):
        return ScreenDecision(False, "placeholder")
    # An explicitly revoked basis never reaches a provider.
    if str(context.get("source_status") or "") == "revoked":
        return ScreenDecision(False, "source_revoked")
    return ScreenDecision(True, "ok")

def screen_statement_candidate(
    draft: Any,
    *,
    allowed_people: Iterable[str] = (),
    sources: Iterable[Any] | None = None,
) -> "ScreenDecision":
    """The *candidate* screen: what may become a durable statement.

    This is where the reporting rules belong, together with the structural checks the
    input screen cannot make: a proposition of its own, at least one source, and person
    references that the runtime actually offered.  A model may neither invent a person
    identifier nor a role.
    """
    content = str(getattr(draft, "content", "") or "")
    verdict = screen_statement_content(content)
    if verdict.rejected:
        return ScreenDecision(False, str(verdict.reason))

    basis = str(getattr(draft, "basis", "") or "").strip().lower()
    if basis and basis in REJECT_BASES:
        return ScreenDecision(False, basis)
    if basis and basis not in ("explicit_statement", "reported_statement"):
        return ScreenDecision(False, "unsupported_basis")

    offered = {str(item) for item in allowed_people}
    for reference in _candidate_person_references(draft):
        if reference not in offered:
            # A reference the runtime never offered is discarded, never resolved by a
            # name lookup and never turned into a role.
            return ScreenDecision(False, "unoffered_person_reference")

    if sources is not None and not list(sources):
        # A candidate without a source revision is not a statement; it is a claim.
        return ScreenDecision(False, "missing_source")
    return ScreenDecision(True, "ok")

def _candidate_person_references(draft: Any) -> tuple[str, ...]:
    """Person identifiers a candidate mentions, however it spells them."""
    out: list[str] = []
    for attribute in ("person_id", "person_ids", "subject_ids", "people"):
        value = getattr(draft, attribute, None)
        if value is None:
            continue
        for item in value if isinstance(value, (list, tuple, set)) else (value,):
            token = str(getattr(item, "person_id", item) or "").strip()
            if token:
                out.append(token)
    return tuple(dict.fromkeys(out))

def conversation_report_reason(content: str) -> str:
    """``conversation_report`` when the text only reports what happened in the chat."""
    for pattern in _REPORTED_SPEECH_PATTERNS:
        if pattern.search(content):
            return "conversation_report"
    return ""

def screen_content(content: str) -> CandidateVerdict:
    """The content-only screens, shared by candidate screening and re-screening.

    These are the deterministic rules that decided whether already-stored facts may stay,
    so the same function runs over new candidates and over the existing database.
    """
    if not content.strip():
        return CandidateVerdict(False, "empty")
    if is_meta_statement(content):
        return CandidateVerdict(False, "meta_statement")
    for pattern in _CONVERSATION_PATTERNS:
        if pattern.search(content):
            return CandidateVerdict(False, "conversation_reference")
    report = conversation_report_reason(content)
    if report:
        return CandidateVerdict(False, report)
    for pattern in _HEDGE_PATTERNS:
        if pattern.search(content):
            return CandidateVerdict(False, "hedged_statement")
    return CandidateVerdict(True, "ok")

def is_hedged(content: str) -> bool:
    """True when the wording weighs a possibility instead of asserting a fact."""
    return any(pattern.search(content) for pattern in _HEDGE_PATTERNS)

def screen_statement_content(content: str) -> CandidateVerdict:
    """Content screens for a *statement*, where uncertainty is kept, not erased.

    The shared screens refuse a hedged sentence because a shared fact is meant to be an
    asserted fact.  A statement has a lifecycle instead: it is stored as an assertion and
    can never become confirmed without an explicit later confirmation.  So the same rules
    apply here, with one deliberate difference - a hedge is not a refusal reason but an
    uncertainty marker the caller records on the statement.  Everything that merely
    restates the conversation is still refused, because it says nothing about the world.
    """
    if not content.strip():
        return CandidateVerdict(False, "empty")
    if is_meta_statement(content):
        return CandidateVerdict(False, "meta_statement")
    for pattern in _CONVERSATION_PATTERNS:
        if pattern.search(content):
            return CandidateVerdict(False, "conversation_reference")
    report = conversation_report_reason(content)
    if report:
        return CandidateVerdict(False, report)
    return CandidateVerdict(True, "ok")
