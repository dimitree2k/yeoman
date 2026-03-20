"""Daily LLM budget tracker — token + call ceilings with date-based reset."""
from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from yeoman_overseer.state import OverseerState


class BudgetTracker:
    def __init__(
        self,
        state: OverseerState,
        *,
        calls_per_day: int,
        tokens_per_day: int,
    ) -> None:
        self._state = state
        self._calls_limit = calls_per_day
        self._tokens_limit = tokens_per_day

    def _reset_if_new_day(self) -> None:
        today = date.today().isoformat()
        if self._state.budget.get("budget_reset_date") != today:
            self._state.budget["tokens_daily"] = 0
            self._state.budget["llm_daily"] = 0
            self._state.budget["budget_reset_date"] = today

    def _pct(self) -> float:
        """Return the higher of token% and call% consumed today."""
        self._reset_if_new_day()
        token_pct = self._state.budget.get("tokens_daily", 0) / self._tokens_limit
        call_pct = self._state.budget.get("llm_daily", 0) / self._calls_limit
        return max(token_pct, call_pct)

    def can_call_llm(self, domain: str) -> bool:
        pct = self._pct()
        if pct >= 1.0:
            return False
        if pct >= 0.8 and domain != "health":
            return False
        return True

    def consume(self, tokens: int, calls: int) -> None:
        self._reset_if_new_day()
        self._state.budget["tokens_daily"] = (
            self._state.budget.get("tokens_daily", 0) + tokens
        )
        self._state.budget["llm_daily"] = (
            self._state.budget.get("llm_daily", 0) + calls
        )
