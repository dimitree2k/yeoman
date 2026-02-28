"""In-memory telemetry backend for testing and development.

Simple drop-in replacement for PrometheusTelemetry that supports counters,
gauges, histograms, and timings for testing without external dependencies.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any


@dataclass
class InMemoryTelemetry:
    """In-memory telemetry backend for testing and development.

    Stores all metrics in memory for inspection during tests.
    """

    counters: dict[str, Counter] = field(default_factory=lambda: defaultdict(Counter))
    gauges: dict[str, float] = field(default_factory=dict)
    histograms: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    timings: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))

    def incr(self, name: str, value: int = 1, labels: tuple[tuple[str, str], ...] = ()) -> None:
        """Increase a named counter."""
        key = self._make_key(name, labels)
        self.counters[key][name] += value

    def gauge(self, name: str, value: float, labels: tuple[tuple[str, str], ...] = ()) -> None:
        """Set a gauge value."""
        key = self._make_key(name, labels)
        self.gauges[key] = value

    def histogram(
        self, name: str, value: float, labels: tuple[tuple[str, str], ...] = ()
    ) -> None:
        """Observe a histogram value."""
        key = self._make_key(name, labels)
        self.histograms[key].append(value)

    def timing(
        self, name: str, value: float, labels: tuple[tuple[str, str], ...] = ()
    ) -> None:
        """Record timing in seconds."""
        key = self._make_key(name, labels)
        self.timings[key].append(value)

    def _make_key(self, name: str, labels: tuple[tuple[str, str], ...] | None) -> str:
        """Create a unique key for a metric with labels."""
        if not labels:
            return name
        label_str = ",".join(f"{k}={v}" for k, v in labels)
        return f"{name}{{{label_str}}}"

    # ── Test helpers ─────────────────────────────────────────────────────

    def get_counter(self, name: str, labels: tuple[tuple[str, str], ...] = ()) -> int:
        """Get counter value for testing."""
        key = self._make_key(name, labels)
        return int(self.counters[key][name])

    def get_gauge(self, name: str, labels: tuple[tuple[str, str], ...] = ()) -> float | None:
        """Get gauge value for testing."""
        key = self._make_key(name, labels)
        return self.gauges.get(key)

    def get_histogram_values(
        self, name: str, labels: tuple[tuple[str, str], ...] = ()
    ) -> list[float]:
        """Get histogram values for testing."""
        key = self._make_key(name, labels)
        return list(self.histograms[key])

    def get_timing_values(self, name: str, labels: tuple[tuple[str, str], ...] = ()) -> list[float]:
        """Get timing values for testing."""
        key = self._make_key(name, labels)
        return list(self.timings[key])

    def reset(self) -> None:
        """Clear all metrics."""
        self.counters.clear()
        self.gauges.clear()
        self.histograms.clear()
        self.timings.clear()
