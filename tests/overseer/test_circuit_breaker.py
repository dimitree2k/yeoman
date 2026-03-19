"""Tests for per-runbook circuit breaker."""
from __future__ import annotations
from yeoman_overseer.safety.circuit_breaker import CircuitBreaker, RunbookState

def test_initial_state_is_closed() -> None:
    cb = CircuitBreaker()
    assert cb.get_state("test") == RunbookState.CLOSED

def test_failures_below_threshold_stay_closed() -> None:
    cb = CircuitBreaker(failure_threshold=3)
    cb.record_failure("test")
    cb.record_failure("test")
    assert cb.get_state("test") == RunbookState.CLOSED

def test_failures_at_threshold_trip_to_quarantine() -> None:
    cb = CircuitBreaker(failure_threshold=3)
    for _ in range(3): cb.record_failure("test")
    assert cb.get_state("test") == RunbookState.QUARANTINED

def test_success_resets_failure_count() -> None:
    cb = CircuitBreaker(failure_threshold=3)
    cb.record_failure("test")
    cb.record_failure("test")
    cb.record_success("test")
    assert cb.get_state("test") == RunbookState.CLOSED
    cb.record_failure("test")
    cb.record_failure("test")
    assert cb.get_state("test") == RunbookState.CLOSED

def test_quarantine_count_tracks() -> None:
    cb = CircuitBreaker(failure_threshold=1, backoff_base_s=0)
    cb.record_failure("test")
    assert cb.get_quarantine_count("test") == 1
    cb.try_reenable("test")
    assert cb.get_state("test") == RunbookState.CLOSED
    cb.record_failure("test")
    assert cb.get_quarantine_count("test") == 2

def test_permanent_disable_after_max_quarantines() -> None:
    cb = CircuitBreaker(failure_threshold=1, max_quarantines=3, backoff_base_s=0)
    for _ in range(3):
        cb.record_failure("test")
        cb.try_reenable("test")
    cb.record_failure("test")
    assert cb.get_state("test") == RunbookState.PERMANENTLY_DISABLED

def test_can_execute_check() -> None:
    cb = CircuitBreaker(failure_threshold=1)
    assert cb.can_execute("test") is True
    cb.record_failure("test")
    assert cb.can_execute("test") is False

def test_export_import_state() -> None:
    cb = CircuitBreaker(failure_threshold=2)
    cb.record_failure("test")
    exported = cb.export_state()
    cb2 = CircuitBreaker(failure_threshold=2)
    cb2.import_state(exported)
    assert cb2._breakers["test"].consecutive_failures == 1
