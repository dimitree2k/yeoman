import time
from typing import Any

import pytest
from yeoman_gateway.agent.tools.web import _validate_domain, _WebRateLimiter
from yeoman_shared.config.schema import WebToolsConfig


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.status_code = 200
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


class _RecordingAsyncClient:
    calls: list[dict[str, Any]] = []
    response_payload: dict[str, Any] = {"results": []}

    async def __aenter__(self) -> "_RecordingAsyncClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def post(
        self,
        url: str,
        *,
        json: dict[str, Any],
        headers: dict[str, str],
        timeout: float,
    ) -> _FakeResponse:
        self.calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        return _FakeResponse(self.response_payload)


def test_web_tools_config_defaults():
    cfg = WebToolsConfig()
    assert cfg.max_fetch_bytes == 2_097_152
    assert cfg.blocked_domains == []
    assert cfg.allowed_domains == []
    assert cfg.rate_limit_rpm == 20
    assert "text/" in cfg.allowed_content_types
    assert "application/json" in cfg.allowed_content_types


def test_web_tools_config_custom():
    cfg = WebToolsConfig(
        blocked_domains=["evil.com"],
        allowed_domains=["good.com"],
        rate_limit_rpm=5,
        max_fetch_bytes=1_000_000,
    )
    assert cfg.blocked_domains == ["evil.com"]
    assert cfg.rate_limit_rpm == 5
    assert cfg.max_fetch_bytes == 1_000_000


def test_rate_limiter_allows_within_limit():
    rl = _WebRateLimiter(max_requests=3, window_seconds=60)
    assert rl.check() is True
    assert rl.check() is True
    assert rl.check() is True


def test_rate_limiter_blocks_over_limit():
    rl = _WebRateLimiter(max_requests=2, window_seconds=60)
    assert rl.check() is True
    assert rl.check() is True
    assert rl.check() is False


def test_rate_limiter_window_expiry():
    rl = _WebRateLimiter(max_requests=1, window_seconds=0.1)
    assert rl.check() is True
    assert rl.check() is False
    time.sleep(0.15)
    assert rl.check() is True


def test_validate_domain_no_restrictions():
    ok, err = _validate_domain("example.com", [], [])
    assert ok is True


def test_validate_domain_blocked():
    ok, err = _validate_domain("evil.com", blocked=["evil.com"], allowed=[])
    assert ok is False
    assert "blocked" in err.lower()


def test_validate_domain_blocked_subdomain():
    ok, err = _validate_domain("sub.evil.com", blocked=["evil.com"], allowed=[])
    assert ok is False


def test_validate_domain_allowed_only():
    ok, err = _validate_domain("good.com", blocked=[], allowed=["good.com"])
    assert ok is True


def test_validate_domain_not_in_allowlist():
    ok, err = _validate_domain("other.com", blocked=[], allowed=["good.com"])
    assert ok is False
    assert "not in allowed" in err.lower()


def test_validate_domain_allowed_subdomain():
    ok, err = _validate_domain("sub.good.com", blocked=[], allowed=["good.com"])
    assert ok is True


# --- Task 4: Async DNS validation ---


@pytest.mark.asyncio
async def test_async_validate_dns_private():
    from yeoman_gateway.agent.tools.web import _async_validate_dns

    with pytest.raises(ValueError, match="private"):
        await _async_validate_dns("localhost")


@pytest.mark.asyncio
async def test_async_validate_dns_no_resolve():
    from yeoman_gateway.agent.tools.web import _async_validate_dns

    with pytest.raises(ValueError):
        await _async_validate_dns("this-domain-does-not-exist-xyz123.invalid")


# --- Task 5: WebFetchTool config + content-type filter ---


@pytest.mark.asyncio
async def test_web_fetch_respects_max_fetch_bytes():
    from yeoman_gateway.agent.tools.web import WebFetchTool

    cfg = WebToolsConfig(max_fetch_bytes=500)
    tool = WebFetchTool(api_key="", web_config=cfg)
    assert tool._max_fetch_bytes == 500


def test_web_fetch_content_type_check():
    from yeoman_gateway.agent.tools.web import WebFetchTool

    cfg = WebToolsConfig()
    tool = WebFetchTool(api_key="", web_config=cfg)
    assert tool._is_allowed_content_type("text/html; charset=utf-8") is True
    assert tool._is_allowed_content_type("application/json") is True
    assert tool._is_allowed_content_type("image/png") is False
    assert tool._is_allowed_content_type("application/octet-stream") is False
    assert tool._is_allowed_content_type("") is True  # missing = allow


