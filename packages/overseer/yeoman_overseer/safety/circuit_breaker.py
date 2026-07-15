"""Per-runbook circuit breaker with quarantine and exponential backoff."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class RunbookState(Enum):
    CLOSED = "closed"
    QUARANTINED = "quarantined"
    PERMANENTLY_DISABLED = "permanently_disabled"


@dataclass
class BreakerEntry:
    consecutive_failures: int = 0
    quarantine_count: int = 0
    state: RunbookState = RunbookState.CLOSED
    quarantined_at: float = 0.0
    manual_reset_required: bool = False


@dataclass
class CircuitBreaker:
    failure_threshold: int = 3
    max_quarantines: int = 3
    backoff_base_s: float = 3600.0

    _breakers: dict[str, BreakerEntry] = field(default_factory=dict)

    def _get(self, name: str) -> BreakerEntry:
        if name not in self._breakers:
            self._breakers[name] = BreakerEntry()
        return self._breakers[name]

    def record_failure(self, name: str, *, manual_reset_required: bool = False) -> None:
        entry = self._get(name)
        if entry.state == RunbookState.PERMANENTLY_DISABLED:
            return
        entry.consecutive_failures += 1
        if entry.consecutive_failures >= self.failure_threshold:
            entry.quarantine_count += 1
            if entry.quarantine_count > self.max_quarantines:
                entry.state = RunbookState.PERMANENTLY_DISABLED
            else:
                entry.state = RunbookState.QUARANTINED
                entry.quarantined_at = time.monotonic()
                entry.manual_reset_required = manual_reset_required
            entry.consecutive_failures = 0

    def record_success(self, name: str) -> None:
        entry = self._get(name)
        entry.consecutive_failures = 0
        entry.state = RunbookState.CLOSED
        entry.quarantined_at = 0.0
        entry.manual_reset_required = False

    def get_state(self, name: str) -> RunbookState:
        return self._get(name).state

    def get_quarantine_count(self, name: str) -> int:
        return self._get(name).quarantine_count

    def can_execute(self, name: str) -> bool:
        return self.get_state(name) == RunbookState.CLOSED

    def try_reenable(self, name: str) -> bool:
        entry = self._get(name)
        if entry.state != RunbookState.QUARANTINED:
            return False
        if entry.manual_reset_required:
            return False
        backoff = self.backoff_base_s * (2 ** (entry.quarantine_count - 1))
        elapsed = time.monotonic() - entry.quarantined_at
        if elapsed >= backoff:
            entry.state = RunbookState.CLOSED
            entry.consecutive_failures = 0
            return True
        return False

    def export_state(self) -> dict[str, Any]:
        return {
            name: {
                "consecutive_failures": e.consecutive_failures,
                "quarantine_count": e.quarantine_count,
                "state": e.state.value,
                "quarantined_at": e.quarantined_at,
                "manual_reset_required": e.manual_reset_required,
            }
            for name, e in self._breakers.items()
        }

    def import_state(self, data: dict[str, Any]) -> None:
        for name, raw in data.items():
            entry = BreakerEntry(
                consecutive_failures=raw["consecutive_failures"],
                quarantine_count=raw["quarantine_count"],
                state=RunbookState(raw["state"]),
                quarantined_at=raw.get("quarantined_at", 0.0),
                manual_reset_required=raw.get("manual_reset_required", False),
            )
            self._breakers[name] = entry
