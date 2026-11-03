"""Pure renderers for the reply and writer group-state views.

The hot path is one ``store.get`` plus JSON plus this render: no history read, no model
call and no await on a lease. There is deliberately no source-fingerprint check here;
the invalidation pass scrubs changed items within one tick, so a view can briefly show
an item the next tick would remove. It never reads history to verify that itself.

Every derived line lives inside one ``wrap_external_content("group-state", ...)``
envelope. Runtime facts (presence, counts, ages) stay plain lines outside it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from yeoman_gateway.agent.untrusted import neutralize_markers, wrap_external_content
from yeoman_gateway.knowledge.models import PersonNoteView
from yeoman_gateway.reception.labeler import strip_identifiers

from .contract import ReaderContext
from .models import GroupState, Item, Section
from .render import (
    SECTION_ORDER,
    RenderedState,
    _age,
    _count,
    _guard_state,
    _int,
    _mappings,
)

MAX_VIEW_NAME_CHARS = 40
MAX_VIEW_TEXT_CHARS = 120
MAX_VIEW_NAMES = 6
MAX_SOURCE_REFS = 3
MAX_ACTIONS = 3
MAX_PEOPLE_CHARS = 700
MAX_PAST_CHARS = 600
MAX_PAST_CONVERSATIONS = 3
MAX_TURN_PEOPLE = 4
_ARVID = "arvid"
_DERIVED_KINDS = frozenset({"topic", "mood", "open_question", "plan"})


@dataclass(frozen=True, slots=True)
class ViewTarget:
    """The message this view is about, as history ids when they are known.

    The three principal fields are filled by the caller from the event it already has.
    They are deliberately untyped strings: a roster principal, a phone jid and a LID
    all arrive here, and the reader resolves each through proven identity only.
    """

    message_id: str | None = None
    reply_to_message_id: str | None = None
    sender_principal: str | None = None
    reply_to_principal: str | None = None
    mentioned_principals: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "mentioned_principals",
            tuple(str(item) for item in self.mentioned_principals if str(item or "")),
        )


def select_turn_people(
    target: ViewTarget,
    conversation_speakers: Sequence[str],
    *,
    cap: int = MAX_TURN_PEOPLE,
) -> tuple[str, ...]:
    """The people the turn is about, in order, without any history read.

    Sender first, then the replied-to author, then explicit mentions, then the latest
    speakers from the stored conversation section.  Arvid is never a "person here".
    """
    ordered: list[str] = []
    for value in (
        target.sender_principal,
        target.reply_to_principal,
        *target.mentioned_principals,
        *conversation_speakers,
    ):
        text = str(value or "").strip()
        if not text:
            continue
        if text.casefold() == _ARVID or text.casefold().startswith(f"whatsapp:{_ARVID}"):
            continue
        if text not in ordered:
            ordered.append(text)
        if len(ordered) >= max(1, int(cap)):
            break
    return tuple(ordered)


def conversation_speakers(state: GroupState) -> tuple[str, ...]:
    """Participants of the newest stored conversation, newest row only."""
    rows = _conversation_rows(state)
    if not rows:
        return ()
    newest = max(rows, key=lambda row: _int(row.get("last_ms")) or 0)
    return tuple(str(value) for value in newest.get("participants") or () if str(value))


def _linked_conversation_ids(state: GroupState, target: ViewTarget) -> tuple[str, ...]:
    """Stored conversations that contain the quoted message, in section order."""
    if not target.reply_to_message_id:
        return ()
    found: list[str] = []
    for row in _conversation_rows(state):
        if target.reply_to_message_id not in _message_ids(row):
            continue
        conversation_id = str(row.get("conversation_id") or "")
        if conversation_id and conversation_id not in found:
            found.append(conversation_id)
    return tuple(found)


def _person_lines(name: str, note: PersonNoteView) -> list[str]:
    rendered: list[str] = []
    prefix = f"{name}: "
    for line in note.lines:
        text = _clean_text(line.text, MAX_VIEW_TEXT_CHARS)
        if not text:
            continue
        if text.casefold().startswith(prefix.casefold()):
            text = text[len(prefix) :].strip()
        if text and text not in rendered:
            rendered.append(text)
    return rendered


def render_people_block(
    notes: Iterable[PersonNoteView],
    *,
    names: Mapping[str, str] | None = None,
    max_chars: int = MAX_PEOPLE_CHARS,
) -> str:
    """Render active notes for the selected turn people inside ``[People here]``.

    A note without a group-safe display name is dropped, never rendered as a raw id.
    Lines are dropped from the bottom whole; the block is never cut mid-line.
    """
    display = dict(names or {})
    lines: list[str] = ["[People here]"]
    seen: set[str] = set()
    for note in notes:
        if note.status != "active" or not note.lines:
            continue
        person_id = str(note.person_id)
        if person_id in seen:
            continue
        name = _clean_name(display.get(person_id) or "")
        if not name:
            continue
        body = _person_lines(name, note)
        if not body:
            continue
        lines.append(f"- {name}: {'; '.join(body)}")
        seen.add(person_id)
    while len(lines) > 1 and len("\n".join(lines)) > max(0, int(max_chars)):
        lines.pop()
    text = "\n".join(lines)
    return text if len(text) <= max(0, int(max_chars)) else ""


def _summary_lines(item: Item, *, limit: int) -> list[str]:
    raw = item.data.get("lines")
    if not isinstance(raw, (list, tuple)):
        return []
    rendered: list[str] = []
    for value in raw:
        text = _clean_text(value, MAX_VIEW_TEXT_CHARS)
        if text and text not in rendered:
            rendered.append(text)
        if len(rendered) >= max(1, int(limit)):
            break
    return rendered


def render_past_block(
    past: Iterable[Item],
    *,
    linked_conversation_ids: Sequence[str] = (),
    older_than_ms: int | None = None,
    exclude_conversation_ids: Sequence[str] = (),
    now_ms: int | None = None,
    max_chars: int = MAX_PAST_CHARS,
) -> str:
    """Render linked, then the last three older ended conversations, then a week.

    All inputs are stored summaries; nothing here reads history.  Lines are dropped
    from the bottom whole and the block is never cut mid-line.
    """
    items = tuple(past.items if isinstance(past, Section) else past)
    conversations: dict[str, Item] = {}
    weeks: list[Item] = []
    for item in items:
        if now_ms is not None and item.expired(int(now_ms)):
            continue
        if item.kind == "conversation_summary" and item.conversation_id:
            conversations[str(item.conversation_id)] = item
        elif item.kind == "week_summary":
            weeks.append(item)

    excluded = {str(value) for value in exclude_conversation_ids if str(value)}
    chosen: list[Item] = []
    seen: set[str] = set()
    for conversation_id in linked_conversation_ids:
        item = conversations.get(str(conversation_id))
        if item is not None and str(conversation_id) not in seen:
            chosen.append(item)
            seen.add(str(conversation_id))

    ended = sorted(
        (item for key, item in conversations.items() if key not in seen and key not in excluded),
        key=lambda item: (
            _int(item.data.get("ended_ms")) or int(item.produced_at_ms),
            str(item.conversation_id or ""),
        ),
        reverse=True,
    )
    for item in ended:
        if len(chosen) >= len(seen) + MAX_PAST_CONVERSATIONS:
            break
        ended_ms = _int(item.data.get("ended_ms"))
        if older_than_ms is not None and ended_ms is not None and ended_ms >= int(older_than_ms):
            continue
        chosen.append(item)

    week = max(weeks, key=lambda item: str(item.data.get("iso_week") or ""), default=None)
    lines: list[str] = ["[Earlier in this group]"]
    for item in chosen:
        body = _summary_lines(item, limit=3)
        if body:
            lines.append(f"- {'; '.join(body)}")
    if week is not None:
        body = _summary_lines(week, limit=5)
        if body:
            lines.append(f"- week {_clean_text(week.data.get('iso_week'), 16)}: {'; '.join(body)}")
    while len(lines) > 1 and len("\n".join(lines)) > max(0, int(max_chars)):
        lines.pop()
    text = "\n".join(lines)
    return text if len(text) <= max(0, int(max_chars)) else ""


def _clean_text(value: object, limit: int) -> str:
    """Defang, strip identifiers, collapse whitespace and cap again at render."""
    text = neutralize_markers(str(value or ""))
    text = strip_identifiers(text)
    text = " ".join(text.split())
    return text[: max(0, int(limit))].strip()


def _clean_name(value: object) -> str:
    return _clean_text(value, MAX_VIEW_NAME_CHARS)


def _name_map(state: GroupState) -> dict[str, str]:
    """Contact ids to display names, from the state's own name fields only."""
    names: dict[str, str] = {}
    people = state.sections.get("people_activity")
    if people is not None:
        for row in _mappings(people.data.get("people")):
            contact_id = str(row.get("contact_id") or "")
            name = _clean_name(row.get("name"))
            if contact_id and name:
                names[contact_id] = name
    presence = state.sections.get("presence")
    if presence is not None:
        for field in ("active_120m", "active_30m"):
            for row in _mappings(presence.data.get(field)):
                contact_id = str(row.get("contact_id") or "")
                name = _clean_name(row.get("name"))
                if contact_id and name:
                    names[contact_id] = name
    return names


