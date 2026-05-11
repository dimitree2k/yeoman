"""Tests for the trigger evaluator."""
from __future__ import annotations

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
async def test_evaluator_respects_circuit_breaker(tmp_path: Path) -> None:
    rb = parse_runbook(_write_runbook(tmp_path))
    callback = AsyncMock()
    cb = CircuitBreaker(failure_threshold=1)
    cb.record_failure("test-health")
    evaluator = TriggerEvaluator(runbooks=[rb], on_triggered=callback, lock_manager=LockManager(), circuit_breaker=cb, rate_limiter=RateLimiter(), causal_detector=CausalChainDetector(), maintenance=MaintenanceManager(), state=OverseerState())
    await evaluator.tick()
    callback.assert_not_called()

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
