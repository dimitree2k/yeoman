"""Trend cards offered to quiet groups as owner-approved Participation material.

A chat whose policy carries an enabled ``trends`` block gets the best unused cards
that fit its topics when the conversation goes quiet. Each card enters Participation
as one source id (``trend:<card_id>``), so the judge, budgets, quiet hours and the
effect authorizer stay the single owner of whether anything is posted. A card is only
readable in the chat it was offered to.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from datetime import datetime, tzinfo
from typing import Any

from .fit import topics_key
from .models import TrendCard
from .store import TrendStore

TREND_SOURCE_PREFIX = "trend:"
#: The principal of a trend source: the owner, through the chat's trends policy.
TREND_SENDER = "trends:owner"
TREND_SENDER_NAME = "Trendkarte"
#: Cards offered per quiet moment; the judge picks at most one of them.
MAX_SEED_CARDS = 3
#: A chat gets new cards at most this often, however many quiet moments it has.
MIN_SEED_GAP_MS = 3 * 3600 * 1000
_CANDIDATE_POOL = 50
_TERM_SPLIT = re.compile(r"[,;\n]+")


def _minutes(clock: str) -> int:
    hours, _, minutes = clock.partition(":")
    return int(hours) * 60 + int(minutes or 0)


def is_trend_source(source_id: object) -> bool:
    return str(source_id or "").startswith(TREND_SOURCE_PREFIX)


def trend_source_id(card_id: str) -> str:
    return f"{TREND_SOURCE_PREFIX}{card_id}"


_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue"})


def _fold(text: str) -> str:
    """Case-insensitive German spelling: ``Fußball`` and ``fussball`` are one term."""
    return text.casefold().translate(_UMLAUTS)


def topic_terms(text: str) -> tuple[str, ...]:
    """Owner-written comma list as folded terms."""
    return tuple(
        dict.fromkeys(_fold(term.strip()) for term in _TERM_SPLIT.split(text or "") if term.strip())
    )


def _haystack(card: TrendCard) -> str:
    return _fold(" ".join((card.title, card.hook, card.line, " ".join(card.tags))))


def _hits(card: TrendCard, terms: tuple[str, ...]) -> int:
    haystack = _haystack(card)
    return sum(1 for term in terms if term in haystack)


def rank_cards(
    cards: list[TrendCard], *, topics: str, avoid: str, limit: int = MAX_SEED_CARDS
) -> list[TrendCard]:
    """Cards without an avoided term, topic matches first, then score and freshness.

    Keyword hits only order the offer; whether a card fits the group is the judge's
    call, which sees the owner's topics next to the card.
    """
    wanted = topic_terms(topics)
    blocked = topic_terms(avoid)
    allowed = [card for card in cards if not _hits(card, blocked)]
    allowed.sort(key=lambda card: (-_hits(card, wanted), -card.score, -card.created_at_ms))
    return allowed[: max(0, limit)]


def post_text(card: TrendCard) -> str:
    """The message a posted card sends: its line and the article link."""
    return f"{card.line.strip()}\n{card.url.strip()}".strip()


class TrendSeeds:
    """Picks, records and reads the trend cards offered to one chat."""

    def __init__(
        self,
        *,
        store: TrendStore,
        policy_engine: Callable[[], Any],
        clock: Callable[[], float] = time.time,
        post_from: str = "00:00",
        post_until: str = "24:00",
        max_posts_per_day: int | None = None,
        posts_since: Callable[[str, str, int], int] | None = None,
        tz: tzinfo | None = None,
    ) -> None:
        self._store = store
        self._policy_engine = policy_engine
        self._clock = clock
        self._post_from = _minutes(post_from)
        self._post_until = _minutes(post_until)
        self._max_posts_per_day = max_posts_per_day
        self._posts_since = posts_since
        self._tz = tz

    def _now_ms(self) -> int:
        return int(self._clock() * 1000)

    def _local(self, now_ms: int) -> datetime:
        # ``tz=None`` means the gateway's local time, which is the owner's.
        return datetime.fromtimestamp(now_ms / 1000, self._tz).astimezone(self._tz)

    def may_post_now(self, channel: str, chat_id: str, *, now_ms: int | None = None) -> bool:
        """Inside the posting hours and below today's trend post cap."""
        moment = self._now_ms() if now_ms is None else int(now_ms)
        local = self._local(moment)
        minute = local.hour * 60 + local.minute
        if not self._post_from <= minute < self._post_until:
            return False
        if self._max_posts_per_day is None or self._posts_since is None:
            return True
        midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
        posted = self._posts_since(channel, chat_id, int(midnight.timestamp() * 1000))
        return posted < self._max_posts_per_day

    def policy(self, channel: str, chat_id: str) -> Any | None:
        """The chat's enabled trends block, else ``None`` (unreadable policy counts as off)."""
        try:
            engine = self._policy_engine()
            resolved = engine.resolve_policy(channel, chat_id) if engine is not None else None
        except Exception:  # noqa: BLE001 - fail closed
            return None
        trends = getattr(resolved, "trends", None)
        return trends if trends is not None and bool(getattr(trends, "enabled", False)) else None

    def enabled(self, channel: str, chat_id: str) -> bool:
        return self.policy(channel, chat_id) is not None

    def candidates(self, channel: str, chat_id: str, *, now_ms: int | None = None) -> list[TrendCard]:
        """Unused cards for the chat; with topics set, only those the fit check accepted."""
        policy = self.policy(channel, chat_id)
        if policy is None:
            return []
        moment = self._now_ms() if now_ms is None else int(now_ms)
        pool = self._store.unused_cards_for(channel, chat_id, now_ms=moment, limit=_CANDIDATE_POOL)
        if policy.topics.strip():
            fitting = self._store.fitting_card_ids(
                channel, chat_id, topics_key(policy.topics, policy.avoid)
            )
            pool = [card for card in pool if card.card_id in fitting]
        return rank_cards(pool, topics=policy.topics, avoid=policy.avoid)

    def seed(self, channel: str, chat_id: str, *, now_ms: int | None = None) -> tuple[str, ...]:
        """Record the best cards as offered to this chat and return their source ids.

        An offered card is used up for the chat whether or not the judge posts it.
        """
        moment = self._now_ms() if now_ms is None else int(now_ms)
        if not self.may_post_now(channel, chat_id, now_ms=moment):
            return ()
        last = self._store.last_offer_ms(channel, chat_id)
        if last is not None and moment - last < MIN_SEED_GAP_MS:
            return ()
        cards = self.candidates(channel, chat_id, now_ms=moment)
        for card in cards:
            self._store.mark_used(card.card_id, channel, chat_id, now_ms=moment)
        return tuple(trend_source_id(card.card_id) for card in cards)

    def offered(self, channel: str, chat_id: str, source_id: str) -> tuple[TrendCard, int] | None:
        if not is_trend_source(source_id):
            return None
        policy = self.policy(channel, chat_id)
        if policy is None:
            return None
        return self._store.offered_card(
            str(source_id)[len(TREND_SOURCE_PREFIX):], channel, chat_id
        )

    def row(self, channel: str, chat_id: str, source_id: str) -> dict[str, object] | None:
        """The context row of an offered card, shaped like an archive row."""
        policy = self.policy(channel, chat_id)
        found = self.offered(channel, chat_id, source_id)
        if policy is None or found is None:
            return None
        card, offered_at_ms = found
        lines = [
            f"[Trendkarte, vom Owner für diese Gruppe freigegeben] {card.title}",
            f"Worum es geht: {card.hook}" if card.hook else "",
            f"Fertiger Post, falls gepostet: {post_text(card)}",
            f"Themen dieser Gruppe: {policy.topics}" if policy.topics.strip() else "",
            f"Nicht für diese Gruppe: {policy.avoid}" if policy.avoid.strip() else "",
        ]
        return {
            "message_id": str(source_id),
            "event_id": str(source_id),
            "channel": channel,
            "chat_id": chat_id,
            "direction": "in",
            "sender_id": TREND_SENDER,
            "sender_name": TREND_SENDER_NAME,
            "text": "\n".join(line for line in lines if line),
            "timestamp": offered_at_ms / 1000,
            "trend_post": post_text(card),
        }

    def sender(self, channel: str, chat_id: str, source_id: str) -> str | None:
        return TREND_SENDER if self.offered(channel, chat_id, source_id) is not None else None


