"""Tests for the ops tool."""

from __future__ import annotations

import pytest

from yeoman.agent.tools.ops import OpsTool


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
