"""Weekly group reaction signals: deterministic, read-only, no model (Spec B §4).

Three signals for one group chat over one ISO week, computed from history alone:

* **approval** - what people reacted to, ranked by distinct reactors; negative emojis are
  kept and counted separately;
* **engagement** - the week's conversations (Spec A :func:`segment` with the live
  lull/burst settings), ranked by distinct senders x duration; never read as approval;
* **Arvid's effect** - for each of Arvid's messages: did others write within the lull,
  reply, react, and did the conversation continue or end.

Nothing is stored here. :meth:`GroupSignals.aggregates` carries counts and descriptors
only, never message text; excerpts exist solely for the one weekly model call.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo
from typing import Any

from .conversations import Conversation, registry_ids, segment
from .queries import HistoryQueries

#: Kept and counted separately; a negative reaction is not approval.
NEGATIVE_EMOJIS: frozenset[str] = frozenset({"😡", "🙄", "👎", "🤮"})
#: Minimum signal for a weekly note (spec §5.3, owner item O2).
MIN_REACTIONS = 20
MIN_CONVERSATIONS = 5
#: Excerpt length handed to the model, per message.
EXCERPT_CHARS = 200
#: Selection sizes for the model input (spec §5.2).
TOP_MESSAGES = 15
TOP_CONVERSATIONS = 8
OPENING_MESSAGES = 3
ARVID_EXAMPLES = 5
#: Upper bound of rows one weekly read may cover; the largest group writes ~1,300 a week.
MAX_WEEK_ROWS = 20_000

_PAGE = 500
_REACTION_BATCH = 100
_LINK = re.compile(r"https?://|\bwww\.", re.IGNORECASE)
_WEEK = re.compile(r"^(\d{4})-W(\d{2})$")
_MEDIA_KINDS = {
    "image": "image", "video": "video", "audio": "voice", "sticker": "sticker",
    "document": "document", "poll": "poll",
}
#: Arvid's own messages share one sender key, like in :mod:`.conversations`.
_ARVID = "\x00arvid"


# ── weeks ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class DigestWeek:
    """One ISO week in local time: ``[start_ms, end_ms)``."""

    label: str
    start_ms: int
    end_ms: int


def _local_ms(moment: datetime, tz: tzinfo | None) -> int:
    aware = moment.replace(tzinfo=tz) if tz is not None else moment.astimezone()
    return int(aware.timestamp() * 1000)


def _local_datetime(ms: int, tz: tzinfo | None) -> datetime:
    moment = datetime.fromtimestamp(int(ms) / 1000, tz)
    return moment if tz is not None else moment.astimezone()


def iso_week(label: str, *, tz: tzinfo | None = None) -> DigestWeek:
    """The local-time window of an ISO week label such as ``2026-W42``."""
    year, week = _week_parts(label)
    try:
        monday = date.fromisocalendar(year, week, 1)
    except ValueError as exc:
        raise ValueError("no such ISO week") from exc
    start = datetime(monday.year, monday.month, monday.day)
    end = start + timedelta(days=7)
    return DigestWeek(f"{year:04d}-W{week:02d}", _local_ms(start, tz), _local_ms(end, tz))


def week_moment_ms(week: str, *, hours: int, tz: tzinfo | None = None) -> int:
    """Local wall time ``hours`` after the Monday 00:00 that starts ``week``."""
    start = datetime.fromisocalendar(*_week_parts(week), 1)
    return _local_ms(start + timedelta(hours=int(hours)), tz)


def _week_parts(label: str) -> tuple[int, int]:
    match = _WEEK.match(str(label).strip())
    if match is None:
        raise ValueError("week must look like YYYY-Www")
    return int(match.group(1)), int(match.group(2))


def week_of(moment_ms: int, *, tz: tzinfo | None = None) -> DigestWeek:
    """The ISO week (local time) that contains ``moment_ms``."""
    year, week, _ = _local_datetime(moment_ms, tz).isocalendar()
    return iso_week(f"{year:04d}-W{week:02d}", tz=tz)


def previous_week(moment_ms: int, *, tz: tzinfo | None = None) -> DigestWeek:
    """The last complete ISO week before the one that contains ``moment_ms``."""
    current = week_of(moment_ms, tz=tz)
    return week_of(current.start_ms - 1, tz=tz)


# ── signals ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ReactedMessage:
    """One reacted message: descriptors for aggregates, an excerpt for the model."""

    message_id: str
    sent_ms: int
    kind: str
    length: str
    is_reply: bool
    time_of_day: str
    from_arvid: bool
    reactions: int
    reactors: int
    emojis: tuple[str, ...]
    negative: int
    excerpt: str

    def descriptor(self) -> dict[str, object]:
        return {
            "kind": self.kind, "length": self.length, "reply": self.is_reply,
            "time_of_day": self.time_of_day, "from_arvid": self.from_arvid,
            "reactions": self.reactions, "reactors": self.reactors,
            "emojis": list(self.emojis), "negative": self.negative,
        }


@dataclass(frozen=True, slots=True)
class ConversationSignal:
    """One conversation of the week: engagement, never approval."""

    conversation_id: str
    start_ms: int
    duration_ms: int
    senders: int
    messages: int
    reactions: int
    burst: bool
    arvid_messages: int
    arvid_addressed: bool
    #: ``(message_id, excerpt)`` of the first messages, oldest first.
    opening: tuple[tuple[str, str], ...]

    @property
    def score(self) -> int:
        return self.senders * self.duration_ms

    def descriptor(self) -> dict[str, object]:
        return {
            "duration_minutes": self.duration_ms // 60_000, "senders": self.senders,
            "messages": self.messages, "reactions": self.reactions, "burst": self.burst,
            "arvid_messages": self.arvid_messages, "arvid_addressed": self.arvid_addressed,
        }


@dataclass(frozen=True, slots=True)
class ArvidMessage:
    """What happened after one of Arvid's messages."""

    message_id: str
    sent_ms: int
    followed: bool
    replied: bool
    reacted: bool
    continued: bool
    others_after: int
    messages_after: int
    excerpt: str

    @property
    def answered(self) -> bool:
        return self.followed or self.replied or self.reacted


