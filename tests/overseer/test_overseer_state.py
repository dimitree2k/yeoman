"""Tests for overseer state persistence."""
from __future__ import annotations
from pathlib import Path
from yeoman_overseer.state import OverseerState

def test_load_creates_default(tmp_path: Path) -> None:
    state = OverseerState.load(tmp_path / "state.json")
    assert state.heartbeat_ts is None
    assert state.locks == {}
    assert state.circuit_breakers == {}
    assert state.maintenance == {}
    assert state.budget == {"actions_hour": 0, "llm_daily": 0}

def test_save_and_reload(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    state = OverseerState.load(path)
    state.heartbeat_ts = "2026-03-18T04:00:00Z"
    state.budget["actions_hour"] = 5
    state.save(path)
    reloaded = OverseerState.load(path)
    assert reloaded.heartbeat_ts == "2026-03-18T04:00:00Z"
    assert reloaded.budget["actions_hour"] == 5

def test_record_action_increments_budget(tmp_path: Path) -> None:
    state = OverseerState.load(tmp_path / "state.json")
    state.record_action("gateway-health")
    assert state.budget["actions_hour"] == 1
    assert "gateway-health" in state.action_log[0]["runbook"]

def test_budget_reset(tmp_path: Path) -> None:
    state = OverseerState.load(tmp_path / "state.json")
    state.budget["actions_hour"] = 25
    state.budget["llm_daily"] = 15
    state.reset_hourly_budget()
    assert state.budget["actions_hour"] == 0
    assert state.budget["llm_daily"] == 15
