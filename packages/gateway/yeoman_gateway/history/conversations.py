"""Group conversations: a deterministic, read-only segmentation of one chat's history.

A conversation is a maximal run of messages in which no gap between two consecutive
messages exceeds the lull silence. Nothing is stored; the same rows always give the
same conversations and the same ids. Reply links into an earlier conversation are
recorded, never used to merge.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, tzinfo
from typing import Any, Literal

from .queries import HistoryQueries

ConversationState = Literal["active", "slowing", "ended"]

#: Arvid's own messages carry no sender identifier; they share one sender key.
_ARVID = "\x00arvid"


@dataclass(frozen=True, slots=True)
class ConversationBounds:
    """What one read may cover and the live lull/burst definitions."""

    window_ms: int
    max_rows: int
    lull_ms: int
    burst_n: int
    burst_ms: int


@dataclass(frozen=True, slots=True)
class Conversation:
    """One segmented conversation. Ids and aggregates only; no message text."""

    id: str
    chat_id: str
    start_ms: int
    last_ms: int
    #: History message ids, oldest first.
    message_ids: tuple[str, ...]
    #: Provider (native) message ids in the same order; empty string when unknown.
    native_ids: tuple[str, ...]
    sender_count: int
    message_count: int
    reaction_count: int
    top_emojis: tuple[str, ...]
    #: At least one burst inside the conversation ("engaging").
    burst: bool
    arvid_messages: int
    #: Someone else wrote after Arvid's last message.
    answered_after_arvid: bool
    #: An Arvid message in it is linked to one of his registry threads.
    arvid_addressed: bool
    #: ``(message_id, reply_to_native_id)`` for replies to a message outside this one.
    outside_replies: tuple[tuple[str, str], ...]
    state: ConversationState

    @property
    def duration_ms(self) -> int:
        return self.last_ms - self.start_ms

    def stats(self) -> dict[str, object]:
        """JSON-compatible ids and aggregates for reports and logs."""
        return {
            "conversation_id": self.id,
            "start_ms": self.start_ms,
            "last_ms": self.last_ms,
            "message_count": self.message_count,
            "sender_count": self.sender_count,
            "reaction_count": self.reaction_count,
            "top_emojis": list(self.top_emojis),
            "burst": self.burst,
            "arvid_messages": self.arvid_messages,
            "answered_after_arvid": self.answered_after_arvid,
            "arvid_addressed": self.arvid_addressed,
            "outside_reply_count": len(self.outside_replies),
            "state": self.state,
        }


def conversation_id(chat_id: str, first_message_id: str) -> str:
    digest = hashlib.sha256(f"{chat_id}\n{first_message_id}".encode()).hexdigest()
    return "conv:" + digest[:16]


def segment(
    rows: Iterable[Mapping[str, Any]],
    reactions: Iterable[Mapping[str, Any]],
    *,
    lull_ms: int,
    burst_n: int,
    burst_ms: int,
    now_ms: int | None = None,
    registry_ids: Collection[str] = (),
) -> list[Conversation]:
    """Split one chat's history rows into conversations, oldest first.

    ``rows`` are ``messages_current``-shaped (``message_id``, ``native_message_id``,
    ``chat_id``, ``sent_ms``, ``direction``, sender columns, ``reply_to_native_id``).
    Rows without ``sent_ms`` cannot be placed and are skipped. ``reactions`` are
    reaction events (``target_message_id``, actor, ``emoji``, ``removed``,
    ``occurred_ms``); the latest event per message and actor counts. ``registry_ids``
    are native ids of Arvid messages linked to a registry thread. ``now_ms`` (default:
    the newest message) decides the state.
    """
    if lull_ms <= 0 or burst_n <= 0 or burst_ms <= 0:
        raise ValueError("lull, burst threshold and burst window must be positive")
    timed = [row for row in rows if isinstance(row.get("sent_ms"), int)]
    timed.sort(key=lambda row: (int(row["sent_ms"]), str(row.get("message_id") or "")))
    if not timed:
        return []
    chats = {str(row.get("chat_id") or "") for row in timed}
    if len(chats) != 1:
        raise ValueError("segment reads one chat at a time")
    chat_id = chats.pop()
    moment = int(now_ms) if now_ms is not None else int(timed[-1]["sent_ms"])

    groups: list[list[Mapping[str, Any]]] = [[timed[0]]]
    for row in timed[1:]:
        if int(row["sent_ms"]) - int(groups[-1][-1]["sent_ms"]) > lull_ms:
            groups.append([])
        groups[-1].append(row)

    reaction_state = _current_reactions(reactions)
    registry = {str(item) for item in registry_ids if str(item)}
    return [
        _conversation(
            chat_id, group, reaction_state, registry,
            lull_ms=lull_ms, burst_n=burst_n, burst_ms=burst_ms, now_ms=moment,
        )
        for group in groups
    ]


def read_conversations(
    queries: HistoryQueries,
    chat_id: str,
    now_ms: int,
    bounds: ConversationBounds,
    *,
    thread_lookup: Callable[[str], object] | None = None,
) -> list[Conversation]:
    """Segment the bounded recent rows of one chat as of ``now_ms``."""
    rows = queries.recent(
        chat_id=chat_id, after_ms=int(now_ms) - int(bounds.window_ms) - 1, until_ms=int(now_ms),
        limit=max(1, min(int(bounds.max_rows), 500)),
    )
    return segment(
        rows,
        queries.reactions(
            message_ids=tuple(str(row["message_id"]) for row in rows), until_ms=int(now_ms),
        ),
        lull_ms=bounds.lull_ms, burst_n=bounds.burst_n, burst_ms=bounds.burst_ms,
        now_ms=now_ms, registry_ids=registry_ids(rows, thread_lookup),
    )


def current_conversation(
    queries: HistoryQueries,
    chat_id: str,
    now_ms: int,
    bounds: ConversationBounds,
    *,
    thread_lookup: Callable[[str], object] | None = None,
) -> Conversation | None:
    """The newest conversation of one chat as of ``now_ms``, or ``None``."""
    conversations = read_conversations(
        queries, chat_id, now_ms, bounds, thread_lookup=thread_lookup
    )
    return conversations[-1] if conversations else None


def registry_ids(
    rows: Iterable[Mapping[str, Any]], thread_lookup: Callable[[str], object] | None
) -> frozenset[str]:
    """Native ids of Arvid's messages that a registry thread references."""
    if thread_lookup is None:
        return frozenset()
    found: set[str] = set()
    for row in rows:
        native = str(row.get("native_message_id") or "")
        if row.get("direction") == "out" and native and thread_lookup(native) is not None:
            found.add(native)
    return frozenset(found)


