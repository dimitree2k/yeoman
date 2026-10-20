"""Where trend items come from. Each source is one small class behind ``TrendSource``."""

from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from typing import Protocol

import httpx

from .models import Headline, TrendItem

#: Feeds are a few kilobytes; anything far larger is not the feed we expect.
MAX_FEED_BYTES = 1_000_000
_HT = "{https://trends.google.com/trending/rss}"


class TrendSource(Protocol):
    """One place to read fresh topics from."""

    name: str

    async def fetch(self) -> list[TrendItem]: ...


class GoogleTrendsSource:
    """Google's public "Trending now" RSS feed for one country."""

    def __init__(self, *, geo: str = "DE", timeout_seconds: float = 15.0) -> None:
        self.geo = str(geo or "DE").strip().upper()
        self.name = f"google_trends:{self.geo}"
        self._timeout = timeout_seconds

    @property
    def url(self) -> str:
        return f"https://trends.google.com/trending/rss?geo={self.geo}"

    async def fetch(self) -> list[TrendItem]:
        async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=True) as client:
            response = await client.get(self.url)
            response.raise_for_status()
        if len(response.content) > MAX_FEED_BYTES:
            raise ValueError("google trends feed too large")
        return parse_google_trends(response.content, source=self.name)


def parse_google_trends(payload: bytes | str, *, source: str) -> list[TrendItem]:
    """Items of one Google Trends RSS document; malformed items are skipped."""
    root = ET.fromstring(payload)
    items: list[TrendItem] = []
    for node in root.iter("item"):
        title = " ".join((node.findtext("title") or "").split())
        if not title:
            continue
        headlines = tuple(
            Headline(
                title=" ".join((news.findtext(f"{_HT}news_item_title") or "").split()),
                url=(news.findtext(f"{_HT}news_item_url") or "").strip(),
                outlet=(news.findtext(f"{_HT}news_item_source") or "").strip(),
            )
            for news in node.iter(f"{_HT}news_item")
        )
        items.append(
            TrendItem(
                source=source,
                key=f"{source}:{title.casefold()}",
                title=title,
                published_at_ms=_published_ms(node.findtext("pubDate")),
                traffic=(node.findtext(f"{_HT}approx_traffic") or "").strip(),
                headlines=tuple(item for item in headlines if item.title and item.url),
            )
        )
    return items


def _published_ms(value: str | None) -> int:
    try:
        return int(parsedate_to_datetime(str(value)).timestamp() * 1000)
    except (TypeError, ValueError, IndexError):
        return int(time.time() * 1000)
