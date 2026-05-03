"""Web tools: web_search, web_fetch, and deep_research (all powered by Tavily)."""

import asyncio
import html
import ipaddress
import json
import os
import re
import socket
import time
from collections import deque
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import httpx
from loguru import logger

from yeoman_gateway.agent.tools.base import Tool

if TYPE_CHECKING:
    from yeoman_shared.config.schema import WebToolsConfig

# Shared constants
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7_2) AppleWebKit/537.36"
MAX_REDIRECTS = 5  # Limit redirects to prevent DoS attacks
_LOCAL_HOSTS = {"localhost", "localhost.localdomain"}
_TAVILY_SEARCH_URL = "https://api.tavily.com/search"
_TAVILY_EXTRACT_URL = "https://api.tavily.com/extract"
_TAVILY_MAP_URL = "https://api.tavily.com/map"
_TAVILY_CRAWL_URL = "https://api.tavily.com/crawl"


class _WebRateLimiter:
    """Sliding-window rate limiter for web tool calls."""

    def __init__(self, max_requests: int = 20, window_seconds: float = 60.0):
        self._timestamps: deque[float] = deque()
        self._max = max_requests
        self._window = window_seconds

    def check(self) -> bool:
        now = time.monotonic()
        while self._timestamps and now - self._timestamps[0] > self._window:
            self._timestamps.popleft()
        if len(self._timestamps) >= self._max:
            return False
        self._timestamps.append(now)
        return True

    def configure(self, max_requests: int, window_seconds: float = 60.0) -> None:
        self._max = max_requests
        self._window = window_seconds


# Module-level singleton shared across all web tool instances
_rate_limiter = _WebRateLimiter()