@dataclass(frozen=True, slots=True)
class ArvidEffect:
    messages: int = 0
    followed: int = 0
    replied: int = 0
    reacted: int = 0
    continued: int = 0
    ended: int = 0
    addressed_conversations: int = 0
    addressed_with_others: int = 0
    others_after_addressed: int = 0
    answered_and_continued: tuple[ArvidMessage, ...] = ()
    ended_conversation: tuple[ArvidMessage, ...] = ()

    def counts(self) -> dict[str, int]:
        return {
            "messages": self.messages, "followed_within_lull": self.followed,
            "replied": self.replied, "reacted": self.reacted,
            "conversation_continued": self.continued, "conversation_ended": self.ended,
            "addressed_conversations": self.addressed_conversations,
            "addressed_with_others": self.addressed_with_others,
            "others_after_addressed": self.others_after_addressed,
        }


@dataclass(frozen=True, slots=True)
class GroupSignals:
    """The week's signals for one group. Excerpts never leave the model call."""

    chat_id: str
    week: DigestWeek
    messages: int
    senders: int
    arvid_messages: int
    reactions: int
    reacted_messages: int
    reactors: int
    negative_reactions: int
    emoji_counts: tuple[tuple[str, int], ...]
    conversations: int
    engaging_conversations: int
    #: Every reacted message, best first.
    approval: tuple[ReactedMessage, ...]
    #: Every conversation, most engaging first.
    engagement: tuple[ConversationSignal, ...]
    arvid: ArvidEffect
    truncated: bool = False
    min_reactions: int = MIN_REACTIONS
    min_conversations: int = MIN_CONVERSATIONS

    @property
    def meets_minimum(self) -> bool:
        return self.reactions >= self.min_reactions and (
            self.conversations >= self.min_conversations
        )

    def aggregates(self) -> dict[str, object]:
        """Counts and descriptors only: no message text, no ids, no senders."""
        return {
            "week": self.week.label,
            "window_start_ms": self.week.start_ms,
            "window_end_ms": self.week.end_ms,
            "messages": self.messages,
            "senders": self.senders,
            "arvid_messages": self.arvid_messages,
            "reactions": self.reactions,
            "reacted_messages": self.reacted_messages,
            "reactors": self.reactors,
            "negative_reactions": self.negative_reactions,
            "top_emojis": [[emoji, count] for emoji, count in self.emoji_counts[:8]],
            "reacted_by_kind": dict(sorted(Counter(item.kind for item in self.approval).items())),
            "top_reacted": [item.descriptor() for item in self.approval[:TOP_MESSAGES]],
            "conversations": self.conversations,
            "engaging_conversations": self.engaging_conversations,
            "top_conversations": [
                item.descriptor() for item in self.engagement[:TOP_CONVERSATIONS]
            ],
            "arvid": self.arvid.counts(),
            "minimum": {
                "reactions": self.min_reactions,
                "conversations": self.min_conversations,
                "met": self.meets_minimum,
            },
            "truncated": self.truncated,
        }