def _display_name(names: Mapping[str, str], contact_id: object) -> str:
    key = str(contact_id or "")
    if key == "arvid":
        return "Arvid"
    return names.get(key) or "someone"


def _source_refs(sources: tuple[str, ...]) -> str:
    refs: list[str] = []
    for source in sources:
        ref = str(source or "")[-8:]
        if ref and ref not in refs:
            refs.append(ref)
        if len(refs) >= MAX_SOURCE_REFS:
            break
    return f" [src={' '.join(refs)}]" if refs else ""


def _message_ids(row: Mapping[str, Any]) -> set[str]:
    raw = row.get("message_ids")
    if isinstance(raw, (list, tuple)):
        return {str(item) for item in raw if str(item)}
    # Older documents were written without the list; their edge ids still work.
    return {
        str(row.get(field)) for field in ("first_message_id", "last_message_id") if row.get(field)
    }


def _conversation_rows(state: GroupState) -> list[Mapping[str, Any]]:
    section = state.sections.get("conversations")
    return _mappings(section.data.get("conversations")) if section is not None else []


def _choose_conversation(
    rows: list[Mapping[str, Any]],
    target: ViewTarget,
    *,
    now_ms: int,
    lull_ms: int,
) -> Mapping[str, Any] | None:
    """Quoted message first, then the current message, then the still-active newest."""
    if not rows:
        return None
    if target.reply_to_message_id:
        for row in rows:
            if target.reply_to_message_id in _message_ids(row):
                return row
    if target.message_id:
        for row in rows:
            if target.message_id in _message_ids(row):
                return row
    newest = max(rows, key=lambda row: _int(row.get("last_ms")) or 0)
    last_ms = _int(newest.get("last_ms"))
    if last_ms is not None and int(now_ms) - last_ms < max(0, int(lull_ms)):
        return newest
    return None


