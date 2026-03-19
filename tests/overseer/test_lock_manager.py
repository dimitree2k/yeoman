"""Tests for the overseer lock manager."""
from __future__ import annotations
from yeoman_overseer.trigger.lock import LockManager

def test_acquire_exclusive_lock() -> None:
    lm = LockManager()
    assert lm.acquire("gateway", "health-gateway", exclusive=True) is True
    assert lm.is_locked("gateway")

def test_exclusive_blocks_exclusive() -> None:
    lm = LockManager()
    lm.acquire("gateway", "health-gateway", exclusive=True)
    assert lm.acquire("gateway", "ops-restart", exclusive=True) is False

def test_exclusive_blocks_shared() -> None:
    lm = LockManager()
    lm.acquire("gateway", "health-gateway", exclusive=True)
    assert lm.acquire("gateway", "ops-check", exclusive=False) is False

def test_shared_allows_shared() -> None:
    lm = LockManager()
    lm.acquire("metrics", "health-check", exclusive=False)
    assert lm.acquire("metrics", "quality-check", exclusive=False) is True

def test_shared_blocks_exclusive() -> None:
    lm = LockManager()
    lm.acquire("metrics", "health-check", exclusive=False)
    assert lm.acquire("metrics", "prune-metrics", exclusive=True) is False

def test_release_lock() -> None:
    lm = LockManager()
    lm.acquire("gateway", "health-gateway", exclusive=True)
    lm.release("gateway", "health-gateway")
    assert not lm.is_locked("gateway")

def test_llm_lock_serialized() -> None:
    lm = LockManager()
    assert lm.acquire_llm("skill-audit") is True
    assert lm.acquire_llm("memory-hygiene") is False
    lm.release_llm("skill-audit")
    assert lm.acquire_llm("memory-hygiene") is True

def test_lock_expiry() -> None:
    lm = LockManager(lock_timeout_s=0)
    lm.acquire("gateway", "health-gateway", exclusive=True)
    assert lm.acquire("gateway", "ops-restart", exclusive=True) is True

def test_release_nonexistent_is_noop() -> None:
    lm = LockManager()
    lm.release("nonexistent", "nothing")