def trend_chats(engine: Any) -> list[tuple[str, str, Any]]:
    """Every configured chat whose resolved policy has an enabled trends block."""
    policy = getattr(engine, "policy", None)
    channels = getattr(policy, "channels", None) or {}
    found: list[tuple[str, str, Any]] = []
    for channel, channel_policy in channels.items():
        for chat_id in getattr(channel_policy, "chats", {}) or {}:
            try:
                trends = engine.resolve_policy(channel, chat_id).trends
            except Exception:  # noqa: BLE001 - one unreadable chat must not hide the rest
                continue
            if trends.enabled:
                found.append((str(channel), str(chat_id), trends))
    return found


def build_trend_seeds(
    config: Any,
    policy_engine: Callable[[], Any],
    posts_since: Callable[[str, str, int], int] | None = None,
) -> TrendSeeds | None:
    """Seeds over the trends store when the collector is enabled."""
    trends = getattr(config, "trends", None)
    if not bool(getattr(trends, "enabled", False)):
        return None
    from yeoman_shared.utils.helpers import get_operational_store_path

    return TrendSeeds(
        store=TrendStore(get_operational_store_path("trends")),
        policy_engine=policy_engine,
        post_from=trends.post_from,
        post_until=trends.post_until,
        max_posts_per_day=trends.max_posts_per_day,
        posts_since=posts_since,
    )
