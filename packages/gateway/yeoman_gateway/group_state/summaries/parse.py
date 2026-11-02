"""Pure validator for one conversation or week summary answer.

Every text field is defanged and capped before it can be stored. A ref the input never
carried drops that line only, never the whole answer. A line that names somebody the
caller reported as no longer a member also drops.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from yeoman_gateway.agent.untrusted import neutralize_markers
from yeoman_gateway.reception.labeler import strip_identifiers
from yeoman_gateway.trends.scorer import _json_object

from .prompt import MAX_LINE_CHARS

_NAME_TOKEN = re.compile(r"[\w'-]+", re.UNICODE)


class SummaryParseError(ValueError):
    """The answer is not a usable summary object."""


@dataclass(frozen=True, slots=True)
class SummaryLine:
    text: str
    refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ParsedSummary:
    lines: tuple[SummaryLine, ...]
    dropped: int


def parse_summary_lines(
    raw: str,
    *,
    ref_map: Mapping[str, str],
    max_lines: int,
    member_names: Iterable[str] = (),
    outsider_names: Iterable[str] = (),
) -> ParsedSummary:
    """Validate one answer; unknown refs and non-member names drop that line only."""
    payload = _json_object(str(raw or ""))
    if not isinstance(payload, Mapping) or not isinstance(payload.get("lines"), list):
        raise SummaryParseError("schema_mismatch")
    members = {" ".join(str(name or "").split()).casefold() for name in member_names}
    members.discard("")
    outside = _name_patterns(
        name
        for name in outsider_names
        if " ".join(str(name or "").split()).casefold() not in members
    )
    lines: list[SummaryLine] = []
    dropped = 0
    for entry in payload["lines"]:
        if len(lines) >= max(0, int(max_lines)):
            break
        if not isinstance(entry, Mapping):
            dropped += 1
            continue
        text = _text(entry.get("text"))
        raw_refs = entry.get("refs")
        if not text or not isinstance(raw_refs, list):
            dropped += 1
            continue
        refs = tuple(str(ref) for ref in raw_refs)
        if not refs or any(ref not in ref_map for ref in refs):
            dropped += 1
            continue
        if outside and _names_any(text, outside):
            dropped += 1
            continue
        sources = tuple(dict.fromkeys(str(ref_map[ref]) for ref in refs))
        lines.append(SummaryLine(text=text, refs=sources))
    return ParsedSummary(lines=tuple(lines), dropped=dropped)


def _text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    clean = neutralize_markers(value)
    clean = strip_identifiers(clean)
    clean = " ".join(clean.split())
    return clean[:MAX_LINE_CHARS].strip()


def _name_patterns(names: Iterable[str]) -> list[re.Pattern[str]]:
    terms: set[str] = set()
    for name in names:
        full = " ".join(str(name or "").split())
        if not full:
            continue
        if sum(char.isalpha() for char in full) >= 2:
            terms.add(full)
        terms.update(token for token in _NAME_TOKEN.findall(full) if len(token) >= 3)
    return [
        re.compile(rf"(?<!\w){re.escape(term)}(?!\w)", re.IGNORECASE)
        for term in sorted(terms, key=lambda value: (-len(value), value))
    ]


def _names_any(text: str, patterns: Sequence[re.Pattern[str]]) -> bool:
    return any(pattern.search(text) for pattern in patterns)


__all__ = ["ParsedSummary", "SummaryLine", "SummaryParseError", "parse_summary_lines"]