def _strip_tags(text: str) -> str:
    """Remove HTML tags and decode entities."""
    text = re.sub(r"<script[\s\S]*?</script>", "", text, flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", "", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def _normalize(text: str) -> str:
    """Normalize whitespace."""
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _is_private_ip(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _host_resolves_private(hostname: str) -> bool:
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return False
    except Exception:
        return False
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        ip = sockaddr[0]
        if _is_private_ip(ip):
            return True
    return False


def _validate_domain(host: str, blocked: list[str], allowed: list[str]) -> tuple[bool, str]:
    """Check host against blocked/allowed domain lists. Matches subdomains."""
    h = host.lower().strip(".")
    for d in blocked:
        d = d.lower().strip(".")
        if h == d or h.endswith("." + d):
            return False, f"Blocked domain: {host}"
    if allowed:
        for d in allowed:
            d = d.lower().strip(".")
            if h == d or h.endswith("." + d):
                return True, ""
        return False, f"Domain not in allowed list: {host}"
    return True, ""


def _validate_url(
    url: str,
    blocked_domains: list[str] | None = None,
    allowed_domains: list[str] | None = None,
) -> tuple[bool, str]:
    """Validate URL and block SSRF to local/private targets."""
    try:
        p = urlparse(url)
        if p.scheme not in ("http", "https"):
            return False, f"Only http/https allowed, got '{p.scheme or 'none'}'"
        if not p.netloc:
            return False, "Missing domain"

        host = (p.hostname or "").strip().lower()
        if not host:
            return False, "Missing hostname"
        if host in _LOCAL_HOSTS or host.endswith(".local"):
            return False, f"Blocked local host target: {host}"
        if _is_private_ip(host):
            return False, f"Blocked private IP target: {host}"
        if _host_resolves_private(host):
            return False, f"Blocked private-network DNS target: {host}"

        # Domain allowlist/blocklist
        ok, err = _validate_domain(
            host,
            blocked=blocked_domains or [],
            allowed=allowed_domains or [],
        )
        if not ok:
            return False, err

        return True, ""
    except Exception as e:
        return False, str(e)


async def _async_validate_dns(hostname: str) -> None:
    """Async DNS resolve + private-IP validation. Raises ValueError on failure.

    Called immediately before httpx request to minimize TOCTOU window.
    Note: a theoretical gap remains because httpx resolves DNS independently.
    Eliminating it entirely would require a custom httpcore network backend.
    """
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(hostname, None)
    except socket.gaierror as e:
        raise ValueError(f"DNS resolution failed for {hostname}: {e}") from e

    if not infos:
        raise ValueError(f"DNS resolution returned no results for {hostname}")

    for family, type_, proto, canonname, sockaddr in infos:
        ip = sockaddr[0]
        if _is_private_ip(ip):
            raise ValueError(f"DNS rebinding blocked: {hostname} resolved to private IP {ip}")


def _tavily_auth_headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


def _add_optional(payload: dict[str, Any], **values: Any) -> dict[str, Any]:
    for key, value in values.items():
        if value is None:
            continue
        if value == "" or value == []:
            continue
        payload[key] = value
    return payload


class WebSearchTool(Tool):
    """Search the web using Tavily Search API."""

    name = "web_search"
    description = (
        "Search the web with Tavily. Supports recency, news/finance topics, domain filters, "
        "and depth controls. Returns titles, URLs, snippets, and optionally an AI answer."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query"},
            "count": {
                "type": "integer",
                "description": "Backward-compatible alias for max_results",
                "minimum": 1,
                "maximum": 20,
            },
            "max_results": {
                "type": "integer",
                "description": "Results (1-20)",
                "minimum": 1,
                "maximum": 20,
            },
            "search_depth": {
                "type": "string",
                "enum": ["basic", "advanced", "fast", "ultra-fast"],
                "description": "Tavily depth/speed mode. advanced costs more and may be slower.",
            },
            "topic": {
                "type": "string",
                "enum": ["general", "news", "finance"],
                "description": "Search category.",
            },
            "chunks_per_source": {
                "type": "integer",
                "description": "Relevant chunks per result when supported by search_depth.",
                "minimum": 1,
                "maximum": 3,
            },
            "time_range": {
                "type": "string",
                "enum": ["day", "week", "month", "year", "d", "w", "m", "y"],
                "description": "Relative publish/update time filter.",
            },
            "days": {
                "type": "integer",
                "description": "Days back for news searches.",
                "minimum": 1,
            },
            "start_date": {
                "type": "string",
                "description": "Start date filter in YYYY-MM-DD format.",
            },
            "end_date": {
                "type": "string",
                "description": "End date filter in YYYY-MM-DD format.",
            },
            "include_answer": {
                "type": "boolean",
                "description": "Include Tavily's generated answer.",
            },
            "include_raw_content": {
                "type": "string",
                "enum": ["markdown", "text"],
                "description": "Include extracted source content. Increases latency and output size.",
            },
            "include_images": {
                "type": "boolean",
                "description": "Include image search results.",
            },
            "include_image_descriptions": {
                "type": "boolean",
                "description": "Describe included images.",
            },
            "include_favicon": {
                "type": "boolean",
                "description": "Include favicon URLs.",
            },
            "include_usage": {
                "type": "boolean",
                "description": "Include Tavily credit usage metadata.",
            },
            "include_domains": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Domains to include.",
            },
            "exclude_domains": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Domains to exclude.",
            },
            "country": {
                "type": "string",
                "description": "Country boost for general searches, e.g. 'united states'.",
            },
            "auto_parameters": {
                "type": "boolean",
                "description": "Let Tavily infer parameters. May increase credits if it chooses advanced.",
            },
            "exact_match": {
                "type": "boolean",
                "description": "Require quoted phrases to match exactly.",
            },
        },
        "required": ["query"],
    }

    def __init__(
        self,
        api_key: str | None = None,
        max_results: int = 5,
        web_config: "WebToolsConfig | None" = None,
    ):
        from yeoman_shared.config.schema import WebToolsConfig as WebToolsCfg

        self._config = web_config or WebToolsCfg()
        self.api_key = api_key or os.environ.get("TAVILY_API_KEY", "")
        self.max_results = max_results
        _rate_limiter.configure(self._config.rate_limit_rpm)

    async def execute(
        self,
        query: str,
        count: int | None = None,
        max_results: int | None = None,
        search_depth: str = "basic",
        topic: str | None = None,
        chunks_per_source: int | None = None,
        time_range: str | None = None,
        days: int | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        include_answer: bool = True,
        include_raw_content: str | None = None,
        include_images: bool | None = None,
        include_image_descriptions: bool | None = None,
        include_favicon: bool | None = None,
        include_usage: bool | None = None,
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
        country: str | None = None,
        auto_parameters: bool | None = None,
        exact_match: bool | None = None,
        **kwargs: Any,
    ) -> str:
        del kwargs
        requested_results = max_results if max_results is not None else count
        logger.info("web_search query={!r} count={}", query, requested_results or self.max_results)
        if not _rate_limiter.check():
            return "Error: Rate limit exceeded. Try again shortly."

        if not self.api_key:
            return "Error: TAVILY_API_KEY not configured"

        try:
            n = min(max(requested_results or self.max_results, 1), 20)
            payload: dict[str, Any] = {
                "query": query,
                "search_depth": search_depth,
                "max_results": n,
                "include_answer": include_answer,
            }
            _add_optional(
                payload,
                topic=topic,
                chunks_per_source=chunks_per_source,
                time_range=time_range,
                days=days,
                start_date=start_date,
                end_date=end_date,
                include_raw_content=include_raw_content,
                include_images=include_images,
                include_image_descriptions=include_image_descriptions,
                include_favicon=include_favicon,
                include_usage=include_usage,
                include_domains=include_domains,
                exclude_domains=exclude_domains,
                country=country,
                auto_parameters=auto_parameters,
                exact_match=exact_match,
            )
            async with httpx.AsyncClient() as client:
                r = await client.post(
                    _TAVILY_SEARCH_URL,
                    json=payload,
                    headers=_tavily_auth_headers(self.api_key),
                    timeout=15.0,
                )
                r.raise_for_status()

            data = r.json()
            results = data.get("results", [])
            if not results:
                return f"No results for: {query}"

            lines = [f"Results for: {query}\n"]
            if answer := data.get("answer"):
                lines.append(f"Answer: {answer}\n")
            for i, item in enumerate(results[:n], 1):
                lines.append(f"{i}. {item.get('title', '')}\n   {item.get('url', '')}")
                if snippet := item.get("content"):
                    lines.append(f"   {snippet[:300]}")
            return "\n".join(lines)
        except Exception as e:
            return f"Error: {e}"


