"""Tests for SQLite telemetry persistence."""
from __future__ import annotations
from pathlib import Path
from yeoman_shared.telemetry.sqlite import SqliteTelemetry

def test_incr_and_read(tmp_path: Path) -> None:
    t = SqliteTelemetry(tmp_path / "metrics.db")
    t.incr("messages.inbound", 1, (("channel", "whatsapp"),))
    t.incr("messages.inbound", 1, (("channel", "whatsapp"),))
    t.incr("messages.inbound", 1, (("channel", "telegram"),))
    rows = t.query_counters("messages.inbound")
    assert len(rows) == 3

def test_gauge_and_read(tmp_path: Path) -> None:
    t = SqliteTelemetry(tmp_path / "metrics.db")
    t.gauge("sessions.active", 5.0)
    t.gauge("sessions.active", 3.0)
    rows = t.query_gauges("sessions.active", limit=1)
    assert len(rows) == 1
    assert rows[0]["value"] == 3.0

def test_counter_sum(tmp_path: Path) -> None:
    t = SqliteTelemetry(tmp_path / "metrics.db")
    t.incr("tool_calls.search", 3)
    t.incr("tool_calls.search", 7)
    total = t.counter_sum("tool_calls.search")
    assert total == 10