def compute_signals(
    rows: Iterable[Mapping[str, Any]],
    reactions: Iterable[Mapping[str, Any]],
    *,
    chat_id: str,
    week: DigestWeek,
    lull_ms: int,
    burst_n: int,
    burst_ms: int,
    registry: Iterable[str] = (),
    tz: tzinfo | None = None,
    truncated: bool = False,
    min_reactions: int = MIN_REACTIONS,
    min_conversations: int = MIN_CONVERSATIONS,
) -> GroupSignals:
    """The three signals of one chat for one week; pure (rows in, signals out).

    Rows outside ``[week.start_ms, week.end_ms)``, of another chat or without a time
    are ignored. Reactions follow the segmentation rule: the latest event per message
    and actor counts, and a removed reaction does not.
    """
    timed = [
        row for row in rows
        if isinstance(row.get("sent_ms"), int)
        and week.start_ms <= int(row["sent_ms"]) < week.end_ms
        and str(row.get("chat_id") or "") == str(chat_id)
    ]
    timed.sort(key=lambda row: (int(row["sent_ms"]), str(row.get("message_id") or "")))
    reaction_list = [
        event for event in reactions
        if not isinstance(event.get("occurred_ms"), int) or int(event["occurred_ms"]) < week.end_ms
    ]
    current = _current_reactions(reaction_list)
    registry_set = frozenset(str(item) for item in registry if str(item))
    conversations = segment(
        timed, reaction_list, lull_ms=lull_ms, burst_n=burst_n, burst_ms=burst_ms,
        now_ms=week.end_ms, registry_ids=registry_set,
    ) if timed else []

    by_id = {str(row.get("message_id") or ""): row for row in timed}
    approval = _approval(timed, current, tz=tz)
    engagement = tuple(sorted(
        (_conversation_signal(item, by_id) for item in conversations),
        key=lambda item: (-item.score, -item.messages, item.start_ms, item.conversation_id),
    ))
    emoji_counter: Counter[str] = Counter(
        emoji for message_id in by_id for _actor, emoji in current.get(message_id, ())
    )
    reactors = {
        actor for message_id in by_id for actor, _emoji in current.get(message_id, ()) if actor
    }
    return GroupSignals(
        chat_id=str(chat_id),
        week=week,
        messages=len(timed),
        senders=len({_sender_key(row) for row in timed} - {""}),
        arvid_messages=sum(1 for row in timed if row.get("direction") == "out"),
        reactions=sum(emoji_counter.values()),
        reacted_messages=len(approval),
        reactors=len(reactors),
        negative_reactions=sum(emoji_counter[emoji] for emoji in NEGATIVE_EMOJIS),
        emoji_counts=tuple(sorted(emoji_counter.items(), key=lambda item: (-item[1], item[0]))),
        conversations=len(conversations),
        engaging_conversations=sum(1 for item in conversations if item.burst),
        approval=approval,
        engagement=engagement,
        arvid=_arvid_effect(timed, conversations, current, lull_ms=lull_ms, registry=registry_set),
        truncated=bool(truncated),
        min_reactions=int(min_reactions),
        min_conversations=int(min_conversations),
    )


