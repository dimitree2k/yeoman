"""The ``past`` section: stored conversation and week summaries.

Summaries are written by :mod:`yeoman_gateway.group_state.summaries.runner`; this
contributor never calls a model. It carries the stored items forward, drops an item
whose source fingerprint changed and prunes weeks older than ``keepWeeks``. Week items
have no message sources; they depend on conversation ids so a dropped conversation
cascades without a 500-source union.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, tzinfo
from typing import Any

from ..contract import BuildContext
from ..models import Item, Section

PAST_SECTION = "past"
WEEK_MS = 7 * 24 * 3_600_000
_TTL_MS = WEEK_MS


class PastSection:
    key = PAST_SECTION
    schema_version = 1
    trust = "derived"
    layer = "week"

    def applies_to(self, channel: str, chat_kind: str) -> bool:
        return channel == "whatsapp" and chat_kind == "group"

    async def build(self, ctx: BuildContext) -> Section:
        previous: Section | None = None
        store = getattr(ctx, "store", None)
        if store is not None:
            state = store.get(ctx.channel, ctx.chat_id)
            previous = state.sections.get(PAST_SECTION) if state is not None else None
        items = previous.items if previous is not None else ()
        items = _fresh_items(items, ctx.history)
        summaries = getattr(ctx.settings, "summaries", None)
        keep_weeks = max(1, int(getattr(summaries, "keep_weeks", 12)))
        items = prune_past_items(items, now_ms=ctx.now_ms, keep_weeks=keep_weeks)
        return Section(
            key=PAST_SECTION,
            schema_version=1,
            trust="derived",
            layer="week",
            produced_at_ms=ctx.now_ms,
            ttl_ms=_TTL_MS,
            source_cursor=previous.source_cursor if previous is not None else "",
            period_start_ms=previous.period_start_ms if previous is not None else None,
            period_end_ms=previous.period_end_ms if previous is not None else None,
            sources=(),
            data={},
            items=tuple(items),
        )

    def migrate(self, from_version: int, data: Mapping[str, Any]) -> Mapping[str, Any]:
        return data


def _fresh_items(items: Iterable[Item], queries: Any) -> tuple[Item, ...]:
    """Drop items whose stored fingerprints no longer match history, cascading weeks."""
    kept: list[Item] = []
    dropped_conversations: set[str] = set()
    for item in items:
        stale = False
        for source, expected in item.source_fingerprints.items():
            if queries.content_fingerprint(str(source)) != str(expected):
                stale = True
                break
        if stale:
            if item.conversation_id:
                dropped_conversations.add(str(item.conversation_id))
            continue
        kept.append(item)
    conversations = {
        str(item.conversation_id)
        for item in kept
        if item.kind == "conversation_summary" and item.conversation_id
    }
    return tuple(
        item
        for item in kept
        if not _depends_on(item)
        or (
            not _depends_on(item).intersection(dropped_conversations)
            and _depends_on(item).issubset(conversations)
        )
    )


def _depends_on(item: Item) -> set[str]:
    raw = item.data.get("depends_on")
    return {str(value) for value in raw} if isinstance(raw, (list, tuple)) else set()


def prune_past_items(
    items: Iterable[Item],
    *,
    now_ms: int,
    keep_weeks: int,
    tz: tzinfo | None = None,
) -> tuple[Item, ...]:
    """Keep summaries from the last ``keep_weeks`` local weeks, newest items too."""
    cutoff = local_week_start(int(now_ms), tz) - timedelta(weeks=max(1, int(keep_weeks)))
    cutoff_ms = int(cutoff.timestamp() * 1000)
    kept: list[Item] = []
    pruned_conversations: set[str] = set()
    for item in items:
        if item.kind == "week_summary":
            start_ms = iso_week_start_ms(str(item.data.get("iso_week") or ""), tz)
            if start_ms is not None and start_ms < cutoff_ms:
                continue
        elif item.kind == "conversation_summary":
            ended_ms = _int(item.data.get("ended_ms"))
            if ended_ms is not None and ended_ms < cutoff_ms:
                if item.conversation_id:
                    pruned_conversations.add(str(item.conversation_id))
                continue
        kept.append(item)
    if not pruned_conversations:
        return tuple(kept)
    return tuple(item for item in kept if not _depends_on(item).intersection(pruned_conversations))


def local_week_start(now_ms: int, tz: tzinfo | None = None) -> datetime:
    """Monday 00:00 of the chat-local week that contains ``now_ms``."""
    moment = datetime.fromtimestamp(int(now_ms) / 1000, tz)
    if tz is None:
        moment = moment.astimezone()
    monday = moment.date() - timedelta(days=moment.weekday())
    return datetime(monday.year, monday.month, monday.day, tzinfo=moment.tzinfo)


def closed_week_bounds(
    now_ms: int, *, keep_weeks: int, tz: tzinfo | None = None
) -> tuple[tuple[str, int, int], ...]:
    """The last ``keep_weeks`` closed local weeks, oldest first, as label/bounds."""
    current = local_week_start(int(now_ms), tz)
    weeks: list[tuple[str, int, int]] = []
    for offset in range(max(1, int(keep_weeks)), 0, -1):
        start = current - timedelta(weeks=offset)
        end = current - timedelta(weeks=offset - 1)
        year, week, _ = start.isocalendar()
        weeks.append(
            (
                f"{year}-W{week:02d}",
                int(start.timestamp() * 1000),
                int(end.timestamp() * 1000),
            )
        )
    return tuple(weeks)


def iso_week_start_ms(label: str, tz: tzinfo | None = None) -> int | None:
    """Monday 00:00 of ``YYYY-Www`` in chat-local time, or ``None`` when unparsable."""
    year, _, week = str(label or "").partition("-W")
    try:
        start = datetime.fromisocalendar(int(year), int(week), 1)
    except ValueError:
        return None
    if tz is None:
        start = start.astimezone()
    else:
        start = start.replace(tzinfo=tz)
    return int(start.timestamp() * 1000)


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except TypeError, ValueError:
        return None


__all__ = [
    "PAST_SECTION",
    "PastSection",
    "closed_week_bounds",
    "iso_week_start_ms",
    "local_week_start",
    "prune_past_items",
]
