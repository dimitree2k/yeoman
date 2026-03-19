"""Tests for global rate limiter."""
from __future__ import annotations
from yeoman_overseer.safety.rate_limiter import RateLimiter

def test_allows_under_limit() -> None:
    rl = RateLimiter(actions_per_hour=10, llm_calls_per_day=5)
    assert rl.can_act() is True
    assert rl.can_llm() is True

def test_blocks_at_action_limit() -> None:
    rl = RateLimiter(actions_per_hour=3, llm_calls_per_day=5)
    for _ in range(3): rl.record_action()
    assert rl.can_act() is False

def test_blocks_at_llm_limit() -> None:
    rl = RateLimiter(actions_per_hour=30, llm_calls_per_day=2)
    for _ in range(2): rl.record_llm_call()
    assert rl.can_llm() is False

def test_health_domain_allowed_when_actions_exhausted() -> None:
    rl = RateLimiter(actions_per_hour=1, llm_calls_per_day=5)
    rl.record_action()
    assert rl.can_act() is False
    assert rl.can_act(domain="health") is True

def test_llm_critical_only_at_80_percent() -> None:
    rl = RateLimiter(actions_per_hour=30, llm_calls_per_day=10)
    for _ in range(8): rl.record_llm_call()
    assert rl.can_llm(domain="health") is True
    assert rl.can_llm(domain="evolution") is False

def test_reset_hourly() -> None:
    rl = RateLimiter(actions_per_hour=1, llm_calls_per_day=5)
    rl.record_action()
    assert rl.can_act() is False
    rl.reset_hourly()
    assert rl.can_act() is True

def test_reset_daily() -> None:
    rl = RateLimiter(actions_per_hour=30, llm_calls_per_day=1)
    rl.record_llm_call()
    assert rl.can_llm() is False
    rl.reset_daily()
    assert rl.can_llm() is True

def test_remaining_budget() -> None:
    rl = RateLimiter(actions_per_hour=10, llm_calls_per_day=5)
    rl.record_action(); rl.record_action(); rl.record_llm_call()
    remaining = rl.remaining()
    assert remaining["actions_hour"] == 8
    assert remaining["llm_daily"] == 4
