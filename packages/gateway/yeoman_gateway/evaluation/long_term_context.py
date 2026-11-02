"""As-of long-term context blocks for the offline reply-state evaluation.

Nothing here opens a database: the caller passes a bound ``HistoryQueries`` and a
Knowledge service.  Every input is cut at a deterministic instant before the item, and
the model answers are cached by input hash in the run directory.  Message text only
travels inside the untrusted envelopes built by the existing prompt builders.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any

from loguru import logger

from yeoman_gateway.group_state.models import Item
from yeoman_gateway.group_state.sections._common import names_for
from yeoman_gateway.group_state.sections.past import closed_week_bounds
from yeoman_gateway.group_state.summaries.parse import SummaryParseError, parse_summary_lines
from yeoman_gateway.group_state.summaries.prompt import (
    MAX_CONVERSATION_LINES,
    MAX_WEEK_LINES,
    build_conversation_input,
    build_week_input,
)
from yeoman_gateway.group_state.views import (
    ViewTarget,
    render_past_block,
    render_people_block,
    select_turn_people,
)
from yeoman_gateway.history.conversations import ConversationBounds, read_conversations
from yeoman_gateway.knowledge.models import (
    PersonNoteLine,
    PersonNoteView,
    RecallQuery,
)
from yeoman_gateway.knowledge.read_context import build_group_read_context
from yeoman_gateway.processing.person_notes import _statement_for_line
from yeoman_gateway.processing.person_notes_prompt import (
    MAX_LINES_PER_NOTE,
    MAX_OBSERVED_LINES,
    build_observed_input,
    clean_text,
    observed_messages,
    parse_observed_lines,
)

DAY_MS = 86_400_000
PEOPLE_ROUTE = "knowledge.notes"
SUMMARIES_ROUTE = "history.summaries"
MAX_OWN_MESSAGES = 300
MAX_PAST_CANDIDATES = 5
CACHE_FILE = "long-term-cache.jsonl"
TIMEOUT_MS = 30_000
MAX_OUTPUT_TOKENS = 900


def last_daily_cut_ms(at_ms: int, tz: tzinfo | None = None) -> int:
    """Local midnight of the last daily refresh strictly before ``at_ms``."""
    moment = datetime.fromtimestamp(int(at_ms) / 1000, tz)
    if tz is None:
        moment = moment.astimezone()
    start = datetime(moment.year, moment.month, moment.day, tzinfo=moment.tzinfo)
    start_ms = int(start.timestamp() * 1000)
    if start_ms >= int(at_ms):
        start -= timedelta(days=1)
        start_ms = int(start.timestamp() * 1000)
    return start_ms


def tick_ms(settings: Any) -> int:
    """The group-state tick length; a summary needs one full tick of age."""
    return max(1, int(getattr(settings, "min_interval_seconds", 30))) * 1000


def _hash(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class AsOfCache:
    """Input-hash cache of model answers in one run directory."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._entries: dict[str, dict[str, Any]] = {}
        if self.path.is_file():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, Mapping) and isinstance(entry.get("input_hash"), str):
                    self._entries[str(entry["input_hash"])] = dict(entry)

    def get(self, input_hash: str) -> Mapping[str, Any] | None:
        return self._entries.get(str(input_hash))

    def put(self, input_hash: str, value: Mapping[str, Any]) -> None:
        entry = {"input_hash": str(input_hash), **dict(value)}
        self._entries[str(input_hash)] = entry
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")


async def _ask(
    client: Any | None, messages: Sequence[Mapping[str, str]], *, max_tokens: int
) -> tuple[str, dict[str, int]]:
    if client is None:
        return "", {}
    try:
        reply = await client.chat_with_usage(
            [dict(message) for message in messages],
            max_tokens=int(max_tokens),
            response_format={"type": "json_object"},
            timeout_ms=TIMEOUT_MS,
        )
    except Exception as exc:  # noqa: BLE001 - one failed call is a missing line, not a crash
        logger.warning("reply_state_asof_call_failed error_type={}", type(exc).__name__)
        return "", {}
    usage = getattr(reply, "usage", None) or {}
    return str(getattr(reply, "content", "") or ""), {
        str(key): int(value)
        for key, value in usage.items()
        if isinstance(value, int) and not isinstance(value, bool)
    }


