"""Tests for the ops tool."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from yeoman.agent.tools.ops import OpsTool, _parse_loguru_line, _parse_time_spec


@pytest.mark.asyncio
async def test_system_stats_returns_text():
    tool = OpsTool()
    result = await tool.execute(action="system_stats")
    assert isinstance(result, str)
    assert "System Stats" in result  # NB: header changed from pi_stats' "Raspberry Pi Stats"
    assert "cpu_usage_pct" in result
    assert "memory_total_mb" in result
    assert "disk_root_total_gb" in result
    assert "uptime_seconds" in result


@pytest.mark.asyncio
async def test_system_stats_includes_top_processes():
    tool = OpsTool()
    result = await tool.execute(action="system_stats")
    assert "top_processes" in result


def test_ops_tool_name():
    tool = OpsTool()
    assert tool.name == "ops"


def test_ops_tool_schema_has_action_enum():
    tool = OpsTool()
    schema = tool.parameters
    action_schema = schema["properties"]["action"]
    assert "log_scan" in action_schema["enum"]
    assert "service_status" in action_schema["enum"]
    assert "system_stats" in action_schema["enum"]


# ── _parse_time_spec tests ────────────────────────────────────


def test_parse_time_spec_duration_hours():
    now = datetime(2026, 3, 13, 12, 0, 0, tzinfo=timezone.utc)
    result = _parse_time_spec("1h", now=now)
    assert result == datetime(2026, 3, 13, 11, 0, 0, tzinfo=timezone.utc)


def test_parse_time_spec_duration_minutes():
    now = datetime(2026, 3, 13, 12, 0, 0, tzinfo=timezone.utc)
    result = _parse_time_spec("30m", now=now)
    assert result == datetime(2026, 3, 13, 11, 30, 0, tzinfo=timezone.utc)


def test_parse_time_spec_duration_days():
    now = datetime(2026, 3, 13, 12, 0, 0, tzinfo=timezone.utc)
    result = _parse_time_spec("2d", now=now)
    assert result == datetime(2026, 3, 11, 12, 0, 0, tzinfo=timezone.utc)


def test_parse_time_spec_absolute():
    result = _parse_time_spec("2026-03-13 10:00")
    assert result.year == 2026
    assert result.month == 3
    assert result.hour == 10


# ── _parse_loguru_line tests ──────────────────────────────────


def test_parse_loguru_line_extracts_fields():
    line = "2026-03-13 12:47:33.852 | WARNING  | yeoman.channels.telegram:func:42 - Some message"
    ts, level, msg = _parse_loguru_line(line)
    assert ts is not None
    assert ts.hour == 12
    assert ts.minute == 47
    assert level == "WARNING"
    assert "Some message" in msg


def test_parse_loguru_line_returns_none_for_non_loguru():
    ts, level, msg = _parse_loguru_line("some random text")
    assert ts is None
    assert level is None
    assert msg == "some random text"


# ── log_scan tests ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_log_scan_missing_service():
    tool = OpsTool()
    result = await tool.execute(action="log_scan")
    assert "service" in result.lower() or "required" in result.lower()


@pytest.mark.asyncio
async def test_log_scan_missing_log_file(tmp_path):
    tool = OpsTool()
    with patch("yeoman.agent.tools.ops._GATEWAY_LOG", tmp_path / "nonexistent.log"):
        result = await tool.execute(action="log_scan", service="gateway")
    assert "No log file found" in result


@pytest.mark.asyncio
async def test_log_scan_filters_by_level(tmp_path):
    log_file = tmp_path / "gateway.log"
    log_file.write_text(
        "2026-03-13 10:00:00.000 | DEBUG    | mod:fn:1 - debug line\n"
        "2026-03-13 10:00:01.000 | INFO     | mod:fn:2 - info line\n"
        "2026-03-13 10:00:02.000 | WARNING  | mod:fn:3 - warning line\n"
        "2026-03-13 10:00:03.000 | ERROR    | mod:fn:4 - error line\n"
    )
    tool = OpsTool()
    with patch("yeoman.agent.tools.ops._GATEWAY_LOG", log_file):
        result = await tool.execute(
            action="log_scan", service="gateway", level="warning", since="1d"
        )
    assert "warning line" in result
    assert "error line" in result
    assert "debug line" not in result
    assert "info line" not in result


@pytest.mark.asyncio
async def test_log_scan_filters_by_keyword(tmp_path):
    log_file = tmp_path / "gateway.log"
    log_file.write_text(
        "2026-03-13 10:00:00.000 | ERROR    | mod:fn:1 - connection refused\n"
        "2026-03-13 10:00:01.000 | ERROR    | mod:fn:2 - disk full\n"
    )
    tool = OpsTool()
    with patch("yeoman.agent.tools.ops._GATEWAY_LOG", log_file):
        result = await tool.execute(
            action="log_scan", service="gateway", keyword="connection", since="1d"
        )
    assert "connection refused" in result
    assert "disk full" not in result


@pytest.mark.asyncio
async def test_log_scan_respects_limit(tmp_path):
    log_file = tmp_path / "gateway.log"
    lines = [
        f"2026-03-13 10:00:{i:02d}.000 | ERROR    | mod:fn:{i} - error {i}\n"
        for i in range(20)
    ]
    log_file.write_text("".join(lines))
    tool = OpsTool()
    with patch("yeoman.agent.tools.ops._GATEWAY_LOG", log_file):
        result = await tool.execute(
            action="log_scan", service="gateway", level="error", since="1d", limit=5
        )
    log_lines = [line for line in result.splitlines() if "error " in line]
    assert len(log_lines) == 5


@pytest.mark.asyncio
async def test_log_scan_wraps_output_with_untrusted_header(tmp_path):
    log_file = tmp_path / "gateway.log"
    log_file.write_text(
        "2026-03-13 10:00:00.000 | ERROR    | mod:fn:1 - test error\n"
    )
    tool = OpsTool()
    with patch("yeoman.agent.tools.ops._GATEWAY_LOG", log_file):
        result = await tool.execute(
            action="log_scan", service="gateway", level="error", since="1d"
        )
    assert "[LOG OUTPUT" in result
    assert "untrusted" in result.lower()
    assert "[END LOG OUTPUT" in result


@pytest.mark.asyncio
async def test_log_scan_bridge_keyword_filter(tmp_path):
    log_file = tmp_path / "bridge.log"
    log_file.write_text(
        "yeoman WhatsApp Bridge\n"
        "=======================\n"
        "host=127.0.0.1 port=3001\n"
        "connection error: auth failed\n"
    )
    tool = OpsTool()
    with patch("yeoman.agent.tools.ops._BRIDGE_LOG", log_file):
        result = await tool.execute(
            action="log_scan", service="bridge", keyword="auth", since="1d"
        )
    assert "auth failed" in result
    assert "host=127.0.0.1" not in result


@pytest.mark.asyncio
async def test_log_scan_filters_by_time_range(tmp_path):
    log_file = tmp_path / "gateway.log"
    log_file.write_text(
        "2026-03-13 08:00:00.000 | ERROR    | mod:fn:1 - old error\n"
        "2026-03-13 11:00:00.000 | ERROR    | mod:fn:2 - recent error\n"
        "2026-03-13 11:30:00.000 | ERROR    | mod:fn:3 - very recent error\n"
    )
    tool = OpsTool()
    with patch("yeoman.agent.tools.ops._GATEWAY_LOG", log_file):
        result = await tool.execute(
            action="log_scan",
            service="gateway",
            level="error",
            since="2026-03-13 10:00",
            until="2026-03-13 11:15",
        )
    assert "recent error" in result
    assert "old error" not in result
    assert "very recent error" not in result


# ── service_status tests ─────────────────────────────────────


@pytest.mark.asyncio
async def test_service_status_all_reports_both(tmp_path):
    tool = OpsTool()
    with patch("yeoman.agent.tools.ops._GATEWAY_PID", tmp_path / "gw.pid"), \
         patch("yeoman.agent.tools.ops._BRIDGE_PID", tmp_path / "br.pid"):
        result = await tool.execute(action="service_status", service="all")
    assert "gateway" in result.lower()
    assert "bridge" in result.lower()


@pytest.mark.asyncio
async def test_service_status_stopped_no_pid_file(tmp_path):
    tool = OpsTool()
    with patch("yeoman.agent.tools.ops._GATEWAY_PID", tmp_path / "nonexistent.pid"):
        result = await tool.execute(action="service_status", service="gateway")
    assert "stopped" in result.lower()


@pytest.mark.asyncio
async def test_service_status_stale_pid(tmp_path):
    pid_file = tmp_path / "gateway.pid"
    pid_file.write_text("999999")
    tool = OpsTool()
    with patch("yeoman.agent.tools.ops._GATEWAY_PID", pid_file), \
         patch("yeoman.agent.tools.ops.pid_alive", return_value=False):
        result = await tool.execute(action="service_status", service="gateway")
    assert "stale" in result.lower() or "stopped" in result.lower()