def _participant_names(row: Mapping[str, Any], names: Mapping[str, str]) -> str:
    rendered: list[str] = []
    for contact_id in row.get("participants") or ():
        name = _display_name(names, contact_id)
        if name not in rendered:
            rendered.append(name)
        if len(rendered) >= MAX_VIEW_NAMES:
            break
    return ", ".join(rendered)


def _actions(state: GroupState) -> list[Mapping[str, Any]]:
    section = state.sections.get("arvid_actions")
    return _mappings(section.data.get("items")) if section is not None else []


def _sent_in_conversation(actions: list[Mapping[str, Any]], row: Mapping[str, Any]) -> int:
    start_ms = _int(row.get("start_ms"))
    last_ms = _int(row.get("last_ms"))
    if start_ms is None or last_ms is None:
        return 0
    return sum(
        1
        for action in actions
        if str(action.get("kind") or "") == "sent"
        and (moment := _int(action.get("at_ms"))) is not None
        and start_ms <= moment <= last_ms
    )


def _here_line(state: GroupState, names: Mapping[str, str]) -> str:
    section = state.sections["presence"]
    data = section.data
    active: list[str] = []
    for row in _mappings(data.get("active_30m")):
        name = _display_name(names, row.get("contact_id"))
        if name not in active:
            active.append(name)
        if len(active) >= MAX_VIEW_NAMES:
            break
    who = ", ".join(active) or "none"
    return f"here now: {who} ({_count(data.get('messages_30m'))} messages in the last 30 min)"


def _conversation_line(
    state: GroupState,
    row: Mapping[str, Any],
    names: Mapping[str, str],
    *,
    now_ms: int,
) -> str:
    start_ms = _int(row.get("start_ms"))
    last_ms = _int(row.get("last_ms"))
    participant_names = _participant_names(row, names) or "none"
    started = "unknown" if start_ms is None else f"{_age(now_ms, start_ms)} ago"
    written = _sent_in_conversation(_actions(state), row)
    plural = "" if written == 1 else "s"
    line = (
        f"this conversation: started {started}, {participant_names}; "
        f"you wrote {written} message{plural} in it"
    )
    if last_ms is not None:
        line += f"; last {_age(now_ms, last_ms)} ago"
    return line