@pytest.mark.asyncio
async def test_web_search_forwards_tavily_controls(monkeypatch: pytest.MonkeyPatch):
    from yeoman_gateway.agent.tools import web
    from yeoman_gateway.agent.tools.web import WebSearchTool, _rate_limiter

    _rate_limiter._timestamps.clear()
    _rate_limiter.configure(100)
    _RecordingAsyncClient.calls = []
    _RecordingAsyncClient.response_payload = {
        "answer": "summary",
        "results": [{"title": "Title", "url": "https://example.com", "content": "Snippet"}],
    }
    monkeypatch.setattr(web.httpx, "AsyncClient", _RecordingAsyncClient)

    tool = WebSearchTool(api_key="tvly-test", web_config=WebToolsConfig(rate_limit_rpm=100))
    result = await tool.execute(
        query="latest AI agent search",
        max_results=12,
        search_depth="advanced",
        topic="news",
        time_range="week",
        chunks_per_source=2,
        include_domains=["docs.tavily.com"],
        exclude_domains=["spam.example"],
        include_raw_content="markdown",
        include_favicon=True,
        include_usage=True,
    )

    assert "Results for: latest AI agent search" in result
    payload = _RecordingAsyncClient.calls[0]["json"]
    assert payload == {
        "query": "latest AI agent search",
        "search_depth": "advanced",
        "max_results": 12,
        "include_answer": True,
        "topic": "news",
        "time_range": "week",
        "chunks_per_source": 2,
        "include_raw_content": "markdown",
        "include_favicon": True,
        "include_usage": True,
        "include_domains": ["docs.tavily.com"],
        "exclude_domains": ["spam.example"],
    }


@pytest.mark.asyncio
async def test_web_fetch_tavily_extract_accepts_query_focused_options(
    monkeypatch: pytest.MonkeyPatch,
):
    import json as _json

    from yeoman_gateway.agent.tools import web
    from yeoman_gateway.agent.tools.web import WebFetchTool, _rate_limiter

    _rate_limiter._timestamps.clear()
    _rate_limiter.configure(100)
    _RecordingAsyncClient.calls = []
    _RecordingAsyncClient.response_payload = {
        "results": [{"url": "https://example.com/a", "raw_content": "focused chunk"}]
    }
    monkeypatch.setattr(web.httpx, "AsyncClient", _RecordingAsyncClient)

    tool = WebFetchTool(api_key="tvly-test", web_config=WebToolsConfig(rate_limit_rpm=100))
    result = await tool.execute(
        url="https://example.com/a",
        query="pricing table",
        chunks_per_source=4,
        extract_depth="advanced",
        include_images=True,
        include_favicon=True,
        include_usage=True,
    )

    data = _json.loads(result)
    assert data["extractor"] == "tavily"
    assert data["text"] == "focused chunk"
    payload = _RecordingAsyncClient.calls[0]["json"]
    assert payload == {
        "urls": ["https://example.com/a"],
        "query": "pricing table",
        "chunks_per_source": 4,
        "extract_depth": "advanced",
        "format": "markdown",
        "include_images": True,
        "include_favicon": True,
        "include_usage": True,
    }


@pytest.mark.asyncio
async def test_web_map_tool_posts_tavily_map_payload(monkeypatch: pytest.MonkeyPatch):
    import json as _json

    from yeoman_gateway.agent.tools import web
    from yeoman_gateway.agent.tools.web import WebMapTool, _rate_limiter

    _rate_limiter._timestamps.clear()
    _rate_limiter.configure(100)
    _RecordingAsyncClient.calls = []
    _RecordingAsyncClient.response_payload = {
        "base_url": "docs.example.com",
        "results": ["https://docs.example.com/a"],
        "usage": {"credits": 1},
    }
    monkeypatch.setattr(web.httpx, "AsyncClient", _RecordingAsyncClient)

    tool = WebMapTool(api_key="tvly-test", web_config=WebToolsConfig(rate_limit_rpm=100))
    result = await tool.execute(
        url="https://docs.example.com",
        instructions="Find API pages",
        max_depth=2,
        limit=25,
        select_paths=["/api/.*"],
        exclude_paths=["/old/.*"],
        allow_external=False,
        include_usage=True,
    )

    assert _json.loads(result)["results"] == ["https://docs.example.com/a"]
    assert _RecordingAsyncClient.calls[0]["url"] == "https://api.tavily.com/map"
    assert _RecordingAsyncClient.calls[0]["json"] == {
        "url": "https://docs.example.com",
        "instructions": "Find API pages",
        "max_depth": 2,
        "limit": 25,
        "select_paths": ["/api/.*"],
        "exclude_paths": ["/old/.*"],
        "allow_external": False,
        "include_usage": True,
    }


