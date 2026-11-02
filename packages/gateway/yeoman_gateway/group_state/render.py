"""Render one group state as the bounded judge view.

The block is runtime data only: counts, relative times and display names that the
sections already neutralized and capped. It never contains message text, message
ids, opportunity ids or instructions.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from yeoman_gateway.history.ids import classify

from .contract import ReaderContext
from .models import GroupState, Item, Section

MINUTE_MS = 60_000
HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS
#: A stale state may still render when nobody wrote in the chat for this long.
ACTIVE_WINDOW_MS = 30 * MINUTE_MS
DEFAULT_MAX_AGE_MS = 600_000
DEFAULT_MAX_CHARS = 1_200
#: The sections every rendered state needs; their oldest write is the state's age.
REQUIRED_SECTIONS: tuple[str, ...] = ("presence", "budgets")

#: Render order; the last section is dropped first when the budget is short.
SECTION_ORDER: tuple[str, ...] = (
    "presence",
    "budgets",
    "arvid_actions",
    "conversations",
    "commitments",
    "people_activity",
)


@dataclass(frozen=True, slots=True)
class RenderedState:
    """The judge view and what happened to it; ``text`` is empty when not rendered."""

    text: str
    revision: int | None
    age_ms: int | None
    sections: tuple[str, ...]
    dropped: tuple[str, ...]
    fallback: str | None
    #: Non-expired, fingerprint-current item ids; step B renders their content.
    items: tuple[str, ...] = ()


def _int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except TypeError, ValueError:
        return None


def _count(value: Any) -> int:
    number = _int(value)
    return max(0, number) if number is not None else 0


def _age(now_ms: int, moment: Any) -> str:
    """Relative age in the block's vocabulary; never an epoch number."""
    stamp = _int(moment)
    if stamp is None:
        return "unknown"
    delta = max(0, int(now_ms) - stamp)
    if delta < MINUTE_MS:
        return "0 min"
    minutes = delta // MINUTE_MS
    if minutes < 60:
        return f"{minutes} min"
    hours = delta // HOUR_MS
    if hours < 24:
        return f"{hours} h"
    return f"{delta // DAY_MS} d"


def _mappings(value: Any) -> list[Mapping[str, Any]]:
    return (
        [item for item in value if isinstance(item, Mapping)]
        if isinstance(value, (list, tuple))
        else []
    )


def _presence_line(data: Mapping[str, Any], now_ms: int) -> str:
    active = ", ".join(
        f"{str(item.get('name') or '?')} ({_count(item.get('messages'))})"
        for item in _mappings(data.get("active_30m"))
    )
    line = (
        f"presence: {_count(data.get('messages_30m'))} messages in 30 min, "
        f"{_count(data.get('messages_120m'))} in 2 h; active: {active or 'none'}"
    )
    if _int(data.get("last_message_ms")) is not None:
        line += f"; last message {_age(now_ms, data.get('last_message_ms'))} ago"
    if _int(data.get("arvid_last_ms")) is not None:
        line += f"; Arvid last {_age(now_ms, data.get('arvid_last_ms'))} ago"
    return line


def _budgets_line(data: Mapping[str, Any], now_ms: int) -> str:
    limit = data.get("judge_hourly_limit")
    limit_text = "unknown" if limit is None else str(_count(limit))
    line = (
        f"budgets: judge calls {_count(data.get('judge_calls_last_hour'))}/{limit_text} this hour, "
        f"comments {_count(data.get('comments_last_30m'))} in 30 min, "
        f"reactions {_count(data.get('reactions_last_30m'))} in 30 min"
    )
    if _int(data.get("last_comment_ms")) is not None:
        line += f", last comment {_age(now_ms, data.get('last_comment_ms'))} ago"
    return line


