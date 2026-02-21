"""Utility functions for nanobot."""

import os
from datetime import datetime
from pathlib import Path


def ensure_dir(path: Path) -> Path:
    """Ensure a directory exists, creating it if necessary."""
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_data_path() -> Path:
    """Get the nanobot data directory.

    Respects NANOBOT_HOME environment variable; falls back to ~/.nanobot.
    """
    nanobot_home = os.environ.get("NANOBOT_HOME", "").strip()
    if nanobot_home:
        return ensure_dir(Path(nanobot_home))
    return ensure_dir(Path.home() / ".nanobot")


def get_var_path() -> Path:
    """Get the ephemeral state directory (~/.nanobot/var)."""
    return ensure_dir(get_data_path() / "var")


def get_secrets_path() -> Path:
    """Get the secrets directory (~/.nanobot/secrets), chmod 0700."""
    path = ensure_dir(get_data_path() / "secrets")
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def get_operational_data_path() -> Path:
    """Get the long-lived operational data directory (~/.nanobot/data)."""
    return ensure_dir(get_data_path() / "data")


def get_logs_path() -> Path:
    """Get the logs directory (~/.nanobot/var/logs)."""
    return ensure_dir(get_var_path() / "logs")


def get_run_path() -> Path:
    """Get the PID/socket run directory (~/.nanobot/var/run)."""
    return ensure_dir(get_var_path() / "run")


def get_cache_path() -> Path:
    """Get the cache directory (~/.nanobot/var/cache)."""
    return ensure_dir(get_var_path() / "cache")


def get_workspace_path(workspace: str | None = None) -> Path:
    """Get the workspace path.

    Args:
        workspace: Optional workspace path. Defaults to ~/.nanobot/workspace.

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


def get_sessions_path() -> Path:
    """Get the sessions storage directory (~/.nanobot/data/sessions)."""
    return ensure_dir(get_operational_data_path() / "sessions")


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


def migrate_runtime_layout() -> list[str]:
    """One-shot migration from the old flat ~/.nanobot layout to the new subdirectory layout.

    Moves files/directories only when the old path exists and the new path does not.
    Returns a list of old paths that were moved (for logging).
    """
    data = get_data_path()
    moved: list[str] = []

    migrations: list[tuple[Path, Path]] = [
        # Long-lived operational data → data/
        (data / "memory", get_operational_data_path() / "memory"),
        (data / "sessions", get_operational_data_path() / "sessions"),
        (data / "inbound", get_operational_data_path() / "inbound"),
        (data / "cron", get_operational_data_path() / "cron"),
        (data / "policy", get_operational_data_path() / "policy"),
        (data / "seen_chats.json", get_operational_data_path() / "seen_chats.json"),
        # Ephemeral state → var/
        (data / "logs", get_logs_path()),
        (data / "run", get_run_path()),
        (data / "media", get_var_path() / "media"),
        # Derived/cache artifacts → var/cache/
        (data / "bridge", get_cache_path() / "bridge"),
        # Secrets → secrets/
        (data / "whatsapp-auth", get_secrets_path() / "whatsapp-auth"),
    ]

    for old_path, new_path in migrations:
        if not old_path.exists():
            continue
        if new_path.exists():
            continue
        new_path.parent.mkdir(parents=True, exist_ok=True)
        old_path.rename(new_path)
        moved.append(str(old_path))

    return moved


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