def read_group_signals(
    queries: HistoryQueries,
    chat_id: str,
    week: DigestWeek,
    *,
    lull_ms: int,
    burst_n: int,
    burst_ms: int,
    thread_lookup: Callable[[str], object] | None = None,
    tz: tzinfo | None = None,
    max_rows: int = MAX_WEEK_ROWS,
) -> GroupSignals:
    """Read one chat's week from history (inside the caller's history turn) and compute."""
    rows, truncated = _week_rows(queries, chat_id, week, max_rows=max_rows)
    events = _week_reactions(
        queries, tuple(str(row["message_id"]) for row in rows), until_ms=week.end_ms - 1
    )
    return compute_signals(
        rows, events, chat_id=chat_id, week=week, lull_ms=lull_ms, burst_n=burst_n,
        burst_ms=burst_ms, registry=registry_ids(rows, thread_lookup), tz=tz,
        truncated=truncated,
    )


def _week_rows(
    queries: HistoryQueries, chat_id: str, week: DigestWeek, *, max_rows: int
) -> tuple[list[dict[str, Any]], bool]:
    """Every visible row of the week, newest page first, paged by message id."""
    collected: list[dict[str, Any]] = []
    before: str | None = None
    while len(collected) < max_rows:
        page = queries.recent(
            chat_id=chat_id, limit=_PAGE, before_id=before,
            after_ms=week.start_ms - 1, until_ms=week.end_ms - 1,
        )
        if not page:
            break
        collected.extend(page)
        if len(page) < _PAGE:
            break
        before = str(page[0]["message_id"])
    truncated = len(collected) >= max_rows
    collected = collected[:max_rows]
    collected.sort(key=lambda row: (int(row.get("sent_ms") or 0), str(row["message_id"])))
    return collected, truncated


def _week_reactions(
    queries: HistoryQueries, message_ids: tuple[str, ...], *, until_ms: int
) -> list[dict[str, Any]]:
    """Reaction events of these messages; a full batch is split so nothing is cut."""
    events: list[dict[str, Any]] = []
    pending = [
        message_ids[index:index + _REACTION_BATCH]
        for index in range(0, len(message_ids), _REACTION_BATCH)
    ]
    while pending:
        batch = pending.pop()
        found = queries.reactions(message_ids=batch, until_ms=until_ms, limit=_PAGE)
        if len(found) >= _PAGE and len(batch) > 1:
            middle = len(batch) // 2
            pending.extend((batch[:middle], batch[middle:]))
            continue
        events.extend(found)
    return events


# ── helpers ──────────────────────────────────────────────────────────────────


def _sender_key(row: Mapping[str, Any]) -> str:
    if row.get("direction") == "out":
        return _ARVID
    return str(
        row.get("sender_contact_id") or row.get("sender_identifier") or row.get("sender_id") or ""
    )


def _current_reactions(
    reactions: Iterable[Mapping[str, Any]],
) -> dict[str, list[tuple[str, str]]]:
    """``(actor, emoji)`` per target message: the latest event per message and actor wins.

    The same rule as the conversation segmentation, plus the actor for distinct counts.
    """
    latest: dict[tuple[str, str], tuple[tuple[int, str], Mapping[str, Any]]] = {}
    for event in reactions:
        target = str(event.get("target_message_id") or "")
        if not target:
            continue
        actor = str(event.get("actor_contact_id") or event.get("actor_identifier") or "")
        occurred = event.get("occurred_ms")
        rank = (int(occurred) if isinstance(occurred, int) else -1, str(event.get("event_id") or ""))
        slot = (target, actor)
        if slot not in latest or rank > latest[slot][0]:
            latest[slot] = (rank, event)
    current: dict[str, list[tuple[str, str]]] = {}
    for (target, actor), (_rank, event) in sorted(latest.items(), key=lambda item: item[1][0]):
        emoji = str(event.get("emoji") or "")
        if emoji and not event.get("removed"):
            current.setdefault(target, []).append((actor, emoji))
    return current