def _arvid_actions_line(data: Mapping[str, Any], now_ms: int) -> str:
    counts = data.get("counts_24h")
    counts = counts if isinstance(counts, Mapping) else {}
    line = (
        f"arvid_actions: 24 h sent {_count(counts.get('sent'))}, "
        f"comments {_count(counts.get('comments'))}, "
        f"reactions {_count(counts.get('reactions'))}, "
        f"silences {_count(counts.get('silences'))}, "
        f"failures {_count(counts.get('failures'))}"
    )
    comments = [
        _int(item.get("at_ms"))
        for item in _mappings(data.get("items"))
        if str(item.get("kind") or "") == "judge" and str(item.get("outcome") or "") == "comment"
    ]
    latest = max((moment for moment in comments if moment is not None), default=None)
    if latest is not None:
        line += f"; last comment {_age(now_ms, latest)} ago"
    return line


def _participant_count(value: Any) -> int:
    """Participants are contact ids (strings); count the distinct non-empty ones."""
    if not isinstance(value, (list, tuple)):
        return 0
    return len({str(item) for item in value if str(item or "").strip()})


def _conversations_line(data: Mapping[str, Any], now_ms: int) -> str:
    rows = _mappings(data.get("conversations"))
    line = f"conversations: {len(rows)} in window"
    if not rows:
        return line
    newest = max(rows, key=lambda item: _int(item.get("last_ms")) or 0)
    involved = _count(newest.get("arvid_messages")) > 0 or bool(newest.get("arvid_addressed"))
    line += (
        f"; newest {_age(now_ms, newest.get('last_ms'))} ago, "
        f"{_participant_count(newest.get('participants'))} people, "
        f"Arvid {'involved' if involved else 'not involved'}"
    )
    return line


def _commitments_line(data: Mapping[str, Any], now_ms: int) -> str:
    decisions = len(_mappings(data.get("decisions")))
    polls = len(_mappings(data.get("polls")))
    reminders = len(_mappings(data.get("reminders")))
    parts = [
        f"{decisions} open decision{'s' if decisions != 1 else ''}" if decisions else "",
        f"{polls} open poll{'s' if polls != 1 else ''}" if polls else "",
        f"{reminders} reminder{'s' if reminders != 1 else ''}" if reminders else "",
    ]
    return "commitments: " + (", ".join(part for part in parts if part) or "none open")


def _people_activity_line(data: Mapping[str, Any], now_ms: int) -> str:
    entries: list[str] = []
    for item in _mappings(data.get("people"))[:5]:
        replies = _count(item.get("replies_to_arvid_7d"))
        entries.append(
            f"{str(item.get('name') or '?')} "
            f"{_count(item.get('messages_24h'))}/{_count(item.get('messages_7d'))} msgs (24 h/7 d), "
            f"last seen {_age(now_ms, item.get('last_seen_ms'))} ago, "
            f"{replies} repl{'y' if replies == 1 else 'ies'} to Arvid"
        )
    return "people_activity: " + ("; ".join(entries) or "none in window")


_RENDERERS = {
    "presence": _presence_line,
    "budgets": _budgets_line,
    "arvid_actions": _arvid_actions_line,
    "conversations": _conversations_line,
    "commitments": _commitments_line,
    "people_activity": _people_activity_line,
}


def _chat_active(state: GroupState, now_ms: int) -> bool:
    """True when any known message (inbound or Arvid) is within the active window."""
    section: Section | None = state.sections.get("presence")
    data = section.data if section is not None else {}
    for field in ("last_message_ms", "arvid_last_ms"):
        moment = _int(data.get(field))
        if moment is not None and 0 <= int(now_ms) - moment <= ACTIVE_WINDOW_MS:
            return True
    return False


def _live_items(
    state: GroupState,
    now_ms: int,
    fingerprint_of: Callable[[str], str | None] | None,
) -> tuple[str, ...]:
    """Item ids whose own age and source fingerprints still hold."""
    visible: list[str] = []
    for section in state.sections.values():
        for item in section.items:
            if item.expired(now_ms):
                continue
            if fingerprint_of is not None and not _fingerprints_match(item, fingerprint_of):
                continue
            visible.append(item.id)
    return tuple(visible)


