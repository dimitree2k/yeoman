import json
import time
from typing import Any

import httpx
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


class _DirectResponse:
    def __init__(
        self,
        *,
        requested_url: str,
        status_code: int = 200,
        body: str = "<html><body><article>Article body</article></body></html>",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self.url = httpx.URL(requested_url)
        self.headers = httpx.Headers(
            headers or {"content-type": "text/html; charset=utf-8"}
        )
        self._body = body.encode()

    async def __aenter__(self) -> "_DirectResponse":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    def raise_for_status(self) -> None:
        request = httpx.Request("GET", self.url)
        response = httpx.Response(
            self.status_code,
            request=request,
        )
        response.raise_for_status()

    async def aiter_bytes(self, _chunk_size: int):
        yield self._body


class _DirectClient:
    responses: list[_DirectResponse] = []

    def __init__(self, **_kwargs: object) -> None:
        self._responses = iter(self.responses)

    async def __aenter__(self) -> "_DirectClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    def stream(
        self,
        _method: str,
        url: str,
        *,
        headers: dict[str, str],
    ) -> _DirectResponse:
        del headers
        response = next(self._responses)
        response.url = httpx.URL(url)
        return response


def _install_direct_fetch(
    monkeypatch: pytest.MonkeyPatch,
    responses: list[_DirectResponse],
) -> None:
    from yeoman_gateway.agent.tools import web

    async def _allow_dns(_hostname: str) -> None:
        return None

    _DirectClient.responses = responses
    monkeypatch.setattr(web.httpx, "AsyncClient", _DirectClient)
    monkeypatch.setattr(web, "_async_validate_dns", _allow_dns)


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
async def test_web_fetch_provenance_required_bypasses_tavily(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from yeoman_gateway.agent.tools.web import WebFetchTool, _rate_limiter

    _rate_limiter._timestamps.clear()
    _rate_limiter.configure(100)
    _install_direct_fetch(
        monkeypatch,
        [_DirectResponse(requested_url="https://example.com/article")],
    )
    tool = WebFetchTool(
        api_key="tvly-test",
        web_config=WebToolsConfig(rate_limit_rpm=100),
    )

    async def _must_not_call(*args: object, **kwargs: object) -> str:
        raise AssertionError("Tavily must not run for provenance-required fetches")

    monkeypatch.setattr(tool, "_tavily_extract", _must_not_call)

    result = json.loads(
        await tool.execute(
            url="https://example.com/article",
            provenance_required=True,
        )
    )

    assert result["finalUrl"] == "https://example.com/article"
    assert result["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 403, 451])
async def test_web_fetch_auth_wall_status_is_structured_failure(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
) -> None:
    from yeoman_gateway.agent.tools.web import WebFetchTool, _rate_limiter

    _rate_limiter._timestamps.clear()
    _rate_limiter.configure(100)
    _install_direct_fetch(
        monkeypatch,
        [
            _DirectResponse(
                requested_url="https://example.com/article",
                status_code=status_code,
            )
        ],
    )

    result = json.loads(
        await WebFetchTool(
            api_key="",
            web_config=WebToolsConfig(rate_limit_rpm=100),
        ).execute(
            url="https://example.com/article",
            provenance_required=True,
        )
    )

    assert result["error_code"] == "auth_wall"
    assert result["finalUrl"] == "https://example.com/article"


@pytest.mark.asyncio
async def test_web_fetch_empty_body_is_structured_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from yeoman_gateway.agent.tools.web import WebFetchTool, _rate_limiter

    _rate_limiter._timestamps.clear()
    _rate_limiter.configure(100)
    _install_direct_fetch(
        monkeypatch,
        [
            _DirectResponse(
                requested_url="https://example.com/article",
                body="",
            )
        ],
    )

    result = json.loads(
        await WebFetchTool(
            api_key="",
            web_config=WebToolsConfig(rate_limit_rpm=100),
        ).execute(
            url="https://example.com/article",
            provenance_required=True,
        )
    )

    assert result["error_code"] == "empty_body"


@pytest.mark.asyncio
async def test_web_fetch_same_host_canonical_redirect_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from yeoman_gateway.agent.tools.web import WebFetchTool, _rate_limiter

    _rate_limiter._timestamps.clear()
    _rate_limiter.configure(100)
    _install_direct_fetch(
        monkeypatch,
        [
            _DirectResponse(
                requested_url="http://www.example.com/article",
                status_code=301,
                headers={"location": "https://example.com/canonical/article"},
            ),
            _DirectResponse(
                requested_url="https://example.com/canonical/article",
            ),
        ],
    )

    result = json.loads(
        await WebFetchTool(
            api_key="",
            web_config=WebToolsConfig(rate_limit_rpm=100),
        ).execute(
            url="http://www.example.com/article",
            provenance_required=True,
        )
    )

    assert result["finalUrl"] == "https://example.com/canonical/article"
    assert result["text"]


@pytest.mark.asyncio
async def test_web_fetch_cross_host_redirect_is_structured_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from yeoman_gateway.agent.tools.web import WebFetchTool, _rate_limiter

    _rate_limiter._timestamps.clear()
    _rate_limiter.configure(100)
    _install_direct_fetch(
        monkeypatch,
        [
            _DirectResponse(
                requested_url="https://example.com/article",
                status_code=302,
                headers={"location": "https://login.other.example/signin"},
            )
        ],
    )

    result = json.loads(
        await WebFetchTool(
            api_key="",
            web_config=WebToolsConfig(rate_limit_rpm=100),
        ).execute(
            url="https://example.com/article",
            provenance_required=True,
        )
    )

    assert result["error_code"] == "cross_host_redirect"
    assert result["finalUrl"] == "https://login.other.example/signin"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("requested_url", "redirect_url", "error_code", "expected_final_url"),
    [
        (
            "https://example.com/article",
            "http://example.com/canonical",
            "scheme_downgrade",
            "http://example.com/canonical",
        ),
        (
            "http://example.com:8080/article",
            "https://example.com:8443/canonical",
            "unsafe_port_redirect",
            "http://example.com:8080/article",
        ),
    ],
)
async def test_web_fetch_rejects_unsafe_redirect_transition(
    monkeypatch: pytest.MonkeyPatch,
    requested_url: str,
    redirect_url: str,
    error_code: str,
    expected_final_url: str,
) -> None:
    from yeoman_gateway.agent.tools.web import WebFetchTool, _rate_limiter

    _rate_limiter._timestamps.clear()
    _rate_limiter.configure(100)
    _install_direct_fetch(
        monkeypatch,
        [
            _DirectResponse(
                requested_url=requested_url,
                status_code=302,
                headers={"location": redirect_url},
            )
        ],
    )

    result = json.loads(
        await WebFetchTool(
            api_key="",
            web_config=WebToolsConfig(rate_limit_rpm=100),
        ).execute(
            url=requested_url,
            provenance_required=True,
        )
    )

    assert result["error_code"] == error_code
    assert result["finalUrl"] == expected_final_url


@pytest.mark.asyncio
async def test_web_fetch_rejects_explicit_nondefault_request_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from yeoman_gateway.agent.tools.web import WebFetchTool, _rate_limiter

    _rate_limiter._timestamps.clear()
    _rate_limiter.configure(100)
    url = "https://example.com:8443/article"
    _install_direct_fetch(
        monkeypatch,
        [_DirectResponse(requested_url=url)],
    )

    result = json.loads(
        await WebFetchTool(
            api_key="",
            web_config=WebToolsConfig(rate_limit_rpm=100),
        ).execute(
            url=url,
            provenance_required=True,
        )
    )

    assert result["error_code"] == "unsafe_port_redirect"
    assert result["finalUrl"] == url


@pytest.mark.asyncio
async def test_web_fetch_allows_unicode_to_punycode_same_host_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from yeoman_gateway.agent.tools.web import WebFetchTool, _rate_limiter

    _rate_limiter._timestamps.clear()
    _rate_limiter.configure(100)
    _install_direct_fetch(
        monkeypatch,
        [
            _DirectResponse(
                requested_url="http://bücher.example/article",
                status_code=301,
                headers={
                    "location": "https://www.xn--bcher-kva.example/canonical"
                },
            ),
            _DirectResponse(
                requested_url="https://www.xn--bcher-kva.example/canonical",
            ),
        ],
    )

    result = json.loads(
        await WebFetchTool(
            api_key="",
            web_config=WebToolsConfig(rate_limit_rpm=100),
        ).execute(
            url="http://bücher.example/article",
            provenance_required=True,
        )
    )

    assert result["finalUrl"] == (
        "https://www.xn--bcher-kva.example/canonical"
    )
    assert result["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["login", "signin", "consent"])
async def test_web_fetch_auth_wall_redirect_path_is_structured_failure(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    from yeoman_gateway.agent.tools.web import WebFetchTool, _rate_limiter

    _rate_limiter._timestamps.clear()
    _rate_limiter.configure(100)
    _install_direct_fetch(
        monkeypatch,
        [
            _DirectResponse(
                requested_url=f"https://example.com/{path}",
                body="<html><body>Sign in to continue</body></html>",
            )
        ],
    )

    result = json.loads(
        await WebFetchTool(
            api_key="",
            web_config=WebToolsConfig(rate_limit_rpm=100),
        ).execute(
            url=f"https://example.com/{path}",
            provenance_required=True,
        )
    )

    assert result["error_code"] == "auth_wall"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        '<html><body><input type="password"></body></html>',
        "<html><body>Consent to continue</body></html>",
    ],
)
async def test_web_fetch_nonredirecting_auth_wall_body_is_structured_failure(
    monkeypatch: pytest.MonkeyPatch,
    body: str,
) -> None:
    from yeoman_gateway.agent.tools.web import WebFetchTool, _rate_limiter

    _rate_limiter._timestamps.clear()
    _rate_limiter.configure(100)
    _install_direct_fetch(
        monkeypatch,
        [
            _DirectResponse(
                requested_url="https://example.com/article",
                body=body,
            )
        ],
    )

    result = json.loads(
        await WebFetchTool(
            api_key="",
            web_config=WebToolsConfig(rate_limit_rpm=100),
        ).execute(
            url="https://example.com/article",
            provenance_required=True,
        )
    )

    assert result["error_code"] == "auth_wall"


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