def _text(row: Mapping[str, Any]) -> str:
    value = row.get("current_text")
    if value is None:
        value = row.get("text")
    return " ".join(str(value or "").split())


def excerpt(text: str, limit: int = EXCERPT_CHARS) -> str:
    """Whitespace-collapsed text, at most ``limit`` characters."""
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def message_kind(row: Mapping[str, Any]) -> str:
    """text / link / image / video / voice / sticker / document / poll / media."""
    raw = row.get("media_json")
    media: Any = None
    if isinstance(raw, str) and raw:
        try:
            media = json.loads(raw)
        except json.JSONDecodeError:
            media = {"kind": "unknown"}
    elif isinstance(raw, Mapping):
        media = raw
    if isinstance(media, list):
        media = next((item for item in media if isinstance(item, Mapping)), None)
    if isinstance(media, Mapping):
        return _MEDIA_KINDS.get(str(media.get("kind") or ""), "media")
    return "link" if _LINK.search(_text(row)) else "text"


def length_bucket(text: str) -> str:
    size = len(text.strip())
    if size == 0:
        return "none"
    if size <= 40:
        return "short"
    if size <= EXCERPT_CHARS:
        return "medium"
    return "long"


def time_of_day(ms: int, tz: tzinfo | None) -> str:
    hour = _local_datetime(ms, tz).hour
    if hour < 6:
        return "night"
    if hour < 12:
        return "morning"
    if hour < 18:
        return "afternoon"
    return "evening"


def _approval(
    rows: list[Mapping[str, Any]],
    current: Mapping[str, list[tuple[str, str]]],
    *,
    tz: tzinfo | None,
) -> tuple[ReactedMessage, ...]:
    reacted: list[ReactedMessage] = []
    for row in rows:
        message_id = str(row.get("message_id") or "")
        entries = current.get(message_id) or []
        if not entries:
            continue
        counts = Counter(emoji for _actor, emoji in entries)
        text = _text(row)
        reacted.append(ReactedMessage(
            message_id=message_id,
            sent_ms=int(row["sent_ms"]),
            kind=message_kind(row),
            length=length_bucket(text),
            is_reply=bool(row.get("reply_to_native_id")),
            time_of_day=time_of_day(int(row["sent_ms"]), tz),
            from_arvid=row.get("direction") == "out",
            reactions=len(entries),
            reactors=len({actor for actor, _emoji in entries}),
            emojis=tuple(sorted(counts, key=lambda emoji: (-counts[emoji], emoji))),
            negative=sum(counts[emoji] for emoji in NEGATIVE_EMOJIS),
            excerpt=excerpt(text),
        ))
    reacted.sort(key=lambda item: (-item.reactors, -item.reactions, item.sent_ms, item.message_id))
    return tuple(reacted)


def _conversation_signal(
    conversation: Conversation, by_id: Mapping[str, Mapping[str, Any]]
) -> ConversationSignal:
    opening = tuple(
        (message_id, excerpt(_text(by_id[message_id])))
        for message_id in conversation.message_ids[:OPENING_MESSAGES]
        if message_id in by_id
    )
    return ConversationSignal(
        conversation_id=conversation.id,
        start_ms=conversation.start_ms,
        duration_ms=conversation.duration_ms,
        senders=conversation.sender_count,
        messages=conversation.message_count,
        reactions=conversation.reaction_count,
        burst=conversation.burst,
        arvid_messages=conversation.arvid_messages,
        arvid_addressed=conversation.arvid_addressed,
        opening=opening,
    )


