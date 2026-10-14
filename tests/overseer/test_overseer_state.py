"""Tests for overseer state persistence."""
from __future__ import annotations

from pathlib import Path

import pytest
from yeoman_overseer.state import OverseerState


def test_load_creates_default(tmp_path: Path) -> None:
    state = OverseerState.load(tmp_path / "state.json")
    assert state.heartbeat_ts is None
    assert state.locks == {}
    assert state.circuit_breakers == {}
    assert state.maintenance == {}
    assert state.budget == {
        "actions_hour": 0, "llm_daily": 0,
        "tokens_daily": 0, "budget_reset_date": "",
    }

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


@pytest.mark.parametrize("content", ["", "{not json", "[1, 2]", "\xff\xfe"])
def test_unreadable_state_starts_from_defaults_and_is_kept_aside(
    tmp_path: Path, content: str
) -> None:
    path = tmp_path / "state.json"
    path.write_bytes(content.encode("latin-1"))
    state = OverseerState.load(path)
    assert state.heartbeat_ts is None and state.locks == {}
    assert not path.exists()
    kept = list(tmp_path.glob("state.json.corrupt-*"))
    assert len(kept) == 1 and kept[0].read_bytes() == content.encode("latin-1")
    state.save(path)
    assert OverseerState.load(path).heartbeat_ts is None


def test_save_replaces_atomically_and_leaves_no_temp_files(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    state = OverseerState.load(path)
    state.heartbeat_ts = "first"
    state.save(path)
    state.heartbeat_ts = "second"
    state.save(path)
    assert OverseerState.load(path).heartbeat_ts == "second"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["state.json"]
