"""Persona file resolution utilities for channel/chat policy."""

from __future__ import annotations

from pathlib import Path

from loguru import logger


def _legacy_persona_relative(raw: Path) -> Path | None:
    """Map legacy memory/personas/* paths to personas/*."""
    parts = raw.parts
    if len(parts) >= 2 and parts[0] == "memory" and parts[1] == "personas":
        return Path("personas", *parts[2:])
    return None


def resolve_persona_path(persona_file: str, workspace: Path) -> Path:
    """Resolve a persona path and ensure it stays inside workspace."""
    workspace_resolved = workspace.expanduser().resolve()
    raw = Path(persona_file).expanduser()
    if raw.is_absolute():
        path = raw.resolve()
    else:
        primary = (workspace_resolved / raw).resolve()
        legacy = _legacy_persona_relative(raw)
        # Backward compatibility: if old memory/personas path is configured
        # but files moved to workspace/personas, use the new location.
        if legacy is not None and not primary.exists():
            fallback = (workspace_resolved / legacy).resolve()
            path = fallback if fallback.exists() else primary
        else:
            path = primary
    try:
        path.relative_to(workspace_resolved)
    except ValueError as e:
        raise ValueError(
            f"Persona file must be inside workspace: {persona_file}"
        ) from e
    return path


def load_persona_text(persona_file: str | None, workspace: Path) -> str | None:
    """Load persona text. Missing files are warned and ignored."""
    if not persona_file:
        return None
    path = resolve_persona_path(persona_file, workspace)
    if not path.exists():
        logger.warning(f"persona file not found: {path}")
        return None
    if not path.is_file():
        logger.warning(f"persona path is not a file: {path}")
        return None
    return path.read_text(encoding="utf-8")
