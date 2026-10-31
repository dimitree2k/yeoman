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
    own_actor_ids: Iterable[str] = (),
) -> GroupSignals:
    """The three signals of one chat for one week; pure (rows in, signals out).

    Rows outside ``[week.start_ms, week.end_ms)``, of another chat or without a time
    are ignored. Reactions follow the segmentation rule: the latest event per message
    and actor counts, and a removed reaction does not. Arvid's own reactions
    (``own_actor_ids``) are not group signal: counting them would let the note learn
    his habits back from him.
    """
    own = frozenset(str(item) for item in own_actor_ids if str(item))
    if own:
        reactions = [
            event for event in reactions if str(event.get("actor_contact_id") or "") not in own
        ]
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
        truncated=truncated, own_actor_ids=queries.assistant_contact_ids(),
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
        answer_at = next(
            (
                position for position, row in enumerate(members)
                if row.get("direction") == "out"
                and str(row.get("native_message_id") or "") in registry
            ),
            None,
        )
        if answer_at is None:  # pragma: no cover - arvid_addressed implies a registry message
            continue
        answer = members[answer_at]
        target = by_native.get(str(answer.get("reply_to_native_id") or ""))
        if target is None or target.get("direction") == "out":
            target = next(
                (row for row in reversed(members[:answer_at]) if row.get("direction") != "out"),
                None,
            )
        addresser = _sender_key(target) if target is not None else ""
        others = {
            _sender_key(row) for row in members[answer_at + 1:] if row.get("direction") != "out"
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


# ── reception (P1) ───────────────────────────────────────────────────────────

#: A direct answer may arrive later than the first conversational follow-up.
DIRECT_WINDOW_MS = 30 * 60_000
#: The addressed person writing again, or anyone else replying, is the first reaction.
INDIRECT_WINDOW_MS = 5 * 60_000
#: No human message at all in this window is silence.
SILENCE_WINDOW_MS = 10 * 60_000
#: A week below both floors is stored as ``insufficient_data``.
MIN_ARVID_MESSAGES = 5
MIN_HUMAN_MESSAGES = 50
#: Deterministic baseline sample: every k-th human quote-reply, capped per week.
BASELINE_EVERY = 5
BASELINE_MAX = 200

_WORD = re.compile(r"[^\W\d_]+")


@dataclass(frozen=True, slots=True)
class ReceptionWindow:
    """What followed one Arvid text message, by response kind.

    ``direct`` and ``indirect`` may both be true; ``category`` is the strongest
    non-overlapping label used by the report so its percentages add up.
    """

    message_id: str
    native_message_id: str
    sent_ms: int
    addressed: bool
    direct: bool
    indirect: bool
    others: bool
    silent: bool
    direct_people: tuple[str, ...]
    indirect_people: tuple[str, ...]
    other_people: tuple[str, ...]
    reactions: int
    negative_reactions: int
    emojis: tuple[str, ...]
    emoji_counts: tuple[tuple[str, int], ...] = ()
    action_type: str = "unknown"
    persona_sha: str = "unknown"
    kind: str = "text"

    @property
    def category(self) -> str:
        if self.direct:
            return "direct"
        if self.indirect:
            return "indirect"
        if self.others:
            return "others"
        return "silence" if self.silent else "quiet"

    def descriptor(self) -> dict[str, object]:
        """Ids, flags and counts only: safe for ``metrics_json``."""
        return {
            "message_id": self.message_id,
            "native_message_id": self.native_message_id,
            "sent_ms": self.sent_ms,
            "addressed": self.addressed,
            "direct": self.direct,
            "indirect": self.indirect,
            "others": self.others,
            "silent": self.silent,
            "category": self.category,
            "direct_people": list(self.direct_people),
            "indirect_people": list(self.indirect_people),
            "other_people": list(self.other_people),
            "reactions": self.reactions,
            "negative_reactions": self.negative_reactions,
            "emojis": list(self.emojis),
            "emoji_counts": [[emoji, count] for emoji, count in self.emoji_counts],
            "action_type": self.action_type,
            "persona_sha": self.persona_sha,
            "kind": self.kind,
        }


@dataclass(frozen=True, slots=True)
class BaselineWindow:
    """The same window rules applied to one human quote-reply."""

    message_id: str
    direct: bool
    indirect: bool
    others: bool
    silent: bool

    @property
    def category(self) -> str:
        if self.direct:
            return "direct"
        if self.indirect:
            return "indirect"
        if self.others:
            return "others"
        return "silence" if self.silent else "quiet"


@dataclass(frozen=True, slots=True)
class ReceptionMetrics:
    """One chat-week of deterministic reception numbers. Never any message text."""

    chat_id: str
    week: DigestWeek
    arvid_messages: int
    human_messages: int
    addressed: int
    unprompted: int
    direct: int
    indirect: int
    others: int
    silence: int
    reactions_received: int
    negative_reactions: int
    arvid_reactions_given: int
    human_reactions: int
    engaged_people: tuple[str, ...]
    top_person_id: str
    top_person_share: float
    task_requests: int
    baseline_sampled: int
    baseline_direct: int
    baseline_indirect: int
    baseline_silence: int
    windows: tuple[ReceptionWindow, ...]
    baseline_windows: tuple[BaselineWindow, ...] = ()
    min_arvid_messages: int = MIN_ARVID_MESSAGES
    min_human_messages: int = MIN_HUMAN_MESSAGES

    @property
    def sufficient(self) -> bool:
        """Both activity floors are below their minimum: too little to read."""
        return not (
            self.arvid_messages < self.min_arvid_messages
            and self.human_messages < self.min_human_messages
        )

    @property
    def status(self) -> str:
        return "ok" if self.sufficient else "insufficient_data"

    @staticmethod
    def _rate(count: int, total: int) -> float:
        return round(count / total, 4) if total else 0.0

    def metrics(self) -> dict[str, object]:
        """Numbers and contact ids only; safe for ``metrics_json``."""
        total = self.arvid_messages
        return {
            "arvid_messages": total,
            "human_messages": self.human_messages,
            "addressed": self.addressed,
            "unprompted": self.unprompted,
            "direct": self.direct,
            "indirect": self.indirect,
            "others": self.others,
            "silence": self.silence,
            "direct_rate": self._rate(self.direct, total),
            "indirect_rate": self._rate(self.indirect, total),
            "others_rate": self._rate(self.others, total),
            "silence_rate": self._rate(self.silence, total),
            "reactions_received": self.reactions_received,
            "reactions_per_arvid": self._rate(self.reactions_received, total),
            "negative_reactions": self.negative_reactions,
            "arvid_reactions_given": self.arvid_reactions_given,
            "human_reactions": self.human_reactions,
            "reactions_per_human": self._rate(self.human_reactions, self.human_messages),
            "engaged_people": list(self.engaged_people),
            "breadth": len(self.engaged_people),
            "top_person_id": self.top_person_id,
            "top_person_share": self.top_person_share,
            "task_requests": self.task_requests,
            "baseline_sampled": self.baseline_sampled,
            "baseline_direct_rate": self._rate(self.baseline_direct, self.baseline_sampled),
            "baseline_indirect_rate": self._rate(
                self.baseline_indirect, self.baseline_sampled
            ),
            "baseline_silence_rate": self._rate(
                self.baseline_silence, self.baseline_sampled
            ),
            "minimum": {
                "arvid_messages": self.min_arvid_messages,
                "human_messages": self.min_human_messages,
                "met": self.sufficient,
            },
            "status": self.status,
            "windows": [item.descriptor() for item in self.windows],
        }


def _mentions_identity(row: Mapping[str, Any], identifiers: frozenset[str]) -> bool:
    """True when a mention entry names one of Arvid's strong identifiers."""
    if not identifiers:
        return False
    raw = row.get("mentions_json")
    if isinstance(raw, str) and raw:
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = None
    values = raw if isinstance(raw, list) else ()
    local = {value.split("@", 1)[0] for value in identifiers if "@" in value}
    for value in values:
        if not isinstance(value, str):
            continue
        candidate = value.lstrip("@")
        candidate = candidate.rsplit(":", 1)[-1] if ":" in candidate else candidate
        if candidate in identifiers or candidate.split("@", 1)[0] in local:
            return True
    text = _text(row)
    if text:
        if any(identifier in text for identifier in identifiers):
            return True
        if any(f"@{number}" in text for number in local if len(number) >= 5):
            return True
    return False


def _name_patterns(names: Iterable[str]) -> tuple[re.Pattern[str], ...]:
    terms: set[str] = set()
    for name in names:
        full = " ".join(str(name).split())
        if full:
            terms.add(full)
        terms.update(token for token in _WORD.findall(full) if len(token) >= 3)
    return tuple(
        re.compile(rf"(?<!\w){re.escape(term)}(?!\w)", re.IGNORECASE)
        for term in sorted(terms, key=lambda value: (-len(value), value))
    )


def _named(row: Mapping[str, Any], patterns: Iterable[re.Pattern[str]]) -> bool:
    text = _text(row)
    return bool(text) and any(pattern.search(text) for pattern in patterns)


def _addressed_to(
    row: Mapping[str, Any],
    *,
    native_ids: frozenset[str],
    identifiers: frozenset[str],
    patterns: tuple[re.Pattern[str], ...],
) -> bool:
    if str(row.get("reply_to_native_id") or "") in native_ids:
        return True
    return _mentions_identity(row, identifiers) or _named(row, patterns)


def _text_row(row: Mapping[str, Any]) -> bool:
    """Arvid's text messages: non-empty text without media."""
    if row.get("media_json"):
        return False
    return bool(_text(row))


def _window_for(
    target: Mapping[str, Any],
    humans: list[Mapping[str, Any]],
    *,
    quoted_author: str,
    identifiers: frozenset[str],
    patterns: tuple[re.Pattern[str], ...],
    direct_window_ms: int = DIRECT_WINDOW_MS,
    indirect_window_ms: int = INDIRECT_WINDOW_MS,
    silence_window_ms: int = SILENCE_WINDOW_MS,
) -> tuple[bool, bool, bool, bool, tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Direct/indirect/others/silence for one target, plus the people involved."""
    target_native = str(target.get("native_message_id") or "")
    sent = int(target["sent_ms"])
    direct = indirect = others = False
    silent = True
    direct_people: set[str] = set()
    indirect_people: set[str] = set()
    other_people: set[str] = set()
    for row in humans:
        moment = int(row["sent_ms"])
        if moment <= sent:
            continue
        if moment <= sent + silence_window_ms:
            silent = False
        if moment > sent + direct_window_ms:
            break
        sender = _sender_key(row)
        quoted = str(row.get("reply_to_native_id") or "")
        is_direct = quoted == target_native or _mentions_identity(
            row, identifiers
        ) or _named(row, patterns)
        if is_direct:
            direct = True
            if sender:
                direct_people.add(sender)
            continue
        if moment > sent + indirect_window_ms:
            continue
        if quoted_author and sender == quoted_author:
            indirect = True
            indirect_people.add(sender)
        else:
            others = True
            if sender:
                other_people.add(sender)
    return (
        direct, indirect, others, silent,
        tuple(sorted(direct_people)), tuple(sorted(indirect_people)),
        tuple(sorted(other_people)),
    )


def _baseline_windows(
    humans: list[Mapping[str, Any]],
    by_native: Mapping[str, Mapping[str, Any]],
    *,
    contact_names: Mapping[str, str],
    every: int,
    maximum: int,
    direct_window_ms: int = DIRECT_WINDOW_MS,
    indirect_window_ms: int = INDIRECT_WINDOW_MS,
    silence_window_ms: int = SILENCE_WINDOW_MS,
) -> tuple[BaselineWindow, ...]:
    """Every k-th human quote-reply, deterministic and capped."""
    candidates = [row for row in humans if row.get("reply_to_native_id")]
    windows: list[BaselineWindow] = []
    for index in range(0, len(candidates), max(1, int(every))):
        if len(windows) >= maximum:
            break
        row = candidates[index]
        author = str(row.get("sender_contact_id") or "")
        quoted = by_native.get(str(row.get("reply_to_native_id") or ""))
        quoted_author = _sender_key(quoted) if quoted is not None else ""
        name = contact_names.get(author, "")
        patterns = _name_patterns((name,)) if name else ()
        identifiers = frozenset(
            value for value in (str(row.get("sender_identifier") or ""),) if value
        )
        direct, indirect, others, silent, *_ = _window_for(
            row, humans, quoted_author=quoted_author, identifiers=identifiers,
            patterns=patterns, direct_window_ms=direct_window_ms,
            indirect_window_ms=indirect_window_ms,
            silence_window_ms=silence_window_ms,
        )
        windows.append(BaselineWindow(
            message_id=str(row.get("message_id") or ""), direct=direct,
            indirect=indirect, others=others, silent=silent,
        ))
    return tuple(windows)


def compute_reception(
    rows: Iterable[Mapping[str, Any]],
    reactions: Iterable[Mapping[str, Any]],
    *,
    chat_id: str,
    week: DigestWeek,
    arvid_identifiers: Iterable[str] = (),
    arvid_names: Iterable[str] = (),
    outbound_meta: Mapping[str, Mapping[str, Any]] | None = None,
    assistant_contact_ids: Iterable[str] = (),
    contact_names: Mapping[str, str] | None = None,
    min_arvid_messages: int = MIN_ARVID_MESSAGES,
    min_human_messages: int = MIN_HUMAN_MESSAGES,
    baseline_every: int = BASELINE_EVERY,
    baseline_max: int = BASELINE_MAX,
    direct_window_ms: int = DIRECT_WINDOW_MS,
    indirect_window_ms: int = INDIRECT_WINDOW_MS,
    silence_window_ms: int = SILENCE_WINDOW_MS,
) -> ReceptionMetrics:
    """Deterministic reception numbers for one chat-week; pure (rows in, numbers out)."""
    identifiers = frozenset(str(item) for item in arvid_identifiers if str(item))
    patterns = _name_patterns(arvid_names)
    names = dict(contact_names or {})
    meta = dict(outbound_meta or {})
    timed = [
        row for row in rows
        if isinstance(row.get("sent_ms"), int)
        and week.start_ms <= int(row["sent_ms"]) < week.end_ms
        and str(row.get("chat_id") or "") == str(chat_id)
    ]
    timed.sort(key=lambda row: (int(row["sent_ms"]), str(row.get("message_id") or "")))
    humans = [row for row in timed if row.get("direction") != "out"]
    arvid_rows = [
        row for row in timed if row.get("direction") == "out" and _text_row(row)
    ]
    by_native = {
        str(row.get("native_message_id")): row
        for row in timed if row.get("native_message_id")
    }
    arvid_native = frozenset(
        str(row.get("native_message_id"))
        for row in timed
        if row.get("direction") == "out" and row.get("native_message_id")
    )
    current = _current_reactions(reactions)
    own = frozenset(str(item) for item in assistant_contact_ids if str(item))

    windows: list[ReceptionWindow] = []
    for row in arvid_rows:
        quoted = by_native.get(str(row.get("reply_to_native_id") or ""))
        quoted_author = _sender_key(quoted) if quoted is not None and quoted.get("direction") != "out" else ""
        direct, indirect, others, silent, direct_people, indirect_people, other_people = (
            _window_for(
                row, humans, quoted_author=quoted_author, identifiers=identifiers,
                patterns=patterns, direct_window_ms=direct_window_ms,
                indirect_window_ms=indirect_window_ms,
                silence_window_ms=silence_window_ms,
            )
        )
        addressed = quoted is not None and _addressed_to(
            quoted, native_ids=arvid_native - {str(row.get("native_message_id") or "")},
            identifiers=identifiers, patterns=patterns,
        )
        entries = current.get(str(row.get("message_id") or ""), ())
        counts = Counter(emoji for _actor, emoji in entries)
        emojis = tuple(sorted(counts, key=lambda emoji: (-counts[emoji], emoji)))
        native = str(row.get("native_message_id") or "")
        detail = meta.get(native) or meta.get(str(row.get("message_id") or "")) or {}
        windows.append(ReceptionWindow(
            message_id=str(row.get("message_id") or ""),
            native_message_id=native,
            sent_ms=int(row["sent_ms"]),
            addressed=bool(addressed),
            direct=direct,
            indirect=indirect,
            others=others,
            silent=silent,
            direct_people=direct_people,
            indirect_people=indirect_people,
            other_people=other_people,
            reactions=len(entries),
            negative_reactions=sum(1 for _actor, emoji in entries if emoji in NEGATIVE_EMOJIS),
            emojis=emojis,
            emoji_counts=tuple((emoji, counts[emoji]) for emoji in emojis),
            action_type=str(detail.get("action_type") or "unknown"),
            persona_sha=str(detail.get("persona_sha") or "unknown"),
        ))

    direct = sum(1 for item in windows if item.category == "direct")
    indirect = sum(1 for item in windows if item.category == "indirect")
    others = sum(1 for item in windows if item.category == "others")
    silence = sum(1 for item in windows if item.category == "silence")

    engagement: Counter[str] = Counter()
    for item in windows:
        for person in (*item.direct_people, *item.indirect_people):
            engagement[person] += 1
    engaged = tuple(sorted(engagement))
    top_id = ""
    top_share = 0.0
    if engagement:
        top_id, top_count = max(engagement.items(), key=lambda pair: (pair[1], pair[0]))
        top_share = round(top_count / sum(engagement.values()), 4)

    human_reaction_total = sum(
        len(current.get(str(row.get("message_id") or ""), ())) for row in humans
    )
    arvid_reaction_total = sum(item.reactions for item in windows)
    given = sum(
        1
        for entries in current.values()
        for actor, _emoji in entries
        if actor in own
    )
    baseline = _baseline_windows(
        humans, by_native, contact_names=names, every=baseline_every,
        maximum=baseline_max, direct_window_ms=direct_window_ms,
        indirect_window_ms=indirect_window_ms, silence_window_ms=silence_window_ms,
    )
    task_requests = sum(
        1 for row in humans if _addressed_to(
            row, native_ids=arvid_native, identifiers=identifiers, patterns=patterns,
        )
    )
    return ReceptionMetrics(
        chat_id=str(chat_id),
        week=week,
        arvid_messages=len(windows),
        human_messages=len(humans),
        addressed=sum(1 for item in windows if item.addressed),
        unprompted=sum(1 for item in windows if not item.addressed),
        direct=direct,
        indirect=indirect,
        others=others,
        silence=silence,
        reactions_received=arvid_reaction_total,
        negative_reactions=sum(item.negative_reactions for item in windows),
        arvid_reactions_given=given,
        human_reactions=human_reaction_total,
        engaged_people=engaged,
        top_person_id=top_id,
        top_person_share=top_share,
        task_requests=task_requests,
        baseline_sampled=len(baseline),
        baseline_direct=sum(1 for item in baseline if item.category == "direct"),
        baseline_indirect=sum(1 for item in baseline if item.category == "indirect"),
        baseline_silence=sum(1 for item in baseline if item.category == "silence"),
        windows=tuple(windows),
        baseline_windows=baseline,
        min_arvid_messages=int(min_arvid_messages),
        min_human_messages=int(min_human_messages),
    )


def read_reception_signals(
    queries: HistoryQueries,
    chat_id: str,
    week: DigestWeek,
    *,
    outbound_meta: Mapping[str, Mapping[str, Any]] | None = None,
    max_rows: int = MAX_WEEK_ROWS,
    min_arvid_messages: int = MIN_ARVID_MESSAGES,
    min_human_messages: int = MIN_HUMAN_MESSAGES,
    baseline_every: int = BASELINE_EVERY,
    baseline_max: int = BASELINE_MAX,
    direct_window_ms: int = DIRECT_WINDOW_MS,
    indirect_window_ms: int = INDIRECT_WINDOW_MS,
    silence_window_ms: int = SILENCE_WINDOW_MS,
) -> ReceptionMetrics:
    """Read one chat's week from history (inside the caller's history turn) and compute."""
    rows, _truncated = _week_rows(queries, chat_id, week, max_rows=max_rows)
    events = _week_reactions(
        queries, tuple(str(row["message_id"]) for row in rows), until_ms=week.end_ms - 1
    )
    identifiers, names = queries.assistant_identity()
    contacts = {
        str(row.get("sender_contact_id"))
        for row in rows if row.get("direction") != "out" and row.get("sender_contact_id")
    }
    if not identifiers:
        identifiers = tuple(sorted({
            str(row.get("sender_identifier"))
            for row in rows
            if row.get("direction") == "out" and row.get("sender_identifier")
        }))
    if not names:
        out_contacts = {
            str(row.get("sender_contact_id"))
            for row in rows
            if row.get("direction") == "out" and row.get("sender_contact_id")
        }
        names = tuple(sorted(queries.contact_names(out_contacts).values(), key=str.casefold))
    return compute_reception(
        rows, events, chat_id=chat_id, week=week, arvid_identifiers=identifiers,
        arvid_names=names, outbound_meta=outbound_meta,
        assistant_contact_ids=queries.assistant_contact_ids(),
        contact_names=queries.contact_names(contacts),
        min_arvid_messages=min_arvid_messages,
        min_human_messages=min_human_messages,
        baseline_every=baseline_every, baseline_max=baseline_max,
        direct_window_ms=direct_window_ms,
        indirect_window_ms=indirect_window_ms,
        silence_window_ms=silence_window_ms,
    )


__all__ = [
    "ARVID_EXAMPLES",
    "BASELINE_EVERY",
    "BASELINE_MAX",
    "DIRECT_WINDOW_MS",
    "EXCERPT_CHARS",
    "INDIRECT_WINDOW_MS",
    "MIN_ARVID_MESSAGES",
    "MIN_CONVERSATIONS",
    "MIN_HUMAN_MESSAGES",
    "MIN_REACTIONS",
    "NEGATIVE_EMOJIS",
    "SILENCE_WINDOW_MS",
    "TOP_CONVERSATIONS",
    "TOP_MESSAGES",
    "ArvidEffect",
    "ArvidMessage",
    "BaselineWindow",
    "ConversationSignal",
    "DigestWeek",
    "GroupSignals",
    "ReactedMessage",
    "ReceptionMetrics",
    "ReceptionWindow",
    "compute_reception",
    "compute_signals",
    "excerpt",
    "iso_week",
    "length_bucket",
    "message_kind",
    "previous_week",
    "read_group_signals",
    "read_reception_signals",
    "time_of_day",
    "week_moment_ms",
    "week_of",
]
