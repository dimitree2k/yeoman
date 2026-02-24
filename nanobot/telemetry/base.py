"""Base telemetry port protocol."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class TelemetryPort(Protocol):
    """Protocol for telemetry backends (Prometheus, in-memory, etc.).

    The telemetry port provides a simple interface for recording metrics:
    - Counters: Monotonically increasing values (requests, errors, tokens)
    - Gauges: Point-in-time values (queue size, active sessions)
    - Histograms: Distribution of values (latency, response size)
    - Timing: Duration measurements
    """

    def incr(self, name: str, value: int = 1, labels: tuple[tuple[str, str], ...] = ()) -> None:
        """Increase a named counter by ``value`` with optional labels.

        Args:
            name: Metric name (e.g., "llm_requests_total")
            value: Amount to increment (default 1)
            labels: Optional label tuples (e.g., (("model", "gpt-4"), ("channel", "whatsapp")))
        """

    def gauge(self, name: str, value: float, labels: tuple[tuple[str, str], ...] = ()) -> None:
        """Set a gauge value.

        Args:
            name: Metric name (e.g., "active_sessions")
            value: Current value
            labels: Optional label tuples
        """

    def histogram(
        self, name: str, value: float, labels: tuple[tuple[str, str], ...] = ()
    ) -> None:
        """Observe a histogram value.

        Args:
            name: Metric name (e.g., "request_duration_seconds")
            value: Observed value
            labels: Optional label tuples
        """

    def timing(
        self, name: str, value: float, labels: tuple[tuple[str, str], ...] = ()
    ) -> None:
        """Record timing of an operation in seconds.

        Args:
            name: Metric name (e.g., "llm_request_duration_seconds")
            value: Duration in seconds
            labels: Optional label tuples
        """
