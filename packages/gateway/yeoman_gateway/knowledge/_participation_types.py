"""Read contracts used by Participation Knowledge context."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class FactReadContext:
    """Read-side facts about the reader. Built from runtime state, never from a model."""

    principal_id: str
    chat_scope_key: str
    current_members: frozenset[str] | None
    audience_snapshot_id: str | None = None
    epoch: int = 0
    now_ms: int = 0
    owner: bool = False
    group_wide: bool = False

    @property
    def membership_known(self) -> bool:
        """True only when a proven membership list exists for the chat right now."""
        return self.current_members is not None


@dataclass(slots=True)
class FactRetrievalResult:
    """What a read produced: filtered text plus the source revisions it rests on."""

    text: str = ""
    hits: tuple[Any, ...] = ()
    used_source_refs: Mapping[str, tuple[tuple[str, int], ...]] = field(default_factory=dict)
    denied_count: int = 0

