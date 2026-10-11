"""Result value passed from Knowledge retrieval into Participation selection."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(slots=True)
class FactRetrievalResult:
    """Filtered retrieval text and the source revisions it rests on."""

    text: str = ""
    hits: tuple[Any, ...] = ()
    used_source_refs: Mapping[str, tuple[tuple[str, int], ...]] = field(default_factory=dict)
    denied_count: int = 0
