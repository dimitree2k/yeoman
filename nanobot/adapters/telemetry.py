"""Simple structured telemetry sink for vNext intents."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from loguru import logger


@dataclass(slots=True)
class InMemoryTelemetry:
    """In-memory counter sink with structured debug logging."""

    counters: Counter[str] = field(default_factory=Counter)

    def incr(self, name: str, value: int = 1, labels: tuple[tuple[str, str], ...] = ()) -> None:
        self.counters[name] += int(value)
        if labels:
            labels_text = ",".join(f"{k}={v}" for k, v in labels)
            logger.debug("telemetry {} += {} ({})", name, value, labels_text)
        else:
            logger.debug("telemetry {} += {}", name, value)