class WebFetchTool(Tool):
    """Fetch and extract content from a URL.

    Uses Tavily Extract when an API key is available (handles JS-heavy and paywalled pages),
    falls back to direct HTTP fetch with Readability extraction.
    """

    name = "web_fetch"
    description = (
        "Fetch URL and extract readable content (HTML → markdown/text). "
        "Not suitable for JS-heavy apps (Google Maps, Twitter/X, Instagram, SPAs) — use browse instead."
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL to fetch"},
            "extract_mode": {"type": "string", "enum": ["markdown", "text"], "default": "markdown"},
            "max_chars": {"type": "integer", "minimum": 100},
            "query": {
                "type": "string",
                "description": "Optional query for Tavily query-focused extraction.",
            },
            "chunks_per_source": {
                "type": "integer",
                "description": "Relevant chunks to return when query is provided.",
                "minimum": 1,
                "maximum": 5,
            },
            "extract_depth": {
                "type": "string",
                "enum": ["basic", "advanced"],
                "description": "Tavily extraction depth. advanced is slower and costs more.",
            },
            "include_images": {
                "type": "boolean",
                "description": "Include extracted image URLs.",
            },
            "include_favicon": {
                "type": "boolean",
                "description": "Include favicon URL.",
            },
            "include_usage": {
                "type": "boolean",
                "description": "Include Tavily usage metadata.",
            },
        },
        "required": ["url"],
    }

    def __init__(
        self,
        api_key: str | None = None,
        max_chars: int = 50000,
        web_config: "WebToolsConfig | None" = None,
    ):
        from yeoman_shared.config.schema import WebToolsConfig as WebToolsCfg

        self._config = web_config or WebToolsCfg()
        self.api_key = api_key or os.environ.get("TAVILY_API_KEY", "")
        self.max_chars = max_chars
        self._max_fetch_bytes = self._config.max_fetch_bytes
        self._allowed_content_types = self._config.allowed_content_types
        self._blocked_domains = self._config.blocked_domains
        self._allowed_domains = self._config.allowed_domains
        _rate_limiter.configure(self._config.rate_limit_rpm)

    def _is_allowed_content_type(self, ctype: str) -> bool:
        """Check if response content-type matches allowed prefixes."""
        if not ctype:
            return True  # Allow missing content-type (best-effort)
        ctype_lower = ctype.lower().split(";")[0].strip()
        return any(ctype_lower.startswith(allowed) for allowed in self._allowed_content_types)

    async def execute(
        self,
        url: str,
        extract_mode: str = "markdown",
        max_chars: int | None = None,
        query: str | None = None,
        chunks_per_source: int | None = None,
        extract_depth: str | None = None,
        include_images: bool | None = None,
        include_favicon: bool | None = None,
        include_usage: bool | None = None,
        **kwargs: Any,
    ) -> str:
        del kwargs
        logger.info("web_fetch url={!r} mode={}", url, extract_mode)
        if not _rate_limiter.check():
            return json.dumps({"error": "Rate limit exceeded. Try again shortly.", "url": url})

        max_chars = max_chars or self.max_chars

        # Validate URL before fetching
        is_valid, error_msg = _validate_url(
            url,
            blocked_domains=self._blocked_domains,
            allowed_domains=self._allowed_domains,
        )
        if not is_valid:
            return json.dumps({"error": f"URL validation failed: {error_msg}", "url": url})

        # YouTube pages don't yield transcripts via web_fetch — redirect to the dedicated tool
        if "youtube.com" in url or "youtu.be" in url:
            return json.dumps(
                {
                    "error": "YouTube URLs cannot be fetched this way — web_fetch only gets page HTML, not video transcripts.",
                    "action": f'Use the youtube_transcript tool instead: youtube_transcript(url="{url}")',
                }
            )

        # Try Tavily Extract first (handles JS-heavy pages and paywall content better)
        if self.api_key:
            try:
                result = await self._tavily_extract(
                    url,
                    max_chars,
                    query=query,
                    chunks_per_source=chunks_per_source,
                    extract_depth=extract_depth,
                    extract_mode=extract_mode,
                    include_images=include_images,
                    include_favicon=include_favicon,
                    include_usage=include_usage,
                )
                if result is not None:
                    return result
            except Exception:
                pass  # Fall through to direct fetch

        # Fallback: direct fetch with Readability
        return await self._direct_fetch(url, extract_mode, max_chars)

    async def _tavily_extract(
        self,
        url: str,
        max_chars: int,
        *,
        query: str | None = None,
        chunks_per_source: int | None = None,
        extract_depth: str | None = None,
        extract_mode: str = "markdown",
        include_images: bool | None = None,
        include_favicon: bool | None = None,
        include_usage: bool | None = None,
    ) -> str | None:
        """Extract content via Tavily Extract API. Returns None on failure."""
        payload: dict[str, Any] = {"urls": [url]}
        _add_optional(
            payload,
            query=query,
            chunks_per_source=chunks_per_source,
            extract_depth=extract_depth,
            format=extract_mode,
            include_images=include_images,
            include_favicon=include_favicon,
            include_usage=include_usage,
        )
        async with httpx.AsyncClient() as client:
            r = await client.post(
                _TAVILY_EXTRACT_URL,
                json=payload,
                headers=_tavily_auth_headers(self.api_key),
                timeout=30.0,
            )
            if r.status_code != 200:
                return None

        data = r.json()
        results = data.get("results", [])
        if not results:
            return None

        item = results[0]
        raw_content = item.get("raw_content") or item.get("content") or ""
        if not raw_content:
            return None

        truncated = len(raw_content) > max_chars
        text = raw_content[:max_chars] if truncated else raw_content
        return json.dumps(
            {
                "url": url,
                "finalUrl": url,
                "extractor": "tavily",
                "truncated": truncated,
                "length": len(text),
                "text": text,
            }
        )

    async def _direct_fetch(self, url: str, extract_mode: str, max_chars: int) -> str:
        """Direct HTTP fetch with streaming size guard + content-type filter."""
        from readability import Document

        try:
            async with httpx.AsyncClient(
                follow_redirects=False,
                timeout=30.0,
            ) as client:
                next_url = url
                redirects = 0
                final_status = 200
                final_url = url
                while True:
                    is_valid, error_msg = _validate_url(
                        next_url,
                        blocked_domains=self._blocked_domains,
                        allowed_domains=self._allowed_domains,
                    )
                    if not is_valid:
                        return json.dumps(
                            {"error": f"URL validation failed: {error_msg}", "url": next_url}
                        )

                    # Async DNS validation (minimize TOCTOU)
                    parsed = urlparse(next_url)
                    hostname = (parsed.hostname or "").strip().lower()
                    if hostname and not _is_private_ip(hostname):
                        try:
                            await _async_validate_dns(hostname)
                        except ValueError as e:
                            return json.dumps({"error": str(e), "url": next_url})

                    async with client.stream(
                        "GET", next_url, headers={"User-Agent": USER_AGENT}
                    ) as r:
                        if r.status_code in {301, 302, 303, 307, 308} and "location" in r.headers:
                            redirects += 1
                            if redirects > MAX_REDIRECTS:
                                return json.dumps({"error": "Too many redirects", "url": url})
                            location = r.headers.get("location", "")
                            if not location:
                                return json.dumps(
                                    {
                                        "error": "Redirect missing location header",
                                        "url": next_url,
                                    }
                                )
                            next_url = str(r.url.join(location))
                            continue

                        r.raise_for_status()

                        # Content-type filter
                        ctype = r.headers.get("content-type", "")
                        if not self._is_allowed_content_type(ctype):
                            logger.warning(
                                "web_fetch error url={!r} err={!r}",
                                url,
                                f"Blocked content type: {ctype}",
                            )
                            return json.dumps(
                                {
                                    "error": f"Blocked content type: {ctype}",
                                    "url": next_url,
                                }
                            )

                        # Streaming size guard: check Content-Length header
                        content_length = r.headers.get("content-length")
                        if content_length and int(content_length) > self._max_fetch_bytes:
                            logger.warning(
                                "web_fetch error url={!r} err={!r}",
                                url,
                                f"Response too large: {content_length} bytes",
                            )
                            return json.dumps(
                                {
                                    "error": (
                                        f"Response too large: {content_length} bytes"
                                        f" (limit {self._max_fetch_bytes})"
                                    ),
                                    "url": next_url,
                                }
                            )

                        # Read body in chunks up to max_fetch_bytes
                        chunks: list[bytes] = []
                        total = 0
                        async for chunk in r.aiter_bytes(4096):
                            total += len(chunk)
                            if total > self._max_fetch_bytes:
                                break
                            chunks.append(chunk)

                        raw_bytes = b"".join(chunks)
                        body_text = raw_bytes.decode("utf-8", errors="replace")
                        final_status = r.status_code
                        final_url = str(r.url)
                        break

            # Parse based on content type
            if "application/json" in ctype:
                text, extractor = json.dumps(json.loads(body_text), indent=2), "json"
            elif "text/html" in ctype or body_text[:256].lower().startswith(("<!doctype", "<html")):
                doc = Document(body_text)
                content = (
                    self._to_markdown(doc.summary())
                    if extract_mode == "markdown"
                    else _strip_tags(doc.summary())
                )
                text = f"# {doc.title()}\n\n{content}" if doc.title() else content
                extractor = "readability"
            else:
                text, extractor = body_text, "raw"

            truncated = len(text) > max_chars
            if truncated:
                text = text[:max_chars]

            logger.info("web_fetch ok url={!r} extractor={} len={}", url, extractor, len(text))
            return json.dumps(
                {
                    "url": url,
                    "finalUrl": final_url,
                    "status": final_status,
                    "extractor": extractor,
                    "truncated": truncated,
                    "length": len(text),
                    "text": text,
                }
            )
        except Exception as e:
            logger.warning("web_fetch error url={!r} err={!r}", url, str(e))
            return json.dumps({"error": str(e), "url": url})

    def _to_markdown(self, html_text: str) -> str:
        """Convert HTML to markdown."""
        # Convert links, headings, lists before stripping tags
        text = re.sub(
            r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>([\s\S]*?)</a>',
            lambda m: f"[{_strip_tags(m[2])}]({m[1]})",
            html_text,
            flags=re.I,
        )
        text = re.sub(
            r"<h([1-6])[^>]*>([\s\S]*?)</h\1>",
            lambda m: f"\n{'#' * int(m[1])} {_strip_tags(m[2])}\n",
            text,
            flags=re.I,
        )
        text = re.sub(
            r"<li[^>]*>([\s\S]*?)</li>", lambda m: f"\n- {_strip_tags(m[1])}", text, flags=re.I
        )
        text = re.sub(r"</(p|div|section|article)>", "\n\n", text, flags=re.I)
        text = re.sub(r"<(br|hr)\s*/?>", "\n", text, flags=re.I)
        return _normalize(_strip_tags(text))


