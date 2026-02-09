"""Typed models for long-term memory."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

MemoryKind = Literal["preference", "decision", "fact", "episodic"]
MemoryScopeType = Literal["chat", "user", "global"]
MemorySourceType = Literal["auto_heuristic", "manual", "import"]


@dataclass(slots=True)
class MemoryEntry:
    """One stored memory entry."""

    id: str
    workspace_id: str
    scope_type: MemoryScopeType
    scope_key: str
    kind: MemoryKind
    content: str
    content_norm: str
    content_hash: str
    importance: float
    confidence: float
    source: MemorySourceType
    channel: str | None = None
    chat_id: str | None = None
    sender_id: str | None = None
    source_message_id: str | None = None
    source_role: str | None = None
    meta_json: str = "{}"
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    last_accessed_at: str | None = None
    expires_at: str | None = None
    is_deleted: bool = False


@dataclass(slots=True)
class MemoryHit:
    """One scored retrieval hit."""

    entry: MemoryEntry
    fts_score: float
    fts_score_norm: float = 0.0
    recency_score: float = 0.0
    final_score: float = 0.0


@dataclass(slots=True)
class MemoryCaptureCandidate:
    """Candidate extracted from conversation for memory persistence."""

    kind: MemoryKind
    content: str
    importance: float
    confidence: float
    source_role: Literal["user", "assistant"] = "user"
    source_message_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class MemoryCaptureResult:
    """Capture summary for one turn."""

    candidates: list[MemoryCaptureCandidate] = field(default_factory=list)
    saved: list[MemoryEntry] = field(default_factory=list)
    dropped_low_confidence: int = 0
    dropped_low_importance: int = 0
    dropped_safety: int = 0
    deduped: int = 0
