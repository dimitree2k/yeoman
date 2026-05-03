"""Global rate limiter — caps actions and LLM calls across all runbooks."""
from __future__ import annotations

from dataclasses import dataclass

CRITICAL_DOMAINS = frozenset({"health"})


@dataclass
class RateLimiter:
    actions_per_hour: int = 30
    llm_calls_per_day: int = 80
    _action_count: int = 0
    _llm_count: int = 0

    def record_action(self) -> None:
        self._action_count += 1

    def record_llm_call(self) -> None:
        self._llm_count += 1

    def can_act(self, *, domain: str = "") -> bool:
        if self._action_count < self.actions_per_hour:
            return True
        return domain in CRITICAL_DOMAINS

    def can_llm(self, *, domain: str = "") -> bool:
        if self._llm_count >= self.llm_calls_per_day:
            return False
        if self._llm_count >= self.llm_calls_per_day * 0.8:
            return domain in CRITICAL_DOMAINS
        return True

    def reset_hourly(self) -> None:
        self._action_count = 0

    def reset_daily(self) -> None:
        self._llm_count = 0

    def remaining(self) -> dict[str, int]:
        return {
            "actions_hour": max(0, self.actions_per_hour - self._action_count),
            "llm_daily": max(0, self.llm_calls_per_day - self._llm_count),
        }
