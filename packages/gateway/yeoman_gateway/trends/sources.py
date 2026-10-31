"""Where trend items come from. Each source is one small class behind ``TrendSource``."""

from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Protocol
from urllib.parse import urlsplit

import httpx

from .models import Headline, TrendItem

#: Feeds are a few kilobytes; anything far larger is not the feed we expect.
MAX_FEED_BYTES = 1_000_000
_HT = "{https://trends.google.com/trending/rss}"
_ATOM = "{http://www.w3.org/2005/Atom}"
#: Newest items taken from one news feed per run; feeds list 20-40 stories.
MAX_ITEMS_PER_FEED = 10


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


class RssFeedSource:
    """One news feed (RSS 2.0 or Atom): every story is its own topic with its link."""

    def __init__(
        self, *, url: str, timeout_seconds: float = 15.0, max_items: int = MAX_ITEMS_PER_FEED
    ) -> None:
        self.url = str(url).strip()
        host = urlsplit(self.url).hostname or "feed"
        self.name = f"rss:{host.removeprefix('www.')}"
        self._timeout = timeout_seconds
        self._max_items = max_items

    async def fetch(self) -> list[TrendItem]:
        async with httpx.AsyncClient(
            timeout=self._timeout,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; yeoman-trends)"},
        ) as client:
            response = await client.get(self.url)
            response.raise_for_status()
        if len(response.content) > MAX_FEED_BYTES:
            raise ValueError("news feed too large")
        items = parse_news_feed(response.content, source=self.name)
        items.sort(key=lambda item: item.published_at_ms, reverse=True)
        return items[: self._max_items]


def parse_news_feed(payload: bytes | str, *, source: str) -> list[TrendItem]:
    """Stories of one RSS or Atom document; stories without title or link are skipped."""
    root = ET.fromstring(payload)
    outlet = source.removeprefix("rss:")
    stories: list[tuple[str, str, int]] = []
    for node in root.iter("item"):
        stories.append((
            node.findtext("title") or "",
            (node.findtext("link") or "").strip(),
            _published_ms(node.findtext("pubDate")),
        ))
    for node in root.iter(f"{_ATOM}entry"):
        link = node.find(f"{_ATOM}link")
        stories.append((
            node.findtext(f"{_ATOM}title") or "",
            (link.get("href") or "").strip() if link is not None else "",
            _iso_ms(node.findtext(f"{_ATOM}published") or node.findtext(f"{_ATOM}updated")),
        ))
    items: list[TrendItem] = []
    for raw_title, url, published in stories:
        title = " ".join(raw_title.split())
        if not title or not url.startswith(("https://", "http://")):
            continue
        items.append(
            TrendItem(
                source=source,
                key=f"{source}:{url}",
                title=title,
                published_at_ms=published,
                headlines=(Headline(title=title, url=url, outlet=outlet),),
            )
        )
    return items


def _iso_ms(value: str | None) -> int:
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() * 1000)
    except (TypeError, ValueError):
        return int(time.time() * 1000)


def _published_ms(value: str | None) -> int:
    try:
        return int(parsedate_to_datetime(str(value)).timestamp() * 1000)
    except (TypeError, ValueError, IndexError):
        return int(time.time() * 1000)
