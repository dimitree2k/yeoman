"""Web tools: web_search, web_fetch, and deep_research (all powered by Tavily)."""

import html
import ipaddress
import json
import os
import re
import socket
from typing import Any
from urllib.parse import urlparse

import httpx

from nanobot.agent.tools.base import Tool

# Shared constants
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7_2) AppleWebKit/537.36"
MAX_REDIRECTS = 5  # Limit redirects to prevent DoS attacks
_LOCAL_HOSTS = {"localhost", "localhost.localdomain"}
_TAVILY_SEARCH_URL = "https://api.tavily.com/search"
_TAVILY_EXTRACT_URL = "https://api.tavily.com/extract"


def _strip_tags(text: str) -> str:
    """Remove HTML tags and decode entities."""
    text = re.sub(r'<script[\s\S]*?</script>', '', text, flags=re.I)
    text = re.sub(r'<style[\s\S]*?</style>', '', text, flags=re.I)
    text = re.sub(r'<[^>]+>', '', text)
    return html.unescape(text).strip()


def _normalize(text: str) -> str:
    """Normalize whitespace."""
    text = re.sub(r'[ \t]+', ' ', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


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


def _validate_url(url: str) -> tuple[bool, str]:
    """Validate URL and block SSRF to local/private targets."""
    try:
        p = urlparse(url)
        if p.scheme not in ('http', 'https'):
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

        return True, ""
    except Exception as e:
        return False, str(e)


def _tavily_auth_headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


class WebSearchTool(Tool):
    """Search the web using Tavily Search API."""

    name = "web_search"
    description = "Search the web. Returns titles, URLs, snippets, and an AI-generated answer."
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query"},
            "count": {"type": "integer", "description": "Results (1-10)", "minimum": 1, "maximum": 10}
        },
        "required": ["query"]
    }

    def __init__(self, api_key: str | None = None, max_results: int = 5):
        self.api_key = api_key or os.environ.get("TAVILY_API_KEY", "")
        self.max_results = max_results

    async def execute(self, query: str, count: int | None = None, **kwargs: Any) -> str:
        if not self.api_key:
            return "Error: TAVILY_API_KEY not configured"

        try:
            n = min(max(count or self.max_results, 1), 10)
            payload: dict[str, Any] = {
                "query": query,
                "search_depth": "basic",
                "max_results": n,
                "include_answer": True,
            }
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
    description = "Fetch URL and extract readable content (HTML → markdown/text)."
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL to fetch"},
            "extract_mode": {"type": "string", "enum": ["markdown", "text"], "default": "markdown"},
            "max_chars": {"type": "integer", "minimum": 100}
        },
        "required": ["url"]
    }

    def __init__(self, api_key: str | None = None, max_chars: int = 50000):
        self.api_key = api_key or os.environ.get("TAVILY_API_KEY", "")
        self.max_chars = max_chars

    async def execute(
        self, url: str, extract_mode: str = "markdown", max_chars: int | None = None, **kwargs: Any
    ) -> str:
        max_chars = max_chars or self.max_chars

        # Validate URL before fetching
        is_valid, error_msg = _validate_url(url)
        if not is_valid:
            return json.dumps({"error": f"URL validation failed: {error_msg}", "url": url})

        # Try Tavily Extract first (handles JS-heavy pages and paywall content better)
        if self.api_key:
            try:
                result = await self._tavily_extract(url, max_chars)
                if result is not None:
                    return result
            except Exception:
                pass  # Fall through to direct fetch

        # Fallback: direct fetch with Readability
        return await self._direct_fetch(url, extract_mode, max_chars)

    async def _tavily_extract(self, url: str, max_chars: int) -> str | None:
        """Extract content via Tavily Extract API. Returns None on failure."""
        async with httpx.AsyncClient() as client:
            r = await client.post(
                _TAVILY_EXTRACT_URL,
                json={"urls": [url]},
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
        return json.dumps({
            "url": url,
            "finalUrl": url,
            "extractor": "tavily",
            "truncated": truncated,
            "length": len(text),
            "text": text,
        })

    async def _direct_fetch(self, url: str, extract_mode: str, max_chars: int) -> str:
        """Direct HTTP fetch with Readability extraction."""
        from readability import Document

        try:
            async with httpx.AsyncClient(
                follow_redirects=False,
                timeout=30.0
            ) as client:
                next_url = url
                redirects = 0
                while True:
                    is_valid, error_msg = _validate_url(next_url)
                    if not is_valid:
                        return json.dumps({"error": f"URL validation failed: {error_msg}", "url": next_url})

                    r = await client.get(next_url, headers={"User-Agent": USER_AGENT})

                    if r.status_code in {301, 302, 303, 307, 308} and "location" in r.headers:
                        redirects += 1
                        if redirects > MAX_REDIRECTS:
                            return json.dumps({"error": "Too many redirects", "url": url})
                        location = r.headers.get("location", "")
                        if not location:
                            return json.dumps({"error": "Redirect missing location header", "url": next_url})
                        next_url = str(r.url.join(location))
                        continue

                    r.raise_for_status()
                    break

            ctype = r.headers.get("content-type", "")

            # JSON
            if "application/json" in ctype:
                text, extractor = json.dumps(r.json(), indent=2), "json"
            # HTML
            elif "text/html" in ctype or r.text[:256].lower().startswith(("<!doctype", "<html")):
                doc = Document(r.text)
                content = (
                    self._to_markdown(doc.summary()) if extract_mode == "markdown" else _strip_tags(doc.summary())
                )
                text = f"# {doc.title()}\n\n{content}" if doc.title() else content
                extractor = "readability"
            else:
                text, extractor = r.text, "raw"

            truncated = len(text) > max_chars
            if truncated:
                text = text[:max_chars]

            return json.dumps({
                "url": url, "finalUrl": str(r.url), "status": r.status_code,
                "extractor": extractor, "truncated": truncated, "length": len(text), "text": text,
            })
        except Exception as e:
            return json.dumps({"error": str(e), "url": url})

    def _to_markdown(self, html_text: str) -> str:
        """Convert HTML to markdown."""
        # Convert links, headings, lists before stripping tags
        text = re.sub(
            r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>([\s\S]*?)</a>',
            lambda m: f'[{_strip_tags(m[2])}]({m[1]})', html_text, flags=re.I,
        )
        text = re.sub(
            r'<h([1-6])[^>]*>([\s\S]*?)</h\1>',
            lambda m: f'\n{"#" * int(m[1])} {_strip_tags(m[2])}\n', text, flags=re.I,
        )
        text = re.sub(r'<li[^>]*>([\s\S]*?)</li>', lambda m: f'\n- {_strip_tags(m[1])}', text, flags=re.I)
        text = re.sub(r'</(p|div|section|article)>', '\n\n', text, flags=re.I)
        text = re.sub(r'<(br|hr)\s*/?>', '\n', text, flags=re.I)
        return _normalize(_strip_tags(text))


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

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("TAVILY_API_KEY", "")

    async def execute(
        self, query: str, depth: str = "advanced", max_results: int = 5, **kwargs: Any
    ) -> str:
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

    def _extract_follow_up_queries(
        self, original: str, results: list[dict[str, Any]]
    ) -> list[str]:
        """Derive meaningful follow-up queries from primary result titles."""
        follow_ups: list[str] = []
        seen: set[str] = set()
        original_lower = original.lower()

        for item in results:
            title = item.get("title", "").strip()
            if not title or title.lower() == original_lower:
                continue
            # Capitalised multi-word phrases as candidate sub-topics
            phrases = re.findall(r'\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+\b', title)
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

    def _format_report(
        self, query: str, answers: list[str], results: list[dict[str, Any]]
    ) -> str:
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
