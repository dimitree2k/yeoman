"""Utility functions for yeoman."""

import os
from collections.abc import MutableMapping
from datetime import datetime
from pathlib import Path
from typing import Any

OPERATIONAL_STORE_PATHS = {
    "session_metadata": "ops/session-metadata.db",
    "processing": "ops/processing.db",
    "speakups": "ops/speakups.db",
    "burst_state": "ops/burst-state.json",
    "lull_state": "ops/lull-state.json",
    "pending_approvals": "ops/pending-approvals.json",
    "document_cache": "ops/document-cache.db",
    "cron": "ops/cron-jobs.json",
    "response_pauses": "ops/response-pauses.json",
    "policy_audit": "ops/policy-audit",
    "bridge_references": "ops/bridge-message-references",
    "a2a": "ops/a2a.db",
    "seen_chats": "ops/seen-chats.json",
    "overseer": "ops/overseer",
    "trends": "ops/trends.db",
    "meme_quota": "ops/meme-quota.json",
    "poll_guard": "ops/poll-guard.json",
    "group_reminder_quota": "ops/group-reminder-quota.json",
    "group_decisions": "ops/group-decisions.db",
}



def ensure_dir(path: Path) -> Path:
    """Ensure a directory exists, creating it if necessary."""
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_yeoman_home() -> Path:
    """Return YEOMAN_HOME, or the legacy ~/.yeoman runtime directory."""
    yeoman_home = os.environ.get("YEOMAN_HOME", "").strip()
    return Path(yeoman_home).expanduser() if yeoman_home else Path.home() / ".yeoman"


def resolve_yeoman_path(path: str | Path, *, is_default: bool = False) -> Path:
    """Expand a legacy ~/.yeoman default through YEOMAN_HOME when provenance says default."""
    value = str(path)
    prefix = "~/.yeoman"
    if is_default and (value == prefix or value.startswith(prefix + "/")):
        return get_yeoman_home() / value[len(prefix) :].lstrip("/")
    return Path(path).expanduser()


def resolve_config_path(config: Any, name: str) -> Path:
    """Resolve a path field, honoring the legacy default only when it was omitted."""
    return resolve_yeoman_path(
        getattr(config, name), is_default=name not in config.model_fields_set
    )


def get_data_path() -> Path:
    """Get the yeoman data directory.

    Respects YEOMAN_HOME environment variable; falls back to ~/.yeoman.
    """
    return ensure_dir(get_yeoman_home())


def get_var_path() -> Path:
    """Get the ephemeral state directory (~/.yeoman/var)."""
    return ensure_dir(get_data_path() / "var")


def get_secrets_path() -> Path:
    """Get the secrets directory (~/.yeoman/secrets), chmod 0700."""
    path = ensure_dir(get_data_path() / "secrets")
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def get_operational_data_path() -> Path:
    """Get the long-lived operational data directory (~/.yeoman/data)."""
    return ensure_dir(get_data_path() / "data")


def get_logs_path() -> Path:
    """Get the logs directory (~/.yeoman/var/logs)."""
    return ensure_dir(get_var_path() / "logs")


def get_run_path() -> Path:
    """Get the canonical PID/socket run directory (~/.yeoman/run)."""
    return ensure_dir(get_data_path() / "run")


def get_session_state_path() -> Path:
    """Return the frozen legacy session-state path without creating it."""
    return get_yeoman_home() / "data" / "memory" / "session-state"


def get_operational_store_path(
    name: str, *, data_dir: Path | None = None, create: bool = True
) -> Path:
    """Resolve a named store under data/ops, optionally without creating directories."""
    try:
        relative = OPERATIONAL_STORE_PATHS[name]
    except KeyError:
        raise ValueError(f"Unknown operational store: {name}") from None
    if data_dir is not None:
        base = data_dir
    elif create:
        base = get_operational_data_path()
    else:
        base = get_yeoman_home() / "data"
    return base / relative


def set_bridge_message_reference_dir(env: MutableMapping[str, str]) -> None:
    """Normalize the bridge path override or supply its YEOMAN_HOME-aware default."""
    env["BRIDGE_MESSAGE_REFERENCE_DIR"] = env.get("BRIDGE_MESSAGE_REFERENCE_DIR", "").strip() or str(
        get_operational_store_path("bridge_references")
    )


def get_cache_path() -> Path:
    """Get the cache directory (~/.yeoman/var/cache)."""
    return ensure_dir(get_var_path() / "cache")


def get_workspace_path(workspace: str | None = None) -> Path:
    """Get the workspace path.

    Args:
        workspace: Optional workspace path. Defaults to ~/.yeoman/workspace.

    Returns:
        Expanded and ensured workspace path.
    """
    base = get_data_path()
    if workspace:
        candidate = Path(workspace).expanduser()
        path = candidate if candidate.is_absolute() else base / candidate
    else:
        path = base / "workspace"
    return ensure_dir(path)


def get_sessions_path(*, create: bool = True) -> Path:
    """Get the session history directory (~/.yeoman/data/inbound)."""
    if create:
        return ensure_dir(get_operational_data_path() / "inbound")
    return get_yeoman_home() / "data" / "inbound"


def get_memory_path(workspace: Path | None = None) -> Path:
    """Get the memory directory.

    When a workspace is provided, the memory sub-directory lives inside it
    (for WAL session state). Otherwise returns the operational data memory dir.
    """
    if workspace is not None:
        return ensure_dir(workspace / "memory")
    return ensure_dir(get_operational_data_path() / "memory")


def get_skills_path(workspace: Path | None = None) -> Path:
    """Get the skills directory within the workspace."""
    ws = workspace or get_workspace_path()
    return ensure_dir(ws / "skills")


def today_date() -> str:
    """Get today's date in YYYY-MM-DD format."""
    return datetime.now().strftime("%Y-%m-%d")


def timestamp() -> str:
    """Get current timestamp in ISO format."""
    return datetime.now().isoformat()


def truncate_string(s: str, max_len: int = 100, suffix: str = "...") -> str:
    """Truncate a string to max length, adding suffix if truncated."""
    if len(s) <= max_len:
        return s
    return s[: max_len - len(suffix)] + suffix


def safe_filename(name: str) -> str:
    """Convert a string to a safe filename."""
    unsafe = '<>:"/\\|?*'
    for char in unsafe:
        name = name.replace(char, "_")
    return name.strip()


def parse_session_key(key: str) -> tuple[str, str]:
    """Parse a session key into channel and chat_id.

    Args:
        key: Session key in format "channel:chat_id"

    Returns:
        Tuple of (channel, chat_id)
    """
    parts = key.split(":", 1)
    if len(parts) != 2:
        raise ValueError(f"Invalid session key: {key}")
    return parts[0], parts[1]
