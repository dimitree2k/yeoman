"""Prometheus metrics backend for yeoman observability.

Provides a Prometheus-compatible metrics endpoint for production monitoring.
Metrics are exposed at /metrics for scraping by Prometheus server.

Usage:
    telemetry = PrometheusTelemetry(port=8080)
    telemetry.incr("llm_requests_total", labels=(("model", "gpt-4"),))
    telemetry.timing("llm_request_duration_seconds", 1.5)

    # Metrics available at http://localhost:8080/metrics
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from loguru import logger

if TYPE_CHECKING:
    from prometheus_client import Counter, Gauge, Histogram


@dataclass
class PrometheusConfig:
    """Configuration for Prometheus telemetry backend."""

    enabled: bool = True
    port: int = 8080
    host: str = "127.0.0.1"  # localhost only by default for security


class PrometheusTelemetry:
    """Prometheus-backed telemetry with /metrics endpoint.

    This adapter:
    - Registers standard yeoman metrics
    - Exposes /metrics endpoint on localhost:8080 by default
    - Thread-safe metrics collection
    - Supports counters, gauges, histograms, and timings

    Security: Binds to localhost only by default. Override with config.host
    if you need external access (not recommended).
    """

    def __init__(self, config: PrometheusConfig | None = None) -> None:
        self._config = config or PrometheusConfig()
        self._metrics: dict[str, Counter | Gauge | Histogram] = {}
        self._started = False

        if not self._config.enabled:
            logger.info("Prometheus telemetry disabled")
            return

        try:
            from prometheus_client import (
                Counter,
                Gauge,
                Histogram,
                start_http_server,
            )
        except ImportError:
            logger.warning(
                "prometheus_client not installed; PrometheusTelemetry will be a no-op. "
                "Install with: pip install prometheus_client"
            )
            self._config.enabled = False
            return

        self._Counter = Counter
        self._Gauge = Gauge
        self._Histogram = Histogram
        self._start_http_server = start_http_server

        # Register standard yeoman metrics
        self._register_standard_metrics()

    def _register_standard_metrics(self) -> None:
        """Register standard yeoman metrics."""
        # LLM metrics
        self._metrics["llm_requests_total"] = self._Counter(
            "yeoman_llm_requests_total",
            "Total LLM requests",
            labelnames=["model", "provider", "channel"],
        )
        self._metrics["llm_tokens_total"] = self._Counter(
            "yeoman_llm_tokens_total",
            "Total LLM tokens generated",
            labelnames=["model", "provider", "channel", "kind"],  # kind=prompt/completion
        )
        self._metrics["llm_cost_dollars"] = self._Counter(
            "yeoman_llm_cost_dollars",
            "Estimated LLM cost in dollars",
            labelnames=["model", "provider", "channel"],
        )
        self._metrics["llm_request_duration_seconds"] = self._Histogram(
            "yeoman_llm_request_duration_seconds",
            "LLM request duration in seconds",
            labelnames=["model", "provider", "channel"],
            buckets=[0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0, 120.0],
        )

        # Tool metrics
        self._metrics["tool_calls_total"] = self._Counter(
            "yeoman_tool_calls_total",
            "Total tool calls executed",
            labelnames=["tool", "channel", "status"],  # status=success/error
        )

        # Event metrics
        self._metrics["events_total"] = self._Counter(
            "yeoman_events_total",
            "Total events processed",
            labelnames=["type", "channel"],
        )
        self._metrics["events_dropped_total"] = self._Counter(
            "yeoman_events_dropped_total",
            "Total events dropped",
            labelnames=["reason", "channel"],
        )

        # Memory metrics
        self._metrics["memory_notes_total"] = self._Counter(
            "yeoman_memory_notes_total",
            "Total memory notes captured",
            labelnames=["mode", "channel"],
        )
        self._metrics["memory_recall_total"] = self._Counter(
            "yeoman_memory_recall_total",
            "Total memory recall operations",
            labelnames=["channel", "status"],  # status=hit/miss
        )

        # Queue metrics
        self._metrics["queue_size"] = self._Gauge(
            "yeoman_queue_size",
            "Queue size",
            labelnames=["queue", "channel"],  # queue=inbound/outbound
        )

        # Response metrics
        self._metrics["response_size_bytes"] = self._Histogram(
            "yeoman_response_size_bytes",
            "Response size in bytes",
            labelnames=["channel"],
            buckets=[100, 500, 1000, 5000, 10000, 50000, 100000],
        )

    def start(self) -> None:
        """Start the Prometheus HTTP server."""
        if not self._config.enabled or self._started:
            return

        try:
            self._start_http_server(
                port=self._config.port,
                host=self._config.host,
            )
            self._started = True
            logger.info(
                f"Prometheus metrics server started on "
                f"http://{self._config.host}:{self._config.port}/metrics"
            )
        except Exception as e:
            logger.error(f"Failed to start Prometheus server: {e}")
            self._config.enabled = False

    def incr(self, name: str, value: int = 1, labels: tuple[tuple[str, str], ...] = ()) -> None:
        """Increase a named counter."""
        if not self._config.enabled:
            return

        metric = self._metrics.get(name)
        if metric is None:
            # Create ad-hoc counter
            labelnames = [k for k, _ in labels] if labels else []
            metric = self._Counter(
                f"yeoman_{name}",
                f"Counter: {name}",
                labelnames=labelnames,
            )
            self._metrics[name] = metric

        if labels:
            label_dict = dict(labels)
            metric.labels(**label_dict).inc(value)
        else:
            metric.inc(value)

    def gauge(self, name: str, value: float, labels: tuple[tuple[str, str], ...] = ()) -> None:
        """Set a gauge value."""
        if not self._config.enabled:
            return

        metric = self._metrics.get(name)
        if metric is None:
            # Create ad-hoc gauge
            labelnames = [k for k, _ in labels] if labels else []
            metric = self._Gauge(
                f"yeoman_{name}",
                f"Gauge: {name}",
                labelnames=labelnames,
            )
            self._metrics[name] = metric

        if labels:
            label_dict = dict(labels)
            metric.labels(**label_dict).set(value)
        else:
            metric.set(value)

    def histogram(
        self, name: str, value: float, labels: tuple[tuple[str, str], ...] = ()
    ) -> None:
        """Observe a histogram value."""
        if not self._config.enabled:
            return

        metric = self._metrics.get(name)
        if metric is None:
            # Create ad-hoc histogram
            labelnames = [k for k, _ in labels] if labels else []
            metric = self._Histogram(
                f"yeoman_{name}",
                f"Histogram: {name}",
                labelnames=labelnames,
            )
            self._metrics[name] = metric

        if labels:
            label_dict = dict(labels)
            metric.labels(**label_dict).observe(value)
        else:
            metric.observe(value)

    def timing(
        self, name: str, value: float, labels: tuple[tuple[str, str], ...] = ()
    ) -> None:
        """Record timing in seconds (alias for histogram)."""
        self.histogram(name, value, labels)

    @contextmanager
    def timeit(self, name: str, labels: tuple[tuple[str, str], ...] = ()):
        """Context manager to time a block of code."""
        start = time.monotonic()
        yield
        elapsed = time.monotonic() - start
        self.timing(name, elapsed, labels)
