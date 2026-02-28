"""Typed models for active semantic memory system."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

MemorySector = Literal["episodic", "semantic", "procedural", "emotional", "reflective"]
MemoryScopeType = Literal["chat", "user", "global"]


@dataclass(slots=True)
class MemoryEntry:
    """One stored memory node."""

    id: str
    workspace_id: str
    scope_type: MemoryScopeType
    scope_key: str
    sector: MemorySector
    kind: str
    content: str
    content_norm: str
    content_hash: str
    salience: float
    confidence: float
    source: str
    channel: str | None = None
    chat_id: str | None = None
    sender_id: str | None = None
    source_message_id: str | None = None
    source_role: str | None = None
    language: str | None = None
    meta_json: str = "{}"
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    last_accessed_at: str | None = None
    valid_from: str | None = None
    valid_to: str | None = None
    is_deleted: bool = False


@dataclass(slots=True)
class MemoryHit:
    """One retrieval hit with explainable score components."""

    entry: MemoryEntry
    lexical_score: float = 0.0
    vector_score: float = 0.0
    salience_score: float = 0.0
    recency_score: float = 0.0
    final_score: float = 0.0
    trace: dict[str, Any] = field(default_factory=dict)

    @property
    def node(self) -> MemoryEntry:
        """Compatibility alias used by older code paths."""
        return self.entry


@dataclass(slots=True)
class MemoryCaptureCandidate:
    """Queued capture summary candidate for responder metrics."""

    kind: str
    content: str
    importance: float
    confidence: float
    source_role: Literal["user", "assistant"] = "user"
    source_message_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class MemoryCaptureResult:
    """Capture summary for one processed turn."""

    candidates: list[MemoryCaptureCandidate] = field(default_factory=list)
    saved: list[MemoryEntry] = field(default_factory=list)
    dropped_low_confidence: int = 0
    dropped_low_importance: int = 0
    dropped_safety: int = 0
    deduped: int = 0


# Backward compatibility alias.
MemoryNode = MemoryEntry