def _principal_of(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.startswith("whatsapp:"):
        return text
    if text.endswith("@s.whatsapp.net") or text.endswith("@c.us"):
        digits = text.split("@", 1)[0]
        return f"whatsapp:{digits}" if digits else None
    if text.endswith("@lid") or text.isdigit():
        return f"whatsapp:{text}"
    return None


def _person_id(knowledge: Any, value: str) -> str | None:
    principal = _principal_of(value)
    if principal:
        found = knowledge.person_for_principal(principal)
        if found:
            return str(found)
    try:
        return str(knowledge.canonical_id(str(value)))
    except Exception:  # noqa: BLE001 - unresolved is not a person
        return None


def _asof_stated_lines(
    knowledge: Any, person_id: str, *, context: Any, cut_ms: int
) -> list[tuple[PersonNoteLine, int]]:
    recalled = knowledge.recall(
        RecallQuery(
            person_ids=(str(person_id),),
            limit=max(MAX_LINES_PER_NOTE * 2, MAX_LINES_PER_NOTE),
            as_of_ms=int(cut_ms),
        ),
        context=context,
        group_wide=True,
    )
    rows: list[tuple[PersonNoteLine, int]] = []
    for statement_id in recalled.statement_ids:
        if len(rows) >= MAX_LINES_PER_NOTE:
            break
        speaker, content, updated_ms = _statement_for_line(knowledge, str(statement_id))
        if not speaker:
            continue
        author = clean_text(knowledge.display_name(speaker, context=context, for_group=True), 60)
        if not author or not any(char.isalpha() for char in author):
            continue
        text = clean_text(content)
        if not text:
            continue
        rows.append(
            (
                PersonNoteLine(
                    kind="stated",
                    text=f"{author}: {text}",
                    author=str(author),
                    refs=(str(statement_id),),
                ),
                int(updated_ms or 0),
            )
        )
    return rows


def _own_messages(
    queries: Any, *, chat_id: str, person_id: str, cut_ms: int
) -> list[tuple[str, str]]:
    rows = queries.recent(chat_id=str(chat_id), limit=MAX_OWN_MESSAGES, until_ms=int(cut_ms))
    own: list[tuple[str, str]] = []
    for row in rows:
        if str(row.get("direction")) != "in":
            continue
        contact = row.get("sender_contact_id")
        if not contact:
            continue
        try:
            terminal = queries.terminal(str(contact))
        except Exception:  # noqa: BLE001 - one broken row is skipped
            terminal = None
        if terminal != str(person_id):
            continue
        own.append((str(row["message_id"]), str(row.get("text") or "")))
    return own


def _note_from_payload(
    payload: Mapping[str, Any],
    *,
    channel: str,
    chat_id: str,
    person_id: str,
    cut_ms: int,
    input_hash: str,
) -> PersonNoteView:
    lines = tuple(
        PersonNoteLine(
            kind=str(item.get("kind") or "stated"),
            text=str(item.get("text") or ""),
            author=item.get("author"),
            refs=tuple(str(ref) for ref in item.get("refs") or ()),
        )
        for item in payload.get("lines") or ()
        if isinstance(item, Mapping)
    )
    return PersonNoteView(
        note_id=f"asof:{person_id}:{cut_ms}",
        channel=str(channel),
        chat_id=str(chat_id),
        person_id=str(person_id),
        status="active",
        input_hash=str(input_hash),
        built_ms=int(cut_ms),
        input_cut_ms=int(cut_ms),
        lines=lines,
    )


async def build_people_block(
    *,
    knowledge: Any | None,
    queries: Any,
    channel: str,
    chat_id: str,
    at_ms: int,
    target: ViewTarget,
    conversation_speakers: Sequence[str] = (),
    client: Any | None = None,
    cache: AsOfCache,
    policy_revision: int = 0,
    max_chars: int = 700,
    timings: list[float] | None = None,
) -> str:
    """The ``[People here]`` block built only from inputs at or before the daily cut."""
    if knowledge is None:
        return ""
    cut_ms = last_daily_cut_ms(int(at_ms))
    audience = queries.members(chat_id=str(chat_id), at_ms=int(cut_ms))
    if audience.status != "known" or not audience.members:
        return ""
    values = select_turn_people(target, conversation_speakers)
    if not values:
        return ""
    principal = str(getattr(target, "sender_principal", "") or "")
    if principal not in audience.members:
        principal = sorted(audience.members)[0]
    context = build_group_read_context(
        queries=queries,
        channel=str(channel),
        chat_id=str(chat_id),
        principal=principal,
        is_owner=False,
        at_ms=int(cut_ms),
        policy_revision=int(policy_revision),
        purpose="reply",
    )
    if context is None:
        return ""
    person_ids = [
        person_id for value in values if (person_id := _person_id(knowledge, value)) is not None
    ]
    person_ids = list(dict.fromkeys(person_ids))[:4]
    if not person_ids:
        return ""

    inputs: list[dict[str, Any]] = []
    prepared: list[tuple[str, str, list[tuple[PersonNoteLine, int]], Any]] = []
    for person_id in person_ids:
        name = clean_text(knowledge.display_name(person_id, context=context, for_group=True), 60)
        if not name or not any(char.isalpha() for char in name):
            continue
        stated = _asof_stated_lines(knowledge, person_id, context=context, cut_ms=cut_ms)
        own = _own_messages(queries, chat_id=str(chat_id), person_id=person_id, cut_ms=cut_ms)
        observed = build_observed_input(name, own)
        fingerprints = [
            [message.message_id, queries.content_fingerprint(message.message_id)]
            for message in observed.messages
        ]
        inputs.append(
            {
                "person": person_id,
                "name": name,
                "cut": int(cut_ms),
                "stated": [[line.refs[0], revision] for line, revision in stated],
                "observed": fingerprints,
            }
        )
        prepared.append((person_id, name, stated, observed))
    if not prepared:
        return ""
    input_hash = _hash({"kind": "people", "inputs": inputs})
    cached = cache.get(input_hash)
    if cached is not None and isinstance(cached.get("notes"), list):
        notes = tuple(
            _note_from_payload(
                payload,
                channel=channel,
                chat_id=chat_id,
                person_id=str(payload.get("person_id") or ""),
                cut_ms=cut_ms,
                input_hash=input_hash,
            )
            for payload in cached["notes"]
            if isinstance(payload, Mapping)
        )
        names = {
            str(payload.get("person_id")): str(payload.get("name") or "")
            for payload in cached["notes"]
            if isinstance(payload, Mapping)
        }
        started = time.perf_counter()
        block = render_people_block(notes, names=names, max_chars=max_chars)
        if timings is not None:
            timings.append((time.perf_counter() - started) * 1000)
        return block

    notes: list[PersonNoteView] = []
    names: dict[str, str] = {}
    cacheable = True
    for person_id, name, stated, observed in prepared:
        lines: list[PersonNoteLine] = [line for line, _ in stated]
        if observed.messages:
            raw, _usage = await _ask(
                client, observed_messages(observed.payload, name), max_tokens=MAX_OUTPUT_TOKENS
            )
            if not raw:
                cacheable = False
            if raw:
                try:
                    parsed = parse_observed_lines(
                        raw,
                        observed=observed,
                        member_names=(),
                        outsider_names=(),
                        max_lines=MAX_OBSERVED_LINES,
                    )
                except ValueError:
                    parsed = None
                if parsed is not None:
                    lines.extend(parsed.lines)
                else:
                    cacheable = False
        lines = lines[:MAX_LINES_PER_NOTE]
        if not lines:
            continue
        names[person_id] = name
        notes.append(
            PersonNoteView(
                note_id=f"asof:{person_id}:{cut_ms}",
                channel=str(channel),
                chat_id=str(chat_id),
                person_id=person_id,
                status="active",
                input_hash=input_hash,
                built_ms=int(cut_ms),
                input_cut_ms=int(cut_ms),
                lines=tuple(lines),
            )
        )
    if not notes:
        return ""
    started = time.perf_counter()
    block = render_people_block(tuple(notes), names=names, max_chars=max_chars)
    if timings is not None:
        timings.append((time.perf_counter() - started) * 1000)
    if cacheable:
        cache.put(
            input_hash,
            {
                "notes": [
                    {
                        "person_id": note.person_id,
                        "name": names.get(note.person_id, ""),
                        "lines": [
                            {
                                "kind": line.kind,
                                "text": line.text,
                                "author": line.author,
                                "refs": list(line.refs),
                            }
                            for line in note.lines
                        ],
                    }
                    for note in notes
                ]
            },
        )
    return block


def _conversation_bounds(settings: Any, consciousness: Any | None) -> ConversationBounds:
    return ConversationBounds(
        window_ms=max(1, int(getattr(settings, "window_hours", 24))) * 3_600_000,
        max_rows=500,
        lull_ms=max(1, int(getattr(consciousness, "lull_silence_minutes", 25))) * 60_000,
        burst_n=max(1, int(getattr(consciousness, "burst_threshold_messages", 8))),
        burst_ms=max(1, int(getattr(consciousness, "burst_window_minutes", 15))) * 60_000,
    )


def _week_label(ended_ms: int, tz: tzinfo | None) -> str:
    moment = datetime.fromtimestamp(int(ended_ms) / 1000, tz)
    if tz is None:
        moment = moment.astimezone()
    year, week, _ = moment.isocalendar()
    return f"{year}-W{week:02d}"


def _conversation_item(conversation: Any, lines: Sequence[str], *, now_ms: int) -> Item:
    return Item(
        id=f"{conversation.id}:summary",
        kind="conversation_summary",
        data={
            "conversation_id": conversation.id,
            "started_ms": conversation.start_ms,
            "ended_ms": conversation.last_ms,
            "participants": [],
            "lines": [str(line) for line in lines],
        },
        sources=tuple(conversation.message_ids),
        source_fingerprints={},
        produced_at_ms=int(now_ms),
        ttl_ms=7 * DAY_MS,
        trust="derived",
        conversation_id=conversation.id,
    )


def _week_item(iso_week: str, lines: Sequence[str], *, now_ms: int) -> Item:
    return Item(
        id=f"week:{iso_week}",
        kind="week_summary",
        data={"iso_week": iso_week, "lines": [str(line) for line in lines], "depends_on": []},
        sources=(),
        source_fingerprints={},
        produced_at_ms=int(now_ms),
        ttl_ms=7 * DAY_MS,
        trust="derived",
    )


async def build_past_block(
    *,
    queries: Any,
    chat_id: str,
    at_ms: int,
    settings: Any,
    client: Any | None = None,
    cache: AsOfCache,
    consciousness: Any | None = None,
    linked_conversation_ids: Sequence[str] = (),
    current_conversation_id: str | None = None,
    current_start_ms: int | None = None,
    max_chars: int = 600,
    timings: list[float] | None = None,
) -> str:
    """The ``[Earlier in this group]`` block from ended conversations and closed weeks."""
    tick = tick_ms(settings)
    bounds = _conversation_bounds(settings, consciousness)
    conversations = read_conversations(queries, str(chat_id), int(at_ms), bounds)
    return await build_past_block_for_conversations(
        queries=queries,
        at_ms=at_ms,
        settings=settings,
        conversations=conversations,
        client=client,
        cache=cache,
        linked_conversation_ids=linked_conversation_ids,
        current_conversation_id=current_conversation_id,
        current_start_ms=current_start_ms,
        max_chars=max_chars,
        tick=tick,
        timings=timings,
    )


async def build_past_block_for_conversations(
    *,
    queries: Any,
    at_ms: int,
    settings: Any,
    conversations: Sequence[Any],
    client: Any | None,
    cache: AsOfCache,
    linked_conversation_ids: Sequence[str] = (),
    current_conversation_id: str | None = None,
    current_start_ms: int | None = None,
    max_chars: int = 600,
    tick: int | None = None,
    timings: list[float] | None = None,
) -> str:
    """Build the block from an explicit chat conversation list (one chat only)."""
    tick = tick_ms(settings) if tick is None else tick
    cut_ms = int(at_ms) - max(1, int(tick))
    ended = [
        conversation
        for conversation in conversations
        if conversation.state == "ended"
        and conversation.message_count >= max(1, int(getattr(settings, "min_messages", 4)))
        and conversation.last_ms <= cut_ms
    ]
    linked = set(str(value) for value in linked_conversation_ids if str(value))
    ended.sort(key=lambda conversation: (conversation.last_ms, conversation.id), reverse=True)
    selected = [conversation for conversation in ended if conversation.id in linked]
    for conversation in ended:
        if len(selected) >= len(linked) + MAX_PAST_CANDIDATES:
            break
        if conversation.id not in {item.id for item in selected}:
            selected.append(conversation)
    if not selected:
        return ""

    items: list[Item] = []
    for conversation in selected:
        input_hash, summary_input = _conversation_summary_input(queries, conversation, settings)
        if summary_input is None:
            continue
        cached = cache.get(input_hash)
        lines: list[str] = []
        if cached is not None and isinstance(cached.get("lines"), list):
            lines = [str(line) for line in cached["lines"] if str(line)]
        else:
            raw, _usage = await _ask(client, summary_input.messages, max_tokens=MAX_OUTPUT_TOKENS)
            if raw:
                try:
                    parsed = parse_summary_lines(
                        raw,
                        ref_map=summary_input.ref_map,
                        max_lines=MAX_CONVERSATION_LINES,
                        member_names=summary_input.member_names,
                        outsider_names=summary_input.outsider_names,
                    )
                except SummaryParseError:
                    parsed = None
                if parsed is not None:
                    lines = [line.text for line in parsed.lines]
            if lines:
                cache.put(input_hash, {"lines": lines})
        if lines:
            items.append(_conversation_item(conversation, lines, now_ms=int(at_ms)))

    week_item = await _week_summary_item(
        queries=queries,
        at_ms=int(at_ms),
        settings=settings,
        items=items,
        client=client,
        cache=cache,
    )
    started = time.perf_counter()
    block = render_past_block(
        (*items, *((week_item,) if week_item is not None else ())),
        linked_conversation_ids=tuple(linked),
        older_than_ms=current_start_ms,
        exclude_conversation_ids=(current_conversation_id,) if current_conversation_id else (),
        now_ms=int(at_ms),
        max_chars=max_chars,
    )
    if timings is not None:
        timings.append((time.perf_counter() - started) * 1000)
    return block


def _conversation_summary_input(queries: Any, conversation: Any, settings: Any) -> tuple[str, Any]:
    rows = queries.recent(
        chat_id=str(conversation.chat_id),
        limit=500,
        after_ms=int(conversation.start_ms) - 1,
        until_ms=int(conversation.last_ms),
    )
    contacts = {
        str(row.get("sender_contact_id") or "") for row in rows if row.get("direction") != "out"
    }
    names = names_for(queries, (contact for contact in contacts if contact))
    summary_input = build_conversation_input(
        rows,
        conversation_id=str(conversation.id),
        message_ids=conversation.message_ids,
        fingerprint_of=queries.content_fingerprint,
        names=names,
        assistant_ids=queries.assistant_contact_ids(),
        max_chars=int(getattr(settings, "max_input_chars", 12_000)),
        min_messages=max(1, int(getattr(settings, "min_messages", 4))),
        member_names=tuple(sorted(set(names.values()), key=str.casefold)),
    )
    return (summary_input.input_hash if summary_input is not None else ""), summary_input


async def _week_summary_item(
    *,
    queries: Any,
    at_ms: int,
    settings: Any,
    items: Sequence[Item],
    client: Any | None,
    cache: AsOfCache,
) -> Item | None:
    if not items:
        return None
    keep_weeks = max(1, int(getattr(settings, "keep_weeks", 12)))
    weeks = closed_week_bounds(int(at_ms), keep_weeks=keep_weeks)
    by_week: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        ended_ms = item.data.get("ended_ms")
        if not isinstance(ended_ms, int):
            continue
        label = _week_label(ended_ms, None)
        by_week.setdefault(label, []).append(
            {
                "conversation_id": str(item.data.get("conversation_id") or ""),
                "lines": [str(line) for line in item.data.get("lines") or ()],
                "participants": [],
            }
        )
    chosen: tuple[str, list[dict[str, Any]]] | None = None
    for label, _start_ms, _end_ms in reversed(weeks):
        if label in by_week:
            chosen = (label, by_week[label])
            break
    if chosen is None:
        return None
    label, conversations = chosen
    week_input = build_week_input(
        label,
        conversations,
        max_chars=int(getattr(settings, "max_input_chars", 12_000)),
    )
    if week_input is None:
        return None
    cached = cache.get(week_input.input_hash)
    lines: list[str] = []
    if cached is not None and isinstance(cached.get("lines"), list):
        lines = [str(line) for line in cached["lines"] if str(line)]
    else:
        raw, _usage = await _ask(client, week_input.messages, max_tokens=MAX_OUTPUT_TOKENS)
        if raw:
            try:
                parsed = parse_summary_lines(
                    raw,
                    ref_map=week_input.ref_map,
                    max_lines=MAX_WEEK_LINES,
                    member_names=week_input.member_names,
                    outsider_names=week_input.outsider_names,
                )
            except SummaryParseError:
                parsed = None
            if parsed is not None:
                lines = [line.text for line in parsed.lines]
        if lines:
            cache.put(week_input.input_hash, {"lines": lines})
    return _week_item(label, lines, now_ms=int(at_ms)) if lines else None


__all__ = [
    "AsOfCache",
    "CACHE_FILE",
    "PEOPLE_ROUTE",
    "SUMMARIES_ROUTE",
    "build_past_block",
    "build_past_block_for_conversations",
    "build_people_block",
    "last_daily_cut_ms",
    "tick_ms",
]