def _fingerprints_match(item: Item, fingerprint_of: Callable[[str], str | None]) -> bool:
    return all(
        fingerprint_of(source) == expected for source, expected in item.source_fingerprints.items()
    )


def _guard_state(
    state: GroupState | None,
    *,
    reader: ReaderContext,
    now_ms: int,
    max_age_ms: int,
    quiet_stale_ok: bool,
) -> tuple[str | None, int | None]:
    """The shared reader/dm/required-section/staleness gate; ``(fallback, age_ms)``."""
    if state is None:
        return "no_state", None
    if (reader.channel, reader.chat_id) != (state.channel, state.chat_id):
        return "reader_mismatch", None
    if getattr(classify(reader.chat_id), "kind", None) != "group":
        return "dm", None
    if any(key not in state.sections for key in REQUIRED_SECTIONS):
        return "no_state", None
    age_ms = state.required_age_ms(now_ms, REQUIRED_SECTIONS)
    if age_ms is None:
        return "no_state", None
    if age_ms > max_age_ms and not (quiet_stale_ok and not _chat_active(state, now_ms)):
        # The judge may keep a quiet chat's old state; a view never shows old presence
        # as current. Otherwise a chat that is talking again needs a rebuild.
        return "stale", age_ms
    return None, age_ms


def render_judge_view(
    state: GroupState | None,
    *,
    reader: ReaderContext,
    now_ms: int,
    max_age_ms: int = DEFAULT_MAX_AGE_MS,
    max_chars: int = DEFAULT_MAX_CHARS,
    unavailable: frozenset[str] = frozenset(),
    fingerprint_of: Callable[[str], str | None] | None = None,
) -> RenderedState:
    """The bounded ``[Group State]`` block, or a recorded reason not to render it."""
    fallback, age_ms = _guard_state(
        state,
        reader=reader,
        now_ms=now_ms,
        max_age_ms=max_age_ms,
        quiet_stale_ok=True,
    )
    if fallback is not None:
        # Only a stale state used to carry item ids; the other fallbacks did not.
        items = (
            _live_items(state, now_ms, fingerprint_of)
            if fallback == "stale" and state is not None
            else ()
        )
        return RenderedState(
            "", state.revision if state is not None else None, age_ms, (), (), fallback, items
        )
    items = _live_items(state, now_ms, fingerprint_of) if state is not None else ()
    assert age_ms is not None

    dropped: list[str] = []
    candidates: list[tuple[str, str]] = []
    for key in SECTION_ORDER:
        if key in unavailable:
            continue
        section = state.sections.get(key)
        if section is None:
            continue
        if int(now_ms) - int(section.produced_at_ms) > int(section.ttl_ms):
            dropped.append(key)
            continue
        line = _RENDERERS[key](section.data, now_ms)
        if line:
            candidates.append((key, line))

    header = f"[Group State] revision {state.revision}, {_age(now_ms, now_ms - age_ms)} old"
    if len(header) > max_chars:
        dropped.extend(key for key, _ in candidates)
        return RenderedState("", state.revision, age_ms, (), tuple(dropped), None, items)
    while candidates:
        text = "\n".join([header, *(line for _, line in candidates)])
        if len(text) <= max_chars:
            break
        # Drop whole sections, lowest priority first; never truncate a line.
        dropped.append(candidates.pop()[0])
    text = "\n".join([header, *(line for _, line in candidates)]) if candidates else header
    return RenderedState(
        text=text,
        revision=state.revision,
        age_ms=age_ms,
        sections=tuple(key for key, _ in candidates),
        dropped=tuple(dropped),
        fallback=None,
        items=items,
    )


__all__ = [
    "DEFAULT_MAX_AGE_MS",
    "DEFAULT_MAX_CHARS",
    "SECTION_ORDER",
    "RenderedState",
    "render_judge_view",
]
