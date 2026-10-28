"""Read the article behind a card: its text, publish date and language.

Only topics that already passed the scorer are read, a handful a day. The fetch uses
the web tool's SSRF checks on every hop, a size cap and HTML only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import httpx
from loguru import logger

from yeoman_gateway.agent.tools.web import USER_AGENT, _async_validate_dns, _validate_url

MAX_ARTICLE_BYTES = 2_000_000
MAX_REDIRECTS = 5
MAX_TEXT_CHARS = 6000

_DATE_META = (
    "article:published_time",
    "og:article:published_time",
    "datePublished",
    "pubdate",
    "publishdate",
    "date",
    "dc.date",
    "article:modified_time",
    "og:updated_time",
)


@dataclass(frozen=True, slots=True)
class Article:
    text: str
    published_at_ms: int | None = None
    language: str = ""


class ArticleReader:
    def __init__(self, *, timeout_seconds: float = 15.0) -> None:
        self._timeout = timeout_seconds

    async def read(self, url: str) -> Article | None:
        """The article, or ``None`` when it cannot be fetched or holds no text."""
        try:
            html = await self._fetch(url)
        except Exception as exc:  # noqa: BLE001 - a missing article only weakens one card
            logger.info("trends article fetch failed host={} error_type={}",
                        urlparse(url).hostname, type(exc).__name__)
            return None
        return parse_article(html) if html else None

    async def _fetch(self, url: str) -> str | None:
        async with httpx.AsyncClient(follow_redirects=False, timeout=self._timeout) as client:
            next_url = url
            for _ in range(MAX_REDIRECTS + 1):
                ok, error = _validate_url(next_url)
                if not ok:
                    raise ValueError(error)
                await _async_validate_dns((urlparse(next_url).hostname or "").lower())
                async with client.stream("GET", next_url, headers={"User-Agent": USER_AGENT}) as r:
                    if r.status_code in {301, 302, 303, 307, 308} and r.headers.get("location"):
                        next_url = str(r.url.join(r.headers["location"]))
                        continue
                    r.raise_for_status()
                    if "html" not in r.headers.get("content-type", "").lower():
                        return None
                    body = bytearray()
                    async for chunk in r.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > MAX_ARTICLE_BYTES:
                            raise ValueError("article too large")
                    return body.decode(r.encoding or "utf-8", errors="replace")
        raise ValueError("too many redirects")


def parse_article(html: str) -> Article | None:
    from lxml import html as lxml_html
    from readability import Document

    try:
        tree = lxml_html.fromstring(html)
    except Exception:  # noqa: BLE001 - unparseable pages carry no article
        return None
    try:
        summary = Document(html).summary(html_partial=True)
        text = lxml_html.fromstring(summary).text_content()
    except Exception:  # noqa: BLE001
        text = ""
    text = " ".join(text.split())[:MAX_TEXT_CHARS]
    if not text:
        return None
    language = str(tree.get("lang") or "").strip().lower()[:2]
    return Article(text=text, published_at_ms=_published_ms(tree), language=language)


def _published_ms(tree: object) -> int | None:
    xpath = getattr(tree, "xpath")
    for key in _DATE_META:
        for value in xpath(f'//meta[@property="{key}" or @name="{key}" or @itemprop="{key}"]/@content'):
            parsed = _parse_date(value)
            if parsed is not None:
                return parsed
    for raw in xpath('//script[@type="application/ld+json"]/text()'):
        match = re.search(r'"datePublished"\s*:\s*"([^"]+)"', raw)
        if match and (parsed := _parse_date(match.group(1))) is not None:
            return parsed
    for value in xpath("//time/@datetime"):
        parsed = _parse_date(value)
        if parsed is not None:
            return parsed
    return None


def _parse_date(value: object) -> int | None:
    """ISO 8601 (full or date only) or RFC 2822, as epoch milliseconds."""
    text = str(value or "").strip()
    if not text:
        return None
    moment: datetime | None = None
    for candidate in (text.replace("Z", "+00:00"), text[:10]):
        try:
            moment = datetime.fromisoformat(candidate)
            break
        except ValueError:
            continue
    if moment is None:
        try:
            moment = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError):
            return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return int(moment.timestamp() * 1000)
