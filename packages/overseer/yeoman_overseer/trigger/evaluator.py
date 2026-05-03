"""Trigger evaluator — schedules and dispatches runbook execution."""
from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from croniter import croniter

from yeoman_overseer.maintenance import MaintenanceManager
from yeoman_overseer.runbook.parser import Runbook
from yeoman_overseer.runbook.schema import TriggerCondition
from yeoman_overseer.safety.causal import CausalChainDetector
from yeoman_overseer.safety.circuit_breaker import CircuitBreaker
from yeoman_overseer.safety.rate_limiter import RateLimiter
from yeoman_overseer.state import OverseerState
from yeoman_overseer.trigger.checks import CheckResult, run_check
from yeoman_overseer.trigger.lock import LockManager

logger = logging.getLogger(__name__)


@dataclass
class TriggerEvaluator:
    runbooks: list[Runbook]
    on_triggered: Callable[[Runbook, CheckResult], Awaitable[None]]
    lock_manager: LockManager
    circuit_breaker: CircuitBreaker
    rate_limiter: RateLimiter
    causal_detector: CausalChainDetector
    maintenance: MaintenanceManager
    state: OverseerState

    _last_poll: dict[str, float] = field(default_factory=dict)
    _last_cron: dict[str, float] = field(default_factory=dict)
    _cooldown_until: dict[str, float] = field(default_factory=dict)

    async def tick(self) -> None:
        now = time.monotonic()
        wall = time.time()

        for rb in self.runbooks:
            name = rb.meta.name
            trigger = rb.meta.trigger

            if not rb.meta.enabled:
                continue
            if not self.circuit_breaker.can_execute(name):
                self.circuit_breaker.try_reenable(name)
                if not self.circuit_breaker.can_execute(name):
                    continue
            if now < self._cooldown_until.get(name, 0):
                continue
            if not self.rate_limiter.can_act(domain=rb.meta.domain):
                continue

            should_fire = False
            check_result = CheckResult(value=None)

            if trigger.kind == "poll":
                last = self._last_poll.get(name, 0)
                if now - last >= (trigger.interval_s or 30):
                    self._last_poll[name] = now
                    check_result = self._evaluate_condition(trigger.condition)
                    should_fire = self._condition_met(trigger.condition, check_result)

            elif trigger.kind == "cron":
                last_wall = self._last_cron.get(name, wall - 86400)
                cron = croniter(trigger.expr, last_wall)
                next_run = cron.get_next(float)
                if wall >= next_run:
                    self._last_cron[name] = wall
                    should_fire = True
                    check_result = CheckResult(value=True, detail="cron trigger")

            if not should_fire:
                continue

            if trigger.condition and self.maintenance.is_active(trigger.condition.target):
                continue

            if not self.lock_manager.acquire(name, name, exclusive=True):
                if rb.meta.safety.on_lock_conflict == "skip":
                    continue

            try:
                await self.on_triggered(rb, check_result)
                self.rate_limiter.record_action()
                self.state.record_action(name)
                self.circuit_breaker.record_success(name)
            except Exception as exc:
                logger.error("Runbook %s failed: %s", name, exc)
                self.circuit_breaker.record_failure(name)
            finally:
                self.lock_manager.release(name, name)
                self._cooldown_until[name] = now + rb.meta.safety.cooldown_s

    def _evaluate_condition(self, condition: TriggerCondition | None) -> CheckResult:
        if condition is None:
            return CheckResult(value=True)
        kwargs: dict[str, Any] = {"target": condition.target}
        if condition.window:
            kwargs["threshold"] = condition.window
        if condition.check in ("disk_usage_above",):
            kwargs["threshold"] = condition.value
        if condition.check in ("file_age_exceeds",):
            kwargs["threshold"] = str(condition.value)
        return run_check(condition.check, **kwargs)

    def _condition_met(self, condition: TriggerCondition | None, result: CheckResult) -> bool:
        if condition is None:
            return True
        op = condition.operator
        expected = condition.value
        actual = result.value
        if op == "==":
            return actual == expected
        elif op == "!=":
            return actual != expected
        elif op == ">":
            return actual > expected
        elif op == ">=":
            return actual >= expected
        elif op == "<":
            return actual < expected
        elif op == "<=":
            return actual <= expected
        return False
