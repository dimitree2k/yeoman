"""Periodic collection: fetch every source, score what is new, keep the cards that pass."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from loguru import logger

from .article import ArticleReader
from .fit import TopicFitter, topics_key
from .models import TrendItem, TrendVerdict
from .scorer import TrendScorer
from .sources import GoogleTrendsSource, RssFeedSource, TrendSource
from .store import TrendStore
from .writer import LineWriter

#: Articles in other languages make lines the group cannot follow.
ARTICLE_LANGUAGES = frozenset({"de", "en", ""})


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


@dataclass(slots=True)
class TrendsReport:
    fetched: int = 0
    scored: int = 0
    cards: int = 0
    fitted_chats: int = 0
    articles_read: int = 0
    dropped: int = 0
    failed_sources: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "fetched": self.fetched,
            "scored": self.scored,
            "cards": self.cards,
            "fitted_chats": self.fitted_chats,
            "articles_read": self.articles_read,
            "dropped": self.dropped,
            "failed_sources": list(self.failed_sources),
        }


class TrendsService:
    def __init__(
        self,
        *,
        sources: Sequence[TrendSource],
        scorer: TrendScorer,
        store: TrendStore,
        interval_seconds: float = 3 * 3600,
        min_score: int = 7,
        card_ttl_ms: int = 72 * 3600 * 1000,
        max_items_per_run: int = 20,
        reader: ArticleReader | None = None,
        writer: LineWriter | None = None,
        max_article_age_ms: int = 48 * 3600 * 1000,
        fitter: TopicFitter | None = None,
        chats: Callable[[], Sequence[tuple[str, str, Any]]] | None = None,
        clock: Callable[[], int] = _now_ms,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self.sources = tuple(sources)
        self.scorer = scorer
        self.store = store
        self.interval_seconds = interval_seconds
        self.min_score = min_score
        self.card_ttl_ms = card_ttl_ms
        self.max_items_per_run = max_items_per_run
        self.reader = reader
        self.writer = writer
        self.max_article_age_ms = max_article_age_ms
        self.fitter = fitter
        self.chats = chats
        self.clock = clock
        self._task: asyncio.Task[None] | None = None

    async def run_once(self) -> TrendsReport:
        report = TrendsReport()
        items = []
        for source in self.sources:
            try:
                fetched = await source.fetch()
            except Exception as exc:  # noqa: BLE001 - one broken source must not stop the rest
                logger.warning("trends source failed source={} error_type={}",
                               source.name, type(exc).__name__)
                report.failed_sources.append(source.name)
                continue
            report.fetched += len(fetched)
            items.extend(fetched)
        fresh = self.store.unseen(items)[: self.max_items_per_run]
        now = self.clock()
        if fresh:
            verdicts = await self.scorer.score(fresh)
            report.scored = len(verdicts)
            verdicts = await self._rewrite_from_articles(fresh, verdicts, report, now_ms=now)
            cards = self.store.record(
                fresh, verdicts, min_score=self.min_score, now_ms=now, ttl_ms=self.card_ttl_ms
            )
            report.cards = len(cards)
        self.store.prune(now_ms=now)
        await self._fit_chats(report, now_ms=now)
        logger.info("trends run {}", report.as_dict())
        return report

    async def _fit_chats(self, report: TrendsReport, *, now_ms: int) -> None:
        """Check new cards against the topics of every chat with trend posts on."""
        if self.fitter is None or self.chats is None:
            return
        try:
            chats = list(self.chats())
        except Exception as exc:  # noqa: BLE001 - unreadable policy means no fitting now
            logger.warning("trends chats unavailable error_type={}", type(exc).__name__)
            return
        for channel, chat_id, policy in chats:
            topics, avoid = str(policy.topics or ""), str(policy.avoid or "")
            if not topics.strip():
                continue
            key = topics_key(topics, avoid)
            cards = self.store.unjudged_cards_for(channel, chat_id, key, now_ms=now_ms)
            if not cards:
                continue
            try:
                fitting = await self.fitter.fit(topics=topics, avoid=avoid, cards=cards)
            except Exception as exc:  # noqa: BLE001 - retried on the next run
                logger.warning("trends fit failed error_type={}", type(exc).__name__)
                continue
            self.store.record_fits(channel, chat_id, key, [c.card_id for c in cards], fitting)
            report.fitted_chats += 1

    async def _rewrite_from_articles(
        self,
        items: Sequence[TrendItem],
        verdicts: list[TrendVerdict],
        report: TrendsReport,
        *,
        now_ms: int,
    ) -> list[TrendVerdict]:
        """Read the article of every verdict at the bar and rewrite or drop it.

        Stale, foreign-language and unreadable articles are dropped: a line written from
        the headline alone too often misses what the story is about.
        """
        if self.reader is None or self.writer is None:
            return verdicts
        by_key = {item.key: item for item in items}
        rewritten: list[TrendVerdict] = []
        for verdict in verdicts:
            item = by_key.get(verdict.item_key)
            if item is None or verdict.score < self.min_score or not verdict.url:
                rewritten.append(verdict)
                continue
            article = await self.reader.read(verdict.url)
            if article is None:
                report.dropped += 1
                rewritten.append(replace(verdict, score=0, kind="unreadable", reason="article unreadable"))
                continue
            report.articles_read += 1
            published = article.published_at_ms
            if published is not None and now_ms - published > self.max_article_age_ms:
                report.dropped += 1
                rewritten.append(replace(verdict, score=0, kind="stale", reason="article too old"))
                continue
            if article.language not in ARTICLE_LANGUAGES:
                report.dropped += 1
                rewritten.append(replace(
                    verdict, score=0, kind="foreign", reason=f"article language {article.language}"
                ))
                continue
            try:
                written = await self.writer.write(item, verdict, article.text)
            except Exception as exc:  # noqa: BLE001 - one failed rewrite keeps the scored line
                logger.warning("trends line writer failed error_type={}", type(exc).__name__)
                written = verdict
            if written.score < self.min_score:
                report.dropped += 1
            rewritten.append(written)
        return rewritten

    async def _run(self) -> None:
        while True:
            try:
                await self.run_once()
            except Exception as exc:  # noqa: BLE001 - the loop outlives one bad pass
                logger.warning("trends run failed error_type={}", type(exc).__name__)
            await asyncio.sleep(self.interval_seconds)

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self.store.close()


def build_trends_service(
    config: Any,
    *,
    store_path: Any = None,
    chats: Callable[[], Sequence[tuple[str, str, Any]]] | None = None,
) -> TrendsService:
    """The configured service; raises ``RouteUnavailableError`` when the route is missing."""
    from yeoman_shared.utils.helpers import get_operational_store_path

    from yeoman_gateway.processing.model_route import RouteClient

    trends = config.trends
    client = RouteClient(config=config, route_key=trends.route)
    return TrendsService(
        sources=[
            GoogleTrendsSource(geo=trends.google_trends_geo),
            *(RssFeedSource(url=url) for url in trends.rss_feeds),
        ],
        scorer=TrendScorer(client=client, language=trends.language),
        reader=ArticleReader() if trends.read_articles else None,
        writer=LineWriter(client=client, language=trends.language) if trends.read_articles else None,
        max_article_age_ms=trends.max_article_age_hours * 3600 * 1000,
        store=TrendStore(store_path or get_operational_store_path("trends")),
        interval_seconds=trends.interval_minutes * 60,
        min_score=trends.min_score,
        card_ttl_ms=trends.card_ttl_hours * 3600 * 1000,
        max_items_per_run=trends.max_items_per_run,
        fitter=TopicFitter(client=client),
        chats=chats,
    )
