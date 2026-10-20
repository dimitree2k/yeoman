"""Periodic collection: fetch every source, score what is new, keep the cards that pass."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from .scorer import TrendScorer
from .sources import GoogleTrendsSource, TrendSource
from .store import TrendStore


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


@dataclass(slots=True)
class TrendsReport:
    fetched: int = 0
    scored: int = 0
    cards: int = 0
    failed_sources: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "fetched": self.fetched,
            "scored": self.scored,
            "cards": self.cards,
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
            cards = self.store.record(
                fresh, verdicts, min_score=self.min_score, now_ms=now, ttl_ms=self.card_ttl_ms
            )
            report.cards = len(cards)
        self.store.prune(now_ms=now)
        logger.info("trends run {}", report.as_dict())
        return report

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


def build_trends_service(config: Any, *, store_path: Any = None) -> TrendsService:
    """The configured service; raises ``RouteUnavailableError`` when the route is missing."""
    from yeoman_shared.utils.helpers import get_operational_store_path

    from yeoman_gateway.processing.model_route import RouteClient

    trends = config.trends
    client = RouteClient(config=config, route_key=trends.route)
    return TrendsService(
        sources=[GoogleTrendsSource(geo=trends.google_trends_geo)],
        scorer=TrendScorer(client=client, language=trends.language),
        store=TrendStore(store_path or get_operational_store_path("trends")),
        interval_seconds=trends.interval_minutes * 60,
        min_score=trends.min_score,
        card_ttl_ms=trends.card_ttl_hours * 3600 * 1000,
        max_items_per_run=trends.max_items_per_run,
    )