class _TavilySiteTool(Tool):
    def __init__(self, api_key: str | None = None, web_config: "WebToolsConfig | None" = None):
        from yeoman_shared.config.schema import WebToolsConfig as WebToolsCfg

        self._config = web_config or WebToolsCfg()
        self.api_key = api_key or os.environ.get("TAVILY_API_KEY", "")
        self._blocked_domains = self._config.blocked_domains
        self._allowed_domains = self._config.allowed_domains
        _rate_limiter.configure(self._config.rate_limit_rpm)

    async def _post_site_request(
        self, endpoint: str, payload: dict[str, Any], timeout: float
    ) -> str:
        if not _rate_limiter.check():
            return json.dumps({"error": "Rate limit exceeded. Try again shortly.", "url": payload.get("url")})
        if not self.api_key:
            return json.dumps({"error": "TAVILY_API_KEY not configured", "url": payload.get("url")})

        url = str(payload.get("url") or "")
        is_valid, error_msg = _validate_url(
            url,
            blocked_domains=self._blocked_domains,
            allowed_domains=self._allowed_domains,
        )
        if not is_valid:
            return json.dumps({"error": f"URL validation failed: {error_msg}", "url": url})

        async with httpx.AsyncClient() as client:
            r = await client.post(
                endpoint,
                json=payload,
                headers=_tavily_auth_headers(self.api_key),
                timeout=timeout,
            )
            r.raise_for_status()
        return json.dumps(r.json())


