"""Persona file resolution utilities for channel/chat policy."""

from __future__ import annotations

from pathlib import Path

from loguru import logger


def resolve_persona_path(persona_file: str, workspace: Path) -> Path:
    """Resolve a persona path and ensure it stays inside workspace."""
    workspace_resolved = workspace.expanduser().resolve()
    raw = Path(persona_file).expanduser()
    if raw.is_absolute():
        path = raw.resolve()
    else:
        path = (workspace_resolved / raw).resolve()
    try:
        path.relative_to(workspace_resolved)
    except ValueError as e:
        raise ValueError(
            f"Persona file must be inside workspace: {persona_file}"
        ) from e
    return path


def load_persona_text(persona_file: str | None, workspace: Path) -> str | None:
    """Load persona text and optional evolution layer. Missing files are warned and ignored."""
    if not persona_file:
        return None
    path = resolve_persona_path(persona_file, workspace)
    if not path.exists():
        logger.warning(f"persona file not found: {path}")
        return None
    if not path.is_file():
        logger.warning(f"persona path is not a file: {path}")
        return None
    text = path.read_text(encoding="utf-8")

    # Load evolution layer by convention: alpha-2.md → alpha-2.evolution.md
    evolution_path = path.parent / f"{path.stem}.evolution{path.suffix}"
    if evolution_path.is_file():
        evolution_text = evolution_path.read_text(encoding="utf-8")
        text += (
            "\n\n# Evolution Layer (Lived Experience)\n\n"
            "This section reflects accumulated experience from past conversations. "
            "It supplements the base persona above — trait tendencies, domain confidence, "
            "and relationship context that have developed over time. "
            "Base persona invariants always take precedence over evolution drift.\n\n"
            + evolution_text
        )
        logger.debug(f"loaded evolution layer: {evolution_path}")

    return text
