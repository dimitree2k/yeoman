"""Persisted reconnect budget for one account (Python twin of the bridge's connect_budget.ts).

At most ``per_hour`` attempts per hour and ``per_day`` per 24 h. Once the daily budget is
spent, one probe every ``probe_interval_s`` and nothing else; the budget is never
replayed. A connection that stays healthy for ``healthy_after_s`` (or an owner action)
resets it. The file is the memory, so restarting the process does not refill it.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

HOUR_S = 3600.0
DAY_S = 24 * HOUR_S


@dataclass(frozen=True, slots=True)
class BudgetDecision:
    action: Literal["connect", "wait"]
    wait_s: float = 0.0
    probe: bool = False
    why: str = ""


class ConnectBudget:
    def __init__(
        self,
        path: Path,
        *,
        per_hour: int = 5,
        per_day: int = 15,
        probe_interval_s: float = 8 * HOUR_S,
        healthy_after_s: float = 600.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = path
        self.per_hour = per_hour
        self.per_day = per_day
        self.probe_interval_s = probe_interval_s
        self.healthy_after_s = healthy_after_s
        self._clock = clock
        self._attempts: list[float] = []
        self._next_probe_at: float | None = None
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except OSError, ValueError:
            return
        if not isinstance(raw, dict):
            return
        attempts = raw.get("attempts")
        if isinstance(attempts, list):
            self._attempts = [float(t) for t in attempts if isinstance(t, int | float)]
        probe = raw.get("next_probe_at")
        self._next_probe_at = float(probe) if isinstance(probe, int | float) else None

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        raw = {"attempts": self._attempts, "next_probe_at": self._next_probe_at}
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=self.path.name, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(raw, fh)
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    @property
    def exhausted(self) -> bool:
        return self._next_probe_at is not None

    def attempts_within(self, window_s: float) -> int:
        cutoff = self._clock() - window_s
        return sum(1 for t in self._attempts if t > cutoff)

    def decide(self) -> BudgetDecision:
        now = self._clock()
        if self._next_probe_at is not None:
            if now >= self._next_probe_at:
                return BudgetDecision("connect", probe=True)
            return BudgetDecision("wait", wait_s=self._next_probe_at - now, why="budget_exhausted")
        self._attempts = [t for t in self._attempts if t > now - DAY_S]
        if len(self._attempts) >= self.per_day:
            self._next_probe_at = now + self.probe_interval_s
            self._save()
            return BudgetDecision("wait", wait_s=self.probe_interval_s, why="budget_exhausted")
        in_hour = sorted(t for t in self._attempts if t > now - HOUR_S)
        if len(in_hour) >= self.per_hour:
            return BudgetDecision(
                "wait", wait_s=max(1.0, in_hour[0] + HOUR_S - now), why="hourly_limit"
            )
        return BudgetDecision("connect")

    def record_attempt(self, *, probe: bool = False) -> None:
        now = self._clock()
        self._attempts.append(now)
        if probe and self._next_probe_at is not None:
            self._next_probe_at = now + self.probe_interval_s
        self._save()

    def reset(self) -> None:
        self._attempts = []
        self._next_probe_at = None
        self._save()
