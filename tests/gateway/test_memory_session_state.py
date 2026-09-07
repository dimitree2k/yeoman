"""Tests for session-state path ownership and persistence."""

from yeoman_gateway.adapters.policy_engine import EnginePolicyAdapter
from yeoman_gateway.memory.session_state import SessionStateStore
from yeoman_shared.config.defaults import DEFAULT_SESSION_STATE_DIR


def test_default_session_state_is_under_runtime_data(tmp_path, monkeypatch) -> None:
    runtime = tmp_path / "yeoman"
    workspace = runtime / "workspace"
    monkeypatch.setenv("YEOMAN_HOME", str(runtime))

    store = SessionStateStore(workspace)

    assert store.state_dir == runtime / "data/memory/session-state"
    assert not (workspace / "memory/session-state").exists()


def test_explicit_relative_session_state_stays_workspace_relative(tmp_path) -> None:
    workspace = tmp_path / "workspace"

    store = SessionStateStore(workspace, state_dir="custom/session-state")

    assert store.state_dir == workspace / "custom/session-state"


def test_explicit_absolute_session_state_is_preserved(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    target = tmp_path / "separate-state"

    store = SessionStateStore(workspace, state_dir=str(target))

    assert store.state_dir == target


def test_policy_adapter_uses_same_default_session_state_root(tmp_path, monkeypatch) -> None:
    runtime = tmp_path / "yeoman"
    workspace = runtime / "workspace"
    monkeypatch.setenv("YEOMAN_HOME", str(runtime))
    adapter = object.__new__(EnginePolicyAdapter)
    adapter._workspace = workspace
    adapter._memory_state_dir = DEFAULT_SESSION_STATE_DIR

    assert adapter._session_wal_path("cli:default") == (
        runtime / "data/memory/session-state/cli_default.md"
    )
