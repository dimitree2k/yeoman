import time

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