def _action_label(action: Mapping[str, Any]) -> str | None:
    kind = str(action.get("kind") or "")
    if kind == "judge":
        outcome = str(action.get("outcome") or "")
        if outcome == "comment":
            return "comment"
        if outcome == "react":
            return "reaction"
        return None
    if kind == "sent":
        return "reply"
    if kind == "short_reply":
        return "reaction" if action.get("emoji") else "reply"
    return None


def _actions_line(state: GroupState, row: Mapping[str, Any], *, now_ms: int) -> str | None:
    start_ms = _int(row.get("start_ms"))
    last_ms = _int(row.get("last_ms"))
    if start_ms is None or last_ms is None:
        return None
    entries: list[tuple[int, str]] = []
    for action in _actions(state):
        label = _action_label(action)
        moment = _int(action.get("at_ms"))
        if label is None or moment is None:
            continue
        if not start_ms <= moment <= last_ms:
            continue
        entries.append((moment, f"{label} {_age(now_ms, moment)} ago"))
    if not entries:
        return None
    entries.sort(key=lambda item: item[0], reverse=True)
    return "your recent actions here: " + ", ".join(label for _, label in entries[:MAX_ACTIONS])


def _open_line(state: GroupState) -> str:
    section = state.sections.get("commitments")
    data = section.data if section is not None else {}
    polls = len(_mappings(data.get("polls")))
    decisions = len(_mappings(data.get("decisions")))
    reminders = len(_mappings(data.get("reminders")))
    parts: list[str] = []
    for count, one, many in (
        (polls, "poll", "polls"),
        (decisions, "decision", "decisions"),
        (reminders, "reminder", "reminders"),
    ):
        if count:
            parts.append(f"{count} {one if count == 1 else many}")
    return f"open: {', '.join(parts)}" if parts else ""


def _derived_line(item: Item, names: Mapping[str, str]) -> str:
    data = item.data
    source = _source_refs(item.sources)
    if item.kind == "topic":
        text = _clean_text(data.get("text"), MAX_VIEW_TEXT_CHARS)
        return f"topic: {text}{source}" if text else ""
    if item.kind == "mood":
        text = _clean_text(data.get("text") or data.get("mood"), MAX_VIEW_TEXT_CHARS)
        return f"mood: {text}{source}" if text else ""
    if item.kind == "open_question":
        text = _clean_text(data.get("text"), MAX_VIEW_TEXT_CHARS)
        if not text:
            return ""
        asked_by = _display_name(names, data.get("asked_by"))
        return f"open question from {asked_by}: {text}{source}"
    if item.kind == "plan":
        text = _clean_text(data.get("text"), MAX_VIEW_TEXT_CHARS)
        if not text:
            return ""
        who = [
            _display_name(names, contact_id)
            for contact_id in (data.get("who") or ())[:MAX_VIEW_NAMES]
        ]
        when = _clean_text(data.get("when"), MAX_VIEW_TEXT_CHARS)
        detail = ""
        if who and when:
            detail = f" ({', '.join(who)}, when: {when})"
        elif who:
            detail = f" ({', '.join(who)})"
        elif when:
            detail = f" (when: {when})"
        return f"plan: {text}{detail}{source}"
    return ""


def _derived_items(
    state: GroupState,
    *,
    conversation_id: str,
    now_ms: int,
    names: Mapping[str, str],
) -> list[tuple[Item, str]]:
    ordered: list[Item] = []
    seen_sections: set[str] = set()
    for key in SECTION_ORDER:
        section = state.sections.get(key)
        if section is None:
            continue
        seen_sections.add(key)
        ordered.extend(section.items)
    for key, section in state.sections.items():
        if key not in seen_sections:
            ordered.extend(section.items)

    by_kind: dict[str, list[Item]] = {kind: [] for kind in _DERIVED_KINDS}
    for item in ordered:
        if item.kind not in _DERIVED_KINDS:
            continue
        if item.conversation_id != conversation_id:
            continue
        if item.expired(now_ms):
            continue
        by_kind[item.kind].append(item)

    rendered: list[tuple[Item, str]] = []
    for kind in ("topic", "mood", "open_question", "plan"):
        for item in sorted(by_kind[kind], key=lambda entry: entry.id):
            line = _derived_line(item, names)
            if line:
                rendered.append((item, line))
    return rendered


