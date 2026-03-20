"""Overseer state persistence — state.json R/W."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class OverseerState:
    """Mutable overseer state, persisted to state.json."""

    heartbeat_ts: str | None = None
    locks: dict[str, Any] = field(default_factory=dict)
    circuit_breakers: dict[str, Any] = field(default_factory=dict)
    maintenance: dict[str, Any] = field(default_factory=dict)
    budget: dict[str, Any] = field(
        default_factory=lambda: {
            "actions_hour": 0,
            "llm_daily": 0,
            "tokens_daily": 0,
            "budget_reset_date": "",
        }
    )
    action_log: list[dict[str, Any]] = field(default_factory=list)
    causal_graph: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> OverseerState:
        """Load from JSON file, or return defaults if missing."""
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            return cls(
                heartbeat_ts=raw.get("heartbeat_ts"),
                locks=raw.get("locks", {}),
                circuit_breakers=raw.get("circuit_breakers", {}),
                maintenance=raw.get("maintenance", {}),
                budget=raw.get("budget", {
                    "actions_hour": 0, "llm_daily": 0,
                    "tokens_daily": 0, "budget_reset_date": "",
                }),
                action_log=raw.get("action_log", []),
                causal_graph=raw.get("causal_graph", {}),
            )
        return cls()

    def save(self, path: Path) -> None:
        """Persist to JSON file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "heartbeat_ts": self.heartbeat_ts,
            "locks": self.locks,
            "circuit_breakers": self.circuit_breakers,
            "maintenance": self.maintenance,
            "budget": self.budget,
            "action_log": self.action_log,
            "causal_graph": self.causal_graph,
        }
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    def record_action(self, runbook_name: str) -> None:
        """Record an action for budget tracking."""
        self.budget["actions_hour"] = self.budget.get("actions_hour", 0) + 1
        self.action_log.append({
            "runbook": runbook_name,
            "ts": time.time(),
        })

    def reset_hourly_budget(self) -> None:
        """Reset the hourly action counter."""
        self.budget["actions_hour"] = 0
