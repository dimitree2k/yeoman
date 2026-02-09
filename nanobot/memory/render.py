"""Rendering helpers for memory prompt context."""

from __future__ import annotations

from nanobot.memory.models import MemoryHit


def _compact(text: str, limit: int) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."


def render_memory_hits(hits: list[MemoryHit], max_chars: int = 2400) -> str:
    """Render bounded memory text for prompt injection as system context."""
    if not hits:
        return ""

    lines = [
        "[Retrieved Memory]",
        "Use these as historical context. Prefer recent/high-score items.",
    ]

    for hit in hits:
        entry = hit.entry
        content = _compact(entry.content, 220)
        line = (
            f"- ({entry.kind}/{entry.scope_type} score={hit.final_score:.2f} "
            f"updated={entry.updated_at[:10]}) {content}"
        )
        candidate = "\n".join(lines + [line])
        if len(candidate) > max_chars:
            break
        lines.append(line)

    rendered = "\n".join(lines)
    if len(rendered) <= max_chars:
        return rendered
    return rendered[:max_chars]


def render_legacy_memory_header(text: str, max_chars: int = 800) -> str:
    """Render compact legacy MEMORY.md header text."""
    compact = _compact(text, max_chars)
    if not compact:
        return ""
    return "[Legacy MEMORY.md]\n" + compact
