"""Tests for the frozen, non-creating session-state path."""

from yeoman_gateway.knowledge._memory.session_state import resolve_session_state_dir


def test_default_session_state_is_under_runtime_data_without_creating_it(tmp_path, monkeypatch) -> None:
    runtime = tmp_path / "yeoman"
    workspace = runtime / "workspace"
    monkeypatch.setenv("YEOMAN_HOME", str(runtime))

    state_dir = resolve_session_state_dir(workspace)

    assert state_dir == runtime / "data/memory/session-state"
    assert not state_dir.exists()
    assert not (workspace / "memory/session-state").exists()


def test_explicit_relative_session_state_stays_workspace_relative(tmp_path) -> None:
    workspace = tmp_path / "workspace"

    state_dir = resolve_session_state_dir(workspace, state_dir="custom/session-state")

    assert state_dir == workspace / "custom/session-state"
    assert not state_dir.exists()


def test_explicit_absolute_session_state_is_preserved(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    target = tmp_path / "separate-state"

    state_dir = resolve_session_state_dir(workspace, state_dir=str(target))

    assert state_dir == target
    assert not state_dir.exists()
