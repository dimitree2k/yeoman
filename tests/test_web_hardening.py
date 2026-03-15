import time

import pytest

from yeoman.agent.tools.web import _validate_domain, _WebRateLimiter
from yeoman.config.schema import WebToolsConfig


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
    from yeoman.agent.tools.web import _async_validate_dns

    with pytest.raises(ValueError, match="private"):
        await _async_validate_dns("localhost")


@pytest.mark.asyncio
async def test_async_validate_dns_no_resolve():
    from yeoman.agent.tools.web import _async_validate_dns

    with pytest.raises(ValueError):
        await _async_validate_dns("this-domain-does-not-exist-xyz123.invalid")


# --- Task 5: WebFetchTool config + content-type filter ---


@pytest.mark.asyncio
async def test_web_fetch_respects_max_fetch_bytes():
    from yeoman.agent.tools.web import WebFetchTool

    cfg = WebToolsConfig(max_fetch_bytes=500)
    tool = WebFetchTool(api_key="", web_config=cfg)
    assert tool._max_fetch_bytes == 500


def test_web_fetch_content_type_check():
    from yeoman.agent.tools.web import WebFetchTool

    cfg = WebToolsConfig()
    tool = WebFetchTool(api_key="", web_config=cfg)
    assert tool._is_allowed_content_type("text/html; charset=utf-8") is True
    assert tool._is_allowed_content_type("application/json") is True
    assert tool._is_allowed_content_type("image/png") is False
    assert tool._is_allowed_content_type("application/octet-stream") is False
    assert tool._is_allowed_content_type("") is True  # missing = allow


# --- Task 7: Rate limiter wiring ---


@pytest.mark.asyncio
async def test_web_fetch_rate_limited():
    import json as _json

    from yeoman.agent.tools.web import WebFetchTool, _rate_limiter

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

    from yeoman.agent.tools.web import WebFetchTool, _rate_limiter

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
