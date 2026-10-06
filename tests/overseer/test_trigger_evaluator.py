"""Tests for the trigger evaluator."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from textwrap import dedent
from unittest.mock import AsyncMock

import pytest
import yeoman_overseer.trigger.evaluator as evaluator_module
from yeoman_overseer.maintenance import MaintenanceManager
from yeoman_overseer.runbook.parser import parse_runbook
from yeoman_overseer.safety.causal import CausalChainDetector
from yeoman_overseer.safety.circuit_breaker import CircuitBreaker
from yeoman_overseer.safety.rate_limiter import RateLimiter
from yeoman_overseer.state import OverseerState
from yeoman_overseer.trigger.evaluator import TriggerEvaluator
from yeoman_overseer.trigger.lock import LockManager


def _write_runbook(tmp_path: Path, name: str = "test-health") -> Path:
    content = dedent(f"""\
        ---
        name: {name}
        domain: health
        enabled: true
        version: 1
        trigger:
          kind: poll
          interval_s: 1
          condition:
            check: process_alive
            target: "1"
            operator: "=="
            value: true
        escalate_to_llm: false
        safety:
          max_actions_per_hour: 10
          cooldown_s: 0
        ---
        # Test
        ## Actions
        1. noop
    """)
    path = tmp_path / f"{name}.md"
    path.write_text(content)
    return path


def _write_manual_reset_runbook(tmp_path: Path, name: str = "test-health") -> Path:
    content = dedent(f"""\
        ---
        name: {name}
        domain: health
        enabled: true
        version: 1
        trigger:
          kind: poll
          interval_s: 1
          condition:
            check: process_alive
            target: "1"
            operator: "=="
            value: true
        escalate_to_llm: false
        safety:
          max_actions_per_hour: 10
          cooldown_s: 0
          manual_reset_after_failures: true
        ---
        # Test
        ## Actions
        1. noop
    """)
    path = tmp_path / f"{name}.md"
    path.write_text(content)
    return path

def _write_cron_runbook(tmp_path: Path, name: str = "test-cron", expr: str = "* * * * *") -> Path:
    content = dedent(f"""\
        ---
        name: {name}
        domain: ops
        enabled: true
        version: 1
        trigger:
          kind: cron
          expr: "{expr}"
        escalate_to_llm: false
        safety:
          max_actions_per_hour: 10
          cooldown_s: 0
        ---
        # Test
        ## Actions
        1. noop
    """)
    path = tmp_path / f"{name}.md"
    path.write_text(content)
    return path

@pytest.mark.asyncio
async def test_evaluator_fires_callback(tmp_path: Path) -> None:
    rb = parse_runbook(_write_runbook(tmp_path))
    callback = AsyncMock()
    evaluator = TriggerEvaluator(runbooks=[rb], on_triggered=callback, lock_manager=LockManager(), circuit_breaker=CircuitBreaker(), rate_limiter=RateLimiter(), causal_detector=CausalChainDetector(), maintenance=MaintenanceManager(), state=OverseerState())
    await evaluator.tick()
    callback.assert_called_once()

@pytest.mark.asyncio
async def test_cron_does_not_catch_up_on_first_tick_after_startup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rb = parse_runbook(_write_cron_runbook(tmp_path))
    callback = AsyncMock()
    clock = {"wall": 1_800_000_000.0, "monotonic": 1_000.0}
    monkeypatch.setattr(evaluator_module.time, "time", lambda: clock["wall"])
    monkeypatch.setattr(evaluator_module.time, "monotonic", lambda: clock["monotonic"])
    evaluator = TriggerEvaluator(runbooks=[rb], on_triggered=callback, lock_manager=LockManager(), circuit_breaker=CircuitBreaker(), rate_limiter=RateLimiter(), causal_detector=CausalChainDetector(), maintenance=MaintenanceManager(), state=OverseerState())

    await evaluator.tick()
    callback.assert_not_called()

    clock["wall"] += 61
    clock["monotonic"] += 61
    await evaluator.tick()
    callback.assert_called_once()


@pytest.mark.asyncio
async def test_cron_occurrence_is_not_replayed_after_clock_rollback_and_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    rb = parse_runbook(_write_cron_runbook(tmp_path, expr="0 8 * * *"))
    state_path = tmp_path / "state.json"

    async def assert_occurrence_persisted(*_args) -> None:
        saved = json.loads(state_path.read_text())
        assert saved["cron_occurrences"]["test-cron"] == occurrence

    callback = AsyncMock(side_effect=assert_occurrence_persisted)
    baseline = datetime(2026, 4, 1, 7, 59, tzinfo=timezone.utc).timestamp()
    occurrence = datetime(2026, 4, 1, 8, 0, tzinfo=timezone.utc).timestamp()
    clock = {"wall": baseline, "monotonic": 1_000.0}
    monkeypatch.setattr(evaluator_module.time, "time", lambda: clock["wall"])
    monkeypatch.setattr(evaluator_module.time, "monotonic", lambda: clock["monotonic"])

    def make_evaluator(state: OverseerState) -> TriggerEvaluator:
        return TriggerEvaluator(
            runbooks=[rb], on_triggered=callback, lock_manager=LockManager(),
            circuit_breaker=CircuitBreaker(), rate_limiter=RateLimiter(),
            causal_detector=CausalChainDetector(), maintenance=MaintenanceManager(),
            state=state,
            persist_state=lambda: state.save(state_path),
        )

    state = OverseerState.load(state_path)
    evaluator = make_evaluator(state)
    await evaluator.tick()
    clock.update(wall=occurrence + 1, monotonic=1_001.0)
    await evaluator.tick()
    assert callback.call_count == 1

    state.save(state_path)
    state = OverseerState.load(state_path)
    clock.update(wall=baseline, monotonic=1_002.0)
    restarted = make_evaluator(state)
    await restarted.tick()
    clock.update(wall=occurrence + 2, monotonic=1_003.0)
    await restarted.tick()
    assert callback.call_count == 1


@pytest.mark.asyncio
async def test_cron_restart_skips_occurrences_missed_before_first_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    rb = parse_runbook(_write_cron_runbook(tmp_path, expr="0 8 * * *"))
    state_path = tmp_path / "state.json"
    yesterday = datetime(2026, 3, 31, 8, 0, tzinfo=timezone.utc).timestamp()
    today = datetime(2026, 4, 1, 8, 0, tzinfo=timezone.utc).timestamp()
    clock = {
        "wall": datetime(2026, 4, 1, 9, 0, tzinfo=timezone.utc).timestamp(),
        "monotonic": 1_000.0,
    }
    monkeypatch.setattr(evaluator_module.time, "time", lambda: clock["wall"])
    monkeypatch.setattr(evaluator_module.time, "monotonic", lambda: clock["monotonic"])
    state = OverseerState(cron_occurrences={"test-cron": yesterday})
    state.save(state_path)
    state = OverseerState.load(state_path)
    callback = AsyncMock()
    evaluator = TriggerEvaluator(
        runbooks=[rb], on_triggered=callback, lock_manager=LockManager(),
        circuit_breaker=CircuitBreaker(), rate_limiter=RateLimiter(),
        causal_detector=CausalChainDetector(), maintenance=MaintenanceManager(),
        state=state, persist_state=lambda: state.save(state_path),
    )

    await evaluator.tick()

    callback.assert_not_called()
    assert json.loads(state_path.read_text())["cron_occurrences"]["test-cron"] == today

    clock["wall"] += 1
    clock["monotonic"] += 1
    await evaluator.tick()
    callback.assert_not_called()

@pytest.mark.asyncio
async def test_evaluator_respects_circuit_breaker(tmp_path: Path) -> None:
    rb = parse_runbook(_write_runbook(tmp_path))
    callback = AsyncMock()
    cb = CircuitBreaker(failure_threshold=1)
    cb.record_failure("test-health")
    evaluator = TriggerEvaluator(runbooks=[rb], on_triggered=callback, lock_manager=LockManager(), circuit_breaker=cb, rate_limiter=RateLimiter(), causal_detector=CausalChainDetector(), maintenance=MaintenanceManager(), state=OverseerState())
    await evaluator.tick()
    callback.assert_not_called()


@pytest.mark.asyncio
async def test_evaluator_manual_reset_runbook_stops_after_three_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rb = parse_runbook(_write_manual_reset_runbook(tmp_path))
    callback = AsyncMock(side_effect=RuntimeError("restart failed"))
    cb = CircuitBreaker(failure_threshold=3)
    clock = {"monotonic": 1_000.0, "wall": 1_800_000_000.0}
    monkeypatch.setattr(evaluator_module.time, "time", lambda: clock["wall"])
    monkeypatch.setattr(evaluator_module.time, "monotonic", lambda: clock["monotonic"])
    evaluator = TriggerEvaluator(
        runbooks=[rb],
        on_triggered=callback,
        lock_manager=LockManager(),
        circuit_breaker=cb,
        rate_limiter=RateLimiter(),
        causal_detector=CausalChainDetector(),
        maintenance=MaintenanceManager(),
        state=OverseerState(),
    )

    for _ in range(3):
        await evaluator.tick()
        clock["monotonic"] += 1.0
        clock["wall"] += 1.0

    clock["monotonic"] += 4_000.0
    clock["wall"] += 4_000.0
    await evaluator.tick()

    assert callback.call_count == 3
    assert cb.can_execute("test-health") is False

@pytest.mark.asyncio
async def test_evaluator_respects_maintenance(tmp_path: Path) -> None:
    rb = parse_runbook(_write_runbook(tmp_path))
    callback = AsyncMock()
    mm = MaintenanceManager()
    mm.enter("1", timeout_s=300, reason="testing")
    evaluator = TriggerEvaluator(runbooks=[rb], on_triggered=callback, lock_manager=LockManager(), circuit_breaker=CircuitBreaker(), rate_limiter=RateLimiter(), causal_detector=CausalChainDetector(), maintenance=mm, state=OverseerState())
    await evaluator.tick()
    callback.assert_not_called()

def _write_ops_runbook(tmp_path: Path, name: str = "test-ops") -> Path:
    content = dedent(f"""\
        ---
        name: {name}
        domain: ops
        enabled: true
        version: 1
        trigger:
          kind: poll
          interval_s: 1
          condition:
            check: process_alive
            target: "1"
            operator: "=="
            value: true
        escalate_to_llm: false
        safety:
          max_actions_per_hour: 10
          cooldown_s: 0
        ---
        # Test
        ## Actions
        1. noop
    """)
    path = tmp_path / f"{name}.md"
    path.write_text(content)
    return path

@pytest.mark.asyncio
async def test_evaluator_respects_rate_limit(tmp_path: Path) -> None:
    rb = parse_runbook(_write_ops_runbook(tmp_path))
    callback = AsyncMock()
    rl = RateLimiter(actions_per_hour=0)
    evaluator = TriggerEvaluator(runbooks=[rb], on_triggered=callback, lock_manager=LockManager(), circuit_breaker=CircuitBreaker(), rate_limiter=rl, causal_detector=CausalChainDetector(), maintenance=MaintenanceManager(), state=OverseerState())
    await evaluator.tick()
    callback.assert_not_called()
