"""Reddit reader for web_fetch: public ``.json`` endpoints instead of the blocked HTML."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx
from loguru import logger

_HOSTS = frozenset(
    {"reddit.com", "www.reddit.com", "old.reddit.com", "new.reddit.com", "np.reddit.com", "m.reddit.com"}
)
_API_BASE = "https://www.reddit.com"
_USER_AGENT = "linux:yeoman-reader:1.0 (personal assistant, read-only)"
_TIMEOUT = 15.0
_MAX_COMMENTS = 25
_MAX_DEPTH = 2
_MAX_BODY_CHARS = 1500
_LISTING_LIMIT = 15

_POST_RE = re.compile(r"^(?:/r/[^/]+)?/comments/([a-z0-9]+)", re.IGNORECASE)
_SHARE_RE = re.compile(r"^/r/[^/]+/s/[A-Za-z0-9]+", re.IGNORECASE)
_SUB_RE = re.compile(r"^/r/([A-Za-z0-9_]+)(?:/(hot|new|top|rising))?/?$", re.IGNORECASE)


def is_reddit_url(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host in _HOSTS or host == "redd.it"


def _api_path(url: str) -> str | None:
    """Map a Reddit page URL to its JSON API path, or None when unsupported."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    path = parts.path.rstrip("/") or "/"
    if host == "redd.it":
        post_id = path.strip("/")
        return f"/comments/{post_id}.json" if re.fullmatch(r"[a-z0-9]+", post_id, re.IGNORECASE) else None
    if m := _POST_RE.match(path):
        return f"/comments/{m.group(1)}.json"
    if m := _SUB_RE.match(path):
        return f"/r/{m.group(1)}/{(m.group(2) or 'hot').lower()}.json"
    return None


def _clip(text: str, limit: int = _MAX_BODY_CHARS) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + " …"


def _date(created: Any) -> str:
    try:
        return datetime.fromtimestamp(float(created), tz=UTC).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OverflowError):
        return "?"


def _post_header(post: dict[str, Any]) -> list[str]:
    lines = [
        f"# {post.get('title', '').strip()}",
        (
            f"r/{post.get('subreddit', '?')} · u/{post.get('author', '?')} · "
            f"score {post.get('score', 0)} · {post.get('num_comments', 0)} comments · "
            f"{_date(post.get('created_utc'))}"
        ),
    ]
    link = post.get("url_overridden_by_dest") or ""
    if link and not post.get("is_self"):
        lines.append(f"Link: {link}")
    return lines


def _render_comments(children: list[dict[str, Any]], depth: int, out: list[str], budget: list[int]) -> None:
    for child in children:
        if budget[0] <= 0 or child.get("kind") != "t1":
            continue
        data = child.get("data") or {}
        budget[0] -= 1
        indent = "  " * depth
        body = _clip(data.get("body", "")).replace("\n", f"\n{indent}  ")
        out.append(f"{indent}- u/{data.get('author', '?')} ({data.get('score', 0)}): {body}")
        replies = data.get("replies")
        if depth + 1 < _MAX_DEPTH and isinstance(replies, dict):
            _render_comments((replies.get("data") or {}).get("children") or [], depth + 1, out, budget)


def render_thread(payload: Any) -> str:
    """Render a ``/comments/<id>.json`` payload as markdown."""
    post = payload[0]["data"]["children"][0]["data"]
    lines = _post_header(post)
    if selftext := (post.get("selftext") or "").strip():
        lines += ["", selftext]
    comments = payload[1]["data"]["children"] if len(payload) > 1 else []
    if comments:
        lines += ["", "## Top comments"]
        _render_comments(comments, 0, lines, [_MAX_COMMENTS])
    return "\n".join(lines)


def render_listing(payload: Any) -> str:
    """Render a subreddit listing payload as markdown."""
    lines: list[str] = []
    for child in (payload.get("data") or {}).get("children") or []:
        post = child.get("data") or {}
        if not post.get("title"):
            continue
        lines.append(
            f"- {post['title'].strip()} (score {post.get('score', 0)}, "
            f"{post.get('num_comments', 0)} comments) https://www.reddit.com{post.get('permalink', '')}"
        )
    return "\n".join(lines)


async def fetch_reddit(url: str) -> tuple[str, str] | None:
    """Return ``(final_url, markdown)`` for a supported Reddit URL, or None to fall back."""
    headers = {"User-Agent": _USER_AGENT, "Accept": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, headers=headers) as client:
            if _SHARE_RE.match(urlsplit(url).path):
                # Share links only redirect to the canonical post; resolve, then re-check the host.
                resolved = await client.get(url, follow_redirects=False)
                url = str(resolved.url.join(resolved.headers.get("location", "")))
                if not is_reddit_url(url) or not _POST_RE.match(urlsplit(url).path):
                    return None
            api_path = _api_path(url)
            if api_path is None:
                return None
            r = await client.get(
                f"{_API_BASE}{api_path}",
                params={"raw_json": 1, "limit": _LISTING_LIMIT if "/comments/" not in api_path else 50},
            )
            if r.status_code != 200:
                logger.info("reddit fetch status={} path={}", r.status_code, api_path)
                return None
            payload = r.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.info("reddit fetch failed: {}", exc)
        return None
    try:
        text = render_thread(payload) if "/comments/" in api_path else render_listing(payload)
    except (KeyError, IndexError, TypeError):
        return None
    return (url, text) if text.strip() else None