@pytest.mark.asyncio
async def test_web_crawl_tool_posts_tavily_crawl_payload(monkeypatch: pytest.MonkeyPatch):
    import json as _json

    from yeoman_gateway.agent.tools import web
    from yeoman_gateway.agent.tools.web import WebCrawlTool, _rate_limiter

    _rate_limiter._timestamps.clear()
    _rate_limiter.configure(100)
    _RecordingAsyncClient.calls = []
    _RecordingAsyncClient.response_payload = {
        "base_url": "docs.example.com",
        "results": [{"url": "https://docs.example.com/a", "raw_content": "Page"}],
    }
    monkeypatch.setattr(web.httpx, "AsyncClient", _RecordingAsyncClient)

    tool = WebCrawlTool(api_key="tvly-test", web_config=WebToolsConfig(rate_limit_rpm=100))
    result = await tool.execute(
        url="https://docs.example.com",
        instructions="Find API pages",
        chunks_per_source=3,
        max_depth=2,
        max_breadth=10,
        limit=20,
        select_domains=["^docs\\.example\\.com$"],
        exclude_domains=["^private\\.example\\.com$"],
        include_images=True,
        extract_depth="advanced",
        format="text",
    )

    assert _json.loads(result)["results"][0]["raw_content"] == "Page"
    assert _RecordingAsyncClient.calls[0]["url"] == "https://api.tavily.com/crawl"
    assert _RecordingAsyncClient.calls[0]["json"] == {
        "url": "https://docs.example.com",
        "instructions": "Find API pages",
        "chunks_per_source": 3,
        "max_depth": 2,
        "max_breadth": 10,
        "limit": 20,
        "select_domains": ["^docs\\.example\\.com$"],
        "exclude_domains": ["^private\\.example\\.com$"],
        "include_images": True,
        "extract_depth": "advanced",
        "format": "text",
    }


# --- Task 7: Rate limiter wiring ---


@pytest.mark.asyncio
async def test_web_fetch_rate_limited():

    from yeoman_gateway.agent.tools.web import WebFetchTool, _rate_limiter

    cfg = WebToolsConfig(rate_limit_rpm=1)
    tool = WebFetchTool(api_key="", web_config=cfg)
    _rate_limiter.configure(1)
    _rate_limiter._timestamps.clear()

    # First call proceeds (will fail on actual fetch but not on rate limit)
    r1 = await tool.execute(url="http://example.com")
    assert "rate limit" not in r1.lower()

    # Second call should be rate-limited
    r2 = await tool.execute(url="http://example.com")
    assert "rate limit" in r2.lower()


# --- Task 10: Full hardening integration test ---


@pytest.mark.asyncio
async def test_web_fetch_full_hardening_integration():
    """Verify all hardening measures work together."""
    import json as _json

    from yeoman_gateway.agent.tools.web import WebFetchTool, _rate_limiter

    _rate_limiter._timestamps.clear()
    _rate_limiter.configure(100)

    cfg = WebToolsConfig(
        rate_limit_rpm=100,
        blocked_domains=["blocked.example"],
        max_fetch_bytes=1_000_000,
    )
    tool = WebFetchTool(api_key="", web_config=cfg)

    # Blocked domain
    result = await tool.execute(url="http://blocked.example/page")
    data = _json.loads(result)
    assert "error" in data
    assert "blocked" in data["error"].lower() or "Blocked" in data["error"]

    # Private IP
    result = await tool.execute(url="http://192.168.1.1/admin")
    data = _json.loads(result)
    assert "error" in data

    # YouTube redirect
    result = await tool.execute(url="https://www.youtube.com/watch?v=abc123")
    data = _json.loads(result)
    assert "youtube_transcript" in data.get("action", "").lower()