def render_header(
    conversation: Conversation, *, tz: tzinfo | None = None
) -> str:
    """One summary line for the judge; data, not instructions."""
    started = datetime.fromtimestamp(conversation.start_ms / 1000, tz)
    if tz is None:
        started = started.astimezone()
    minutes = conversation.duration_ms // 60_000
    parts = [
        f"Conversation since {started:%H:%M} ({minutes} min)",
        _plural(conversation.sender_count, "person", "people"),
        _plural(conversation.message_count, "message", "messages"),
    ]
    reactions = _plural(conversation.reaction_count, "reaction", "reactions")
    if conversation.top_emojis:
        reactions += f" ({''.join(conversation.top_emojis)})"
    parts.append(reactions)
    if conversation.arvid_messages:
        answered = "answered" if conversation.answered_after_arvid else "unanswered"
        parts.append(
            f"Arvid: {_plural(conversation.arvid_messages, 'message', 'messages')}, {answered}"
        )
    else:
        parts.append("Arvid: not in it")
    parts.append(f"state: {conversation.state}")
    return " · ".join(parts)


def _plural(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def _sender_key(row: Mapping[str, Any]) -> str:
    if row.get("direction") == "out":
        return _ARVID
    return str(
        row.get("sender_contact_id") or row.get("sender_identifier") or row.get("sender_id") or ""
    )


def _current_reactions(reactions: Iterable[Mapping[str, Any]]) -> dict[str, list[str]]:
    """Current emojis per target message: the latest event per message and actor wins."""
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
    current: dict[str, list[str]] = {}
    for (target, _actor), (_rank, event) in sorted(latest.items(), key=lambda item: item[1][0]):
        emoji = str(event.get("emoji") or "")
        if emoji and not event.get("removed"):
            current.setdefault(target, []).append(emoji)
    return current


def _has_burst(times: list[int], burst_n: int, burst_ms: int) -> bool:
    start = 0
    for end, moment in enumerate(times):
        while moment - times[start] > burst_ms:
            start += 1
        if end - start + 1 >= burst_n:
            return True
    return False


def _conversation(
    chat_id: str,
    group: list[Mapping[str, Any]],
    reactions: Mapping[str, list[str]],
    registry: set[str],
    *,
    lull_ms: int,
    burst_n: int,
    burst_ms: int,
    now_ms: int,
) -> Conversation:
    message_ids = tuple(str(row.get("message_id") or "") for row in group)
    native_ids = tuple(str(row.get("native_message_id") or "") for row in group)
    times = [int(row["sent_ms"]) for row in group]
    emojis = [emoji for message_id in message_ids for emoji in reactions.get(message_id, ())]
    counts = Counter(emojis)
    first_seen = {emoji: index for index, emoji in reversed(list(enumerate(emojis)))}
    top = sorted(counts, key=lambda emoji: (-counts[emoji], first_seen[emoji]))[:3]

    arvid_positions = [index for index, row in enumerate(group) if row.get("direction") == "out"]
    answered = bool(arvid_positions) and any(
        row.get("direction") != "out" for row in group[arvid_positions[-1] + 1:]
    )
    addressed = any(
        group[index].get("direction") == "out" and native_ids[index] in registry
        for index in arvid_positions
    )
    own = set(native_ids) - {""}
    outside = tuple(
        (message_ids[index], str(row.get("reply_to_native_id")))
        for index, row in enumerate(group)
        if row.get("reply_to_native_id") and str(row.get("reply_to_native_id")) not in own
    )

    last = times[-1]
    state: ConversationState
    if now_ms - last >= lull_ms:
        state = "ended"
    elif sum(1 for moment in times if now_ms - burst_ms <= moment <= now_ms) >= burst_n:
        state = "active"
    else:
        state = "slowing"
    return Conversation(
        id=conversation_id(chat_id, message_ids[0]),
        chat_id=chat_id,
        start_ms=times[0],
        last_ms=last,
        message_ids=message_ids,
        native_ids=native_ids,
        sender_count=len({_sender_key(row) for row in group} - {""}),
        message_count=len(group),
        reaction_count=len(emojis),
        top_emojis=tuple(top),
        burst=_has_burst(times, burst_n, burst_ms),
        arvid_messages=len(arvid_positions),
        answered_after_arvid=answered,
        arvid_addressed=addressed,
        outside_replies=outside,
        state=state,
    )


__all__ = [
    "Conversation",
    "ConversationBounds",
    "ConversationState",
    "conversation_id",
    "current_conversation",
    "read_conversations",
    "registry_ids",
    "render_header",
    "segment",
]