class WebMapTool(_TavilySiteTool):
    """Discover URLs on a website using Tavily Map."""

    name = "web_map"
    description = (
        "Map a website with Tavily to discover URLs without extracting page content. "
        "Use before crawl/extract when you need a site overview or URL list."
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Root URL to map"},
            "instructions": {"type": "string", "description": "Natural language mapping focus"},
            "max_depth": {"type": "integer", "minimum": 1, "maximum": 5},
            "max_breadth": {"type": "integer", "minimum": 1, "maximum": 500},
            "limit": {"type": "integer", "minimum": 1},
            "select_paths": {"type": "array", "items": {"type": "string"}},
            "select_domains": {"type": "array", "items": {"type": "string"}},
            "exclude_paths": {"type": "array", "items": {"type": "string"}},
            "exclude_domains": {"type": "array", "items": {"type": "string"}},
            "allow_external": {"type": "boolean"},
            "timeout": {"type": "number", "minimum": 10, "maximum": 150},
            "include_usage": {"type": "boolean"},
        },
        "required": ["url"],
    }

    async def execute(
        self,
        url: str,
        instructions: str | None = None,
        max_depth: int | None = None,
        max_breadth: int | None = None,
        limit: int | None = None,
        select_paths: list[str] | None = None,
        select_domains: list[str] | None = None,
        exclude_paths: list[str] | None = None,
        exclude_domains: list[str] | None = None,
        allow_external: bool | None = None,
        timeout: float | None = None,
        include_usage: bool | None = None,
        **kwargs: Any,
    ) -> str:
        del kwargs
        logger.info("web_map url={!r} limit={}", url, limit)
        payload: dict[str, Any] = {"url": url}
        _add_optional(
            payload,
            instructions=instructions,
            max_depth=max_depth,
            max_breadth=max_breadth,
            limit=limit,
            select_paths=select_paths,
            select_domains=select_domains,
            exclude_paths=exclude_paths,
            exclude_domains=exclude_domains,
            allow_external=allow_external,
            include_usage=include_usage,
        )
        return await self._post_site_request(_TAVILY_MAP_URL, payload, timeout or 60.0)