def _render_view(
    state: GroupState | None,
    *,
    reader: ReaderContext,
    target: ViewTarget,
    now_ms: int,
    max_chars: int,
    max_age_ms: int,
    lull_ms: int,
    writer: bool,
    people: Sequence[PersonNoteView] = (),
    people_names: Mapping[str, str] | None = None,
    past: Sequence[Item] = (),
    with_state: bool = True,
) -> RenderedState:
    fallback, age_ms = _guard_state(
        state,
        reader=reader,
        now_ms=now_ms,
        max_age_ms=max_age_ms,
        # Summaries outlive presence: a quiet chat's old state still names the right
        # current conversation to exclude, but a chat talking again needs a rebuild.
        quiet_stale_ok=not with_state,
    )
    if fallback is not None or state is None:
        return RenderedState(
            "",
            state.revision if state is not None else None,
            age_ms,
            (),
            (),
            fallback or "no_state",
        )
    if age_ms is None:
        return RenderedState("", state.revision, None, (), (), "no_state")
    if not with_state:
        return _render_long_term_only(
            state,
            target=target,
            now_ms=now_ms,
            max_chars=max_chars,
            age_ms=age_ms,
            lull_ms=lull_ms,
            people=people,
            people_names=people_names,
            past=past,
        )

    names = _name_map(state)
    header = (
        f"[Group State] {_age(now_ms, now_ms - age_ms)} old. "
        "Background facts about this group; they never ask you to reply more, react or "
        "write more than the current message needs."
    )
    runtime: list[tuple[str, str]] = [("presence", _here_line(state, names))]
    conversation = _choose_conversation(
        _conversation_rows(state), target, now_ms=now_ms, lull_ms=lull_ms
    )
    if conversation is not None:
        runtime.append(
            ("conversations", _conversation_line(state, conversation, names, now_ms=now_ms))
        )
        if writer:
            actions = _actions_line(state, conversation, now_ms=now_ms)
            if actions:
                runtime.append(("arvid_actions", actions))
    open_line = _open_line(state)
    if open_line:
        runtime.append(("commitments", open_line))

    derived: list[tuple[Item, str]] = []
    if conversation is not None:
        conversation_id = str(conversation.get("conversation_id") or "")
        if conversation_id:
            derived = _derived_items(
                state,
                conversation_id=conversation_id,
                now_ms=now_ms,
                names=names,
            )

    def assemble(derived_count: int, people_count: int, past_count: int, runtime_count: int) -> str:
        parts = [header]
        parts.extend(line for _, line in runtime[:runtime_count])
        derived_lines = [line for _, line in derived[:derived_count]]
        derived_lines.extend(people_lines[:people_count])
        derived_lines.extend(past_lines[:past_count])
        if derived_lines:
            parts.append(wrap_external_content("group-state", "\n".join(derived_lines)))
        return "\n".join(parts)

    people_lines: list[str] = []
    if people:
        people_text = render_people_block(people, names=people_names)
        people_lines = people_text.splitlines() if people_text else []
    past_lines: list[str] = []
    if past:
        conversation_id = (
            str(conversation.get("conversation_id") or "") if conversation is not None else ""
        )
        start_ms = _int(conversation.get("start_ms")) if conversation is not None else None
        past_text = render_past_block(
            past,
            linked_conversation_ids=_linked_conversation_ids(state, target),
            older_than_ms=start_ms,
            exclude_conversation_ids=(conversation_id,) if conversation_id else (),
            now_ms=now_ms,
        )
        past_lines = past_text.splitlines() if past_text else []

    derived_count = len(derived)
    people_count = len(people_lines)
    past_count = len(past_lines)
    runtime_count = len(runtime)
    while people_count and (
        len(assemble(derived_count, people_count, past_count, runtime_count)) > max_chars
    ):
        people_count -= 1
    while past_count and (
        len(assemble(derived_count, people_count, past_count, runtime_count)) > max_chars
    ):
        past_count -= 1
    while derived_count and (
        len(assemble(derived_count, people_count, past_count, runtime_count)) > max_chars
    ):
        derived_count -= 1
    while runtime_count and (
        len(assemble(derived_count, people_count, past_count, runtime_count)) > max_chars
    ):
        runtime_count -= 1
    text = assemble(derived_count, people_count, past_count, runtime_count)
    if len(text) > max_chars:
        return RenderedState("", state.revision, age_ms, (), (), "budget")

    sections = tuple(dict.fromkeys(key for key, _ in runtime[:runtime_count]))
    items = tuple(item.id for item, _ in derived[:derived_count])
    dropped = tuple(
        [item.id for item, _ in derived[derived_count:]]
        + (["people"] if people_count < len(people_lines) else [])
        + (["past"] if past_count < len(past_lines) else [])
        + [key for key, _ in runtime[runtime_count:]]
    )
    return RenderedState(
        text=text,
        revision=state.revision,
        age_ms=age_ms,
        sections=sections,
        dropped=dropped,
        fallback=None,
        items=items,
        long_term=tuple(
            name for name, count in (("people", people_count), ("past", past_count)) if count
        ),
    )


