"""Persona file resolution utilities for channel/chat policy."""

from __future__ import annotations

from pathlib import Path

from loguru import logger


def resolve_persona_path(persona_file: str, workspace: Path) -> Path:
    """Resolve a persona path and ensure it stays inside workspace."""
    workspace_resolved = workspace.expanduser().resolve()
    raw = Path(persona_file).expanduser()
    path = (workspace_resolved / raw).resolve() if not raw.is_absolute() else raw.resolve()
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