class WebCrawlTool(_TavilySiteTool):
    """Crawl and extract website content using Tavily Crawl."""

    name = "web_crawl"
    description = (
        "Crawl a website with Tavily and extract page content. Use for documentation ingestion, "
        "site research, or focused multi-page extraction. Prefer web_map first for broad sites."
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Root URL to crawl"},
            "instructions": {"type": "string", "description": "Natural language crawl focus"},
            "chunks_per_source": {"type": "integer", "minimum": 1, "maximum": 5},
            "max_depth": {"type": "integer", "minimum": 1, "maximum": 5},
            "max_breadth": {"type": "integer", "minimum": 1, "maximum": 500},
            "limit": {"type": "integer", "minimum": 1},
            "select_paths": {"type": "array", "items": {"type": "string"}},
            "select_domains": {"type": "array", "items": {"type": "string"}},
            "exclude_paths": {"type": "array", "items": {"type": "string"}},
            "exclude_domains": {"type": "array", "items": {"type": "string"}},
            "allow_external": {"type": "boolean"},
            "include_images": {"type": "boolean"},
            "extract_depth": {"type": "string", "enum": ["basic", "advanced"]},
            "format": {"type": "string", "enum": ["markdown", "text"]},
            "include_favicon": {"type": "boolean"},
            "timeout": {"type": "number", "minimum": 10, "maximum": 150},
            "include_usage": {"type": "boolean"},
        },
        "required": ["url"],
    }

    async def execute(
        self,
        url: str,
        instructions: str | None = None,
        chunks_per_source: int | None = None,
        max_depth: int | None = None,
        max_breadth: int | None = None,
        limit: int | None = None,
        select_paths: list[str] | None = None,
        select_domains: list[str] | None = None,
        exclude_paths: list[str] | None = None,
        exclude_domains: list[str] | None = None,
        allow_external: bool | None = None,
        include_images: bool | None = None,
        extract_depth: str | None = None,
        format: str | None = None,
        include_favicon: bool | None = None,
        timeout: float | None = None,
        include_usage: bool | None = None,
        **kwargs: Any,
    ) -> str:
        del kwargs
        logger.info("web_crawl url={!r} limit={}", url, limit)
        payload: dict[str, Any] = {"url": url}
        _add_optional(
            payload,
            instructions=instructions,
            chunks_per_source=chunks_per_source,
            max_depth=max_depth,
            max_breadth=max_breadth,
            limit=limit,
            select_paths=select_paths,
            select_domains=select_domains,
            exclude_paths=exclude_paths,
            exclude_domains=exclude_domains,
            allow_external=allow_external,
            include_images=include_images,
            extract_depth=extract_depth,
            format=format,
            include_favicon=include_favicon,
            include_usage=include_usage,
        )
        return await self._post_site_request(_TAVILY_CRAWL_URL, payload, timeout or 120.0)


