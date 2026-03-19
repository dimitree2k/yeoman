"""Maintenance mode manager — suppresses false alerts during planned restarts."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class MaintenanceEntry:
    started_at: float
    timeout_s: float
    reason: str


@dataclass
class MaintenanceManager:
    _entries: dict[str, MaintenanceEntry] = field(default_factory=dict)

    def enter(self, service: str, *, timeout_s: float, reason: str) -> None:
        self._entries[service] = MaintenanceEntry(
            started_at=time.monotonic(), timeout_s=timeout_s, reason=reason
        )

    def exit(self, service: str) -> None:
        self._entries.pop(service, None)

    def is_active(self, service: str) -> bool:
        entry = self._entries.get(service)
        if entry is None:
            return False
        elapsed = time.monotonic() - entry.started_at
        if elapsed >= entry.timeout_s:
            del self._entries[service]
            return False
        return True

    def get_active(self) -> dict[str, MaintenanceEntry]:
        expired = [s for s in self._entries if not self.is_active(s)]
        for s in expired:
            self._entries.pop(s, None)
        return dict(self._entries)

    def export_state(self) -> dict[str, Any]:
        return {
            service: {
                "started_at": entry.started_at,
                "timeout_s": entry.timeout_s,
                "reason": entry.reason,
            }
            for service, entry in self._entries.items()
        }

    def import_state(self, data: dict[str, Any]) -> None:
        for service, raw in data.items():
            self._entries[service] = MaintenanceEntry(
                started_at=raw["started_at"],
                timeout_s=raw["timeout_s"],
                reason=raw["reason"],
            )