def _arvid_effect(
    rows: list[Mapping[str, Any]],
    conversations: list[Conversation],
    current: Mapping[str, list[tuple[str, str]]],
    *,
    lull_ms: int,
    registry: frozenset[str],
) -> ArvidEffect:
    replied_natives = {
        str(row.get("reply_to_native_id"))
        for row in rows
        if row.get("direction") != "out" and row.get("reply_to_native_id")
    }
    by_id = {str(row.get("message_id") or ""): row for row in rows}
    by_native = {
        str(row.get("native_message_id")): row for row in rows if row.get("native_message_id")
    }
    inbound_times = [int(row["sent_ms"]) for row in rows if row.get("direction") != "out"]

    messages: list[ArvidMessage] = []
    addressed = addressed_with_others = others_total = 0
    for conversation in conversations:
        members = [by_id[message_id] for message_id in conversation.message_ids if message_id in by_id]
        for index, row in enumerate(members):
            if row.get("direction") != "out":
                continue
            sent = int(row["sent_ms"])
            after = [item for item in members[index + 1:] if item.get("direction") != "out"]
            native = str(row.get("native_message_id") or "")
            messages.append(ArvidMessage(
                message_id=str(row.get("message_id") or ""),
                sent_ms=sent,
                followed=any(sent < moment <= sent + lull_ms for moment in inbound_times),
                replied=bool(native) and native in replied_natives,
                reacted=bool(current.get(str(row.get("message_id") or ""))),
                continued=bool(after),
                others_after=len({_sender_key(item) for item in after} - {""}),
                messages_after=len(after),
                excerpt=excerpt(_text(row)),
            ))
        if not conversation.arvid_addressed:
            continue
        addressed += 1
        index = next(
            (
                position for position, row in enumerate(members)
                if row.get("direction") == "out"
                and str(row.get("native_message_id") or "") in registry
            ),
            None,
        )
        if index is None:  # pragma: no cover - arvid_addressed implies a registry message
            continue
        answer = members[index]
        target = by_native.get(str(answer.get("reply_to_native_id") or ""))
        if target is None or target.get("direction") == "out":
            target = next(
                (row for row in reversed(members[:index]) if row.get("direction") != "out"), None
            )
        addresser = _sender_key(target) if target is not None else ""
        others = {
            _sender_key(row) for row in members[index + 1:] if row.get("direction") != "out"
        } - {"", addresser}
        others_total += len(others)
        addressed_with_others += 1 if others else 0

    continued = [item for item in messages if item.continued and item.answered]
    ended = [item for item in messages if not item.continued]
    rank = lambda item: (-item.others_after, -item.messages_after, item.sent_ms)  # noqa: E731
    return ArvidEffect(
        messages=len(messages),
        followed=sum(1 for item in messages if item.followed),
        replied=sum(1 for item in messages if item.replied),
        reacted=sum(1 for item in messages if item.reacted),
        continued=sum(1 for item in messages if item.continued),
        ended=len(ended),
        addressed_conversations=addressed,
        addressed_with_others=addressed_with_others,
        others_after_addressed=others_total,
        answered_and_continued=tuple(sorted(continued, key=rank)[:ARVID_EXAMPLES]),
        ended_conversation=tuple(sorted(ended, key=lambda item: item.sent_ms)[-ARVID_EXAMPLES:]),
    )


__all__ = [
    "ARVID_EXAMPLES",
    "EXCERPT_CHARS",
    "MIN_CONVERSATIONS",
    "MIN_REACTIONS",
    "NEGATIVE_EMOJIS",
    "TOP_CONVERSATIONS",
    "TOP_MESSAGES",
    "ArvidEffect",
    "ArvidMessage",
    "ConversationSignal",
    "DigestWeek",
    "GroupSignals",
    "ReactedMessage",
    "compute_signals",
    "excerpt",
    "iso_week",
    "length_bucket",
    "message_kind",
    "previous_week",
    "read_group_signals",
    "time_of_day",
    "week_moment_ms",
    "week_of",
]