class DeepResearchTool(Tool):
    """Multi-pass web research using Tavily Search API.

    Performs an initial advanced search, derives follow-up queries from the result titles,
    then runs up to two additional basic searches to broaden coverage. Returns a synthesised
    report with key findings and deduplicated source list.

    No shell or exec access required — runs entirely in-process.
    """

    name = "deep_research"
    description = (
        "Conduct multi-pass web research on a topic using Tavily. "
        "Performs an initial advanced search, extracts follow-up queries from results, "
        "and runs additional searches to build a comprehensive report. "
        "Use for questions requiring depth, comparison, or synthesis across multiple sources."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Research topic or question"},
            "depth": {
                "type": "string",
                "enum": ["basic", "advanced"],
                "description": (
                    "Search depth: 'advanced' (default) runs multiple passes; "
                    "'basic' runs a single quick pass"
                ),
                "default": "advanced",
            },
            "max_results": {
                "type": "integer",
                "description": "Max results per search pass (1-10, default 5)",
                "minimum": 1,
                "maximum": 10,
            },
        },
        "required": ["query"],
    }

    def __init__(self, api_key: str | None = None, web_config: "WebToolsConfig | None" = None):
        from yeoman_shared.config.schema import WebToolsConfig as WebToolsCfg

        self._config = web_config or WebToolsCfg()
        self.api_key = api_key or os.environ.get("TAVILY_API_KEY", "")
        _rate_limiter.configure(self._config.rate_limit_rpm)

    async def execute(
        self, query: str, depth: str = "advanced", max_results: int = 5, **kwargs: Any
    ) -> str:
        logger.info("deep_research query={!r} depth={}", query, depth)
        if not _rate_limiter.check():
            return "Error: Rate limit exceeded. Try again shortly."

        if not self.api_key:
            return "Error: TAVILY_API_KEY not configured"

        try:
            n = min(max(max_results, 1), 10)
            all_results: list[dict[str, Any]] = []
            all_answers: list[str] = []
            queries_done: set[str] = {query}

            # Pass 1: primary search with advanced depth for richer results
            primary = await self._search(query, search_depth="advanced", max_results=n)
            all_results.extend(primary.get("results", []))
            if answer := primary.get("answer"):
                all_answers.append(f"[{query}] {answer}")

            if depth == "advanced":
                # Extract follow-up queries from primary result titles
                follow_ups = self._extract_follow_up_queries(query, primary.get("results", []))

                # Passes 2 & 3: follow-up searches
                for fq in follow_ups[:2]:
                    if fq in queries_done:
                        continue
                    queries_done.add(fq)
                    extra = await self._search(fq, search_depth="basic", max_results=n)
                    all_results.extend(extra.get("results", []))
                    if answer := extra.get("answer"):
                        all_answers.append(f"[{fq}] {answer}")

            return self._format_report(query, all_answers, all_results)
        except Exception as e:
            return f"Error: {e}"

    async def _search(
        self, query: str, search_depth: str = "basic", max_results: int = 5
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "query": query,
            "search_depth": search_depth,
            "max_results": max_results,
            "include_answer": True,
        }
        async with httpx.AsyncClient() as client:
            r = await client.post(
                _TAVILY_SEARCH_URL,
                json=payload,
                headers=_tavily_auth_headers(self.api_key),
                timeout=30.0,
            )
            r.raise_for_status()
        return r.json()  # type: ignore[no-any-return]

    def _extract_follow_up_queries(self, original: str, results: list[dict[str, Any]]) -> list[str]:
        """Derive meaningful follow-up queries from primary result titles."""
        follow_ups: list[str] = []
        seen: set[str] = set()
        original_lower = original.lower()

        for item in results:
            title = item.get("title", "").strip()
            if not title or title.lower() == original_lower:
                continue
            # Capitalised multi-word phrases as candidate sub-topics
            phrases = re.findall(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+\b", title)
            for phrase in phrases:
                key = phrase.lower()
                if key not in seen and key not in original_lower:
                    seen.add(key)
                    follow_ups.append(phrase)
            # Also add the full title if it looks sufficiently distinct
            if len(title.split()) >= 3 and title.lower() not in seen:
                seen.add(title.lower())
                follow_ups.append(title)

        return follow_ups[:3]

    def _format_report(self, query: str, answers: list[str], results: list[dict[str, Any]]) -> str:
        """Format a concise research report."""
        # Deduplicate results by URL
        seen_urls: set[str] = set()
        unique_results: list[dict[str, Any]] = []
        for r in results:
            url = r.get("url", "")
            if url and url not in seen_urls:
                seen_urls.add(url)
                unique_results.append(r)

        lines = [f"# Research: {query}\n"]

        if answers:
            lines.append("## Key Findings\n")
            for a in answers:
                lines.append(f"- {a}")
            lines.append("")

        lines.append(f"## Sources ({len(unique_results)} results)\n")
        for i, item in enumerate(unique_results[:15], 1):
            lines.append(f"{i}. **{item.get('title', 'Untitled')}**")
            lines.append(f"   {item.get('url', '')}")
            if content := item.get("content", ""):
                lines.append(f"   {content[:200]}")
            lines.append("")

        return "\n".join(lines)


def _extract_youtube_id(url: str) -> str | None:
    """Extract the YouTube video ID from various URL formats."""
    import re

    patterns = [
        r"youtu\.be/([A-Za-z0-9_-]{11})",
        r"youtube\.com/watch\?.*v=([A-Za-z0-9_-]{11})",
        r"youtube\.com/shorts/([A-Za-z0-9_-]{11})",
        r"youtube\.com/embed/([A-Za-z0-9_-]{11})",
    ]
    for pattern in patterns:
        m = re.search(pattern, url)
        if m:
            return m.group(1)
    return None


class YoutubeTranscriptTool(Tool):
    """Fetch the transcript/captions of a YouTube video."""

    name = "youtube_transcript"
    description = (
        "Fetch the spoken transcript (captions) of a YouTube video. "
        "Use this whenever a YouTube URL is shared and you need to understand or summarize the video content. "
        "Returns the full transcript text. Prefers the user's language, falls back to English, then auto-generated."
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Full YouTube URL (youtu.be or youtube.com)"},
            "language": {
                "type": "string",
                "description": "Preferred transcript language code, e.g. 'en', 'de' (default: 'en')",
            },
        },
        "required": ["url"],
    }

    async def execute(self, url: str, language: str = "en", **kwargs: Any) -> str:
        video_id = _extract_youtube_id(url)
        if not video_id:
            return json.dumps({"error": f"Could not parse YouTube video ID from URL: {url}"})

        try:
            from youtube_transcript_api import YouTubeTranscriptApi
        except ImportError:
            return json.dumps(
                {
                    "error": "youtube-transcript-api is not installed. Run: uv pip install youtube-transcript-api"
                }
            )

        api = YouTubeTranscriptApi()
        # Try preferred language first, then English, then any available
        for langs in ([language], ["en"], None):
            try:
                if langs is None:
                    transcript = api.fetch(video_id)
                else:
                    transcript = api.fetch(video_id, languages=langs)
                break
            except Exception:
                continue
        else:
            return json.dumps(
                {
                    "error": f"No transcript available for video {video_id}. The video may have no captions.",
                    "video_id": video_id,
                }
            )

        snippets = transcript.snippets
        text = " ".join(s.text for s in snippets).strip()
        # Trim to ~12k chars to avoid token overload
        if len(text) > 12000:
            text = text[:12000] + "\n\n[transcript truncated]"

        return json.dumps(
            {
                "video_id": video_id,
                "url": url,
                "language": getattr(transcript, "language_code", language),
                "transcript": text,
                "snippet_count": len(snippets),
            }
        )
