"""Per-session WAL state files for memory durability."""

from __future__ import annotations

from pathlib import Path

from yeoman_shared.config.defaults import DEFAULT_SESSION_STATE_DIR
from yeoman_shared.utils.helpers import get_session_state_path


def resolve_session_state_dir(workspace: Path, state_dir: str = DEFAULT_SESSION_STATE_DIR) -> Path:
    """Resolve the frozen session-state location without creating it."""
    relative = Path(state_dir).expanduser()
    if state_dir == DEFAULT_SESSION_STATE_DIR:
        return get_session_state_path()
    return relative if relative.is_absolute() else workspace / relative
