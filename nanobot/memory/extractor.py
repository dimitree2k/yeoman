"""Heuristic extraction pipeline for memory capture."""

from __future__ import annotations

import re

from nanobot.memory.models import MemoryCaptureCandidate

_PREFERENCE_PATTERNS = [
    re.compile(r"\b(i\s+prefer|my\s+preference\s+is|i\s+like|i\s+usually\s+use)\b", re.IGNORECASE),
    re.compile(r"\b(don't\s+use|do\s+not\s+use|always\s+use)\b", re.IGNORECASE),
]

_FACT_PATTERNS = [
    re.compile(r"\b(my\s+timezone\s+is|i\s+am\s+in|my\s+name\s+is)\b", re.IGNORECASE),
    re.compile(r"\b(i\s+work\s+with|my\s+language\s+is|i\s+speak)\b", re.IGNORECASE),
]

_DECISION_PATTERNS = [
    re.compile(r"\b(let's\s+use|we\s+decided|from\s+now\s+on\s+use|we\s+will\s+use)\b", re.IGNORECASE),
    re.compile(r"\b(do\s+.+\s+not\s+.+)\b", re.IGNORECASE),
]

_UNSAFE_SUBSTRINGS = {
    "ignore previous",
    "system prompt",
    "developer message",
    "tool call",
    "function_call",
    "jailbreak",
    "sudo rm -rf",
}


def _normalize_text(text: str) -> str:
    return " ".join(text.split()).strip()


def _is_command_only(text: str) -> bool:
    compact = text.strip()
    if not compact:
        return True
    if compact.startswith("$"):
        return True
    if compact.startswith(("bash ", "sh ", "zsh ", "python ", "node ")):
        return True
    return False


def _is_unsafe(text: str) -> bool:
    lowered = text.lower()
    if "```" in lowered:
        return True
    for token in _UNSAFE_SUBSTRINGS:
        if token in lowered:
            return True
    return False


def _collect_candidates(
    text: str,
    *,
    source_message_id: str | None,
    source_role: str,
) -> list[MemoryCaptureCandidate]:
    compact = _normalize_text(text)
    if len(compact) < 8 or len(compact) > 300:
        return []
    if _is_command_only(compact) or _is_unsafe(compact):
        return []

    found: list[MemoryCaptureCandidate] = []

    if any(p.search(compact) for p in _PREFERENCE_PATTERNS):
        found.append(
            MemoryCaptureCandidate(
                kind="preference",
                content=compact,
                importance=0.85,
                confidence=0.92,
                source_role=source_role,
                source_message_id=source_message_id,
            )
        )

    if any(p.search(compact) for p in _FACT_PATTERNS):
        found.append(
            MemoryCaptureCandidate(
                kind="fact",
                content=compact,
                importance=0.80,
                confidence=0.88,
                source_role=source_role,
                source_message_id=source_message_id,
            )
        )

    if any(p.search(compact) for p in _DECISION_PATTERNS):
        found.append(
            MemoryCaptureCandidate(
                kind="decision",
                content=compact,
                importance=0.90,
                confidence=0.90,
                source_role=source_role,
                source_message_id=source_message_id,
            )
        )

    return found


def extract_candidates(
    text: str,
    *,
    source_message_id: str | None,
    source_role: str = "user",
    include_episodic: bool = True,
) -> tuple[list[MemoryCaptureCandidate], int]:
    """Extract memory candidates and report safety-drop count."""
    compact = _normalize_text(text)
    if not compact:
        return [], 0

    dropped_safety = 1 if _is_unsafe(compact) else 0
    candidates = _collect_candidates(
        compact,
        source_message_id=source_message_id,
        source_role="assistant" if source_role == "assistant" else "user",
    )

    if include_episodic and len(compact) >= 8 and not _is_command_only(compact) and not _is_unsafe(compact):
        preview = compact[:180]
        if len(compact) > 180:
            preview += "..."
        candidates.append(
            MemoryCaptureCandidate(
                kind="episodic",
                content=f"User message: {preview}",
                importance=0.60,
                confidence=0.75,
                source_role="assistant" if source_role == "assistant" else "user",
                source_message_id=source_message_id,
            )
        )

    dedup: dict[tuple[str, str], MemoryCaptureCandidate] = {}
    for candidate in candidates:
        key = (candidate.kind, _normalize_text(candidate.content).lower())
        if key in dedup:
            continue
        dedup[key] = candidate

    return list(dedup.values()), dropped_safety