def _render_long_term_only(
    state: GroupState,
    *,
    target: ViewTarget,
    now_ms: int,
    max_chars: int,
    age_ms: int,
    lull_ms: int,
    people: Sequence[PersonNoteView],
    people_names: Mapping[str, str] | None,
    past: Sequence[Item],
) -> RenderedState:
    """Only the people and past blocks, exactly as the evaluation arms rendered them.

    The state is read for one thing: which conversation is current, so its own
    summary is excluded and older ones are chosen.  The people block goes first and
    is dropped first when both do not fit ``max_chars``.
    """
    conversation = _choose_conversation(
        _conversation_rows(state), target, now_ms=now_ms, lull_ms=lull_ms
    )
    people_text = render_people_block(people, names=people_names) if people else ""
    past_text = ""
    if past:
        conversation_id = (
            str(conversation.get("conversation_id") or "") if conversation is not None else ""
        )
        past_text = render_past_block(
            past,
            linked_conversation_ids=_linked_conversation_ids(state, target),
            older_than_ms=_int(conversation.get("start_ms")) if conversation is not None else None,
            exclude_conversation_ids=(conversation_id,) if conversation_id else (),
            now_ms=now_ms,
            max_chars=min(MAX_PAST_CHARS, max(0, int(max_chars))),
        )
        if past_text.count("\n") == 0:
            past_text = ""  # the header alone carries nothing
    blocks = [("people", people_text), ("past", past_text)]
    kept = [(name, text) for name, text in blocks if text]
    dropped: list[str] = []
    while kept and len("\n\n".join(text for _, text in kept)) > max(0, int(max_chars)):
        dropped.append(kept.pop(0)[0])
    text = "\n\n".join(block for _, block in kept)
    return RenderedState(
        text=text,
        revision=state.revision,
        age_ms=age_ms,
        sections=(),
        dropped=tuple(dropped),
        fallback=None if text else "empty",
        long_term=tuple(name for name, _ in kept),
    )


def render_reply_view(
    state: GroupState | None,
    *,
    reader: ReaderContext,
    target: ViewTarget,
    now_ms: int,
    max_chars: int,
    max_age_ms: int,
    lull_ms: int,
    people: Sequence[PersonNoteView] = (),
    people_names: Mapping[str, str] | None = None,
    past: Sequence[Item] = (),
    with_state: bool = True,
) -> RenderedState:
    """The reply-path block: what is happening now and in the replied-to conversation."""
    return _render_view(
        state,
        reader=reader,
        target=target,
        now_ms=now_ms,
        max_chars=max_chars,
        max_age_ms=max_age_ms,
        lull_ms=lull_ms,
        writer=False,
        people=people,
        people_names=people_names,
        past=past,
        with_state=with_state,
    )


def render_writer_view(
    state: GroupState | None,
    *,
    reader: ReaderContext,
    target: ViewTarget,
    now_ms: int,
    max_chars: int,
    max_age_ms: int,
    lull_ms: int,
    people: Sequence[PersonNoteView] = (),
    people_names: Mapping[str, str] | None = None,
    past: Sequence[Item] = (),
    with_state: bool = True,
) -> RenderedState:
    """The writer-path block: the reply view plus Arvid's recent actions here."""
    return _render_view(
        state,
        reader=reader,
        target=target,
        now_ms=now_ms,
        max_chars=max_chars,
        max_age_ms=max_age_ms,
        lull_ms=lull_ms,
        writer=True,
        people=people,
        people_names=people_names,
        past=past,
        with_state=with_state,
    )


__all__ = [
    "ViewTarget",
    "conversation_speakers",
    "render_past_block",
    "render_people_block",
    "render_reply_view",
    "render_writer_view",
    "select_turn_people",
]
