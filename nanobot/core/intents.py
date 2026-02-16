"""Intent types emitted by the vNext orchestrator."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from nanobot.core.models import OutboundEvent


@dataclass(frozen=True, slots=True, kw_only=True)
class SetTypingIntent:
    """Toggle typing indicator for a channel chat."""

    channel: str
    chat_id: str
    enabled: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class SendOutboundIntent:
    """Deliver one outbound message."""

    event: OutboundEvent


@dataclass(frozen=True, slots=True, kw_only=True)
class PersistSessionIntent:
    """Persist user/assistant turn for a session."""

    session_key: str
    user_content: str
    assistant_content: str


@dataclass(frozen=True, slots=True, kw_only=True)
class QueueMemoryNotesCaptureIntent:
    """Queue one inbound event for background memory-notes capture."""

    channel: str
    chat_id: str
    sender_id: str
    message_id: str | None
    content: str
    is_group: bool
    mode: Literal["adaptive", "heuristic", "hybrid"]
    batch_interval_seconds: int
    batch_max_messages: int


@dataclass(frozen=True, slots=True, kw_only=True)
class RecordManualMemoryIntent:
    """Persist one explicit memory capture entry immediately."""

    channel: str
    chat_id: str
    sender_id: str | None
    content: str
    entry_kind: Literal["idea", "backlog"]


@dataclass(frozen=True, slots=True, kw_only=True)
class RecordMetricIntent:
    """Emit one structured counter metric."""

    name: str
    value: int = 1
    labels: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class SendReactionIntent:
    """Deliver one reaction emoji to a specific message."""

    channel: str
    chat_id: str
    message_id: str
    emoji: str


type OrchestratorIntent = (
    SetTypingIntent
    | SendOutboundIntent
    | SendReactionIntent
    | PersistSessionIntent
    | QueueMemoryNotesCaptureIntent
    | RecordManualMemoryIntent
    | RecordMetricIntent
)
type IntentKind = Literal[
    "typing",
    "send_outbound",
    "send_reaction",
    "persist_session",
    "queue_memory_notes_capture",
    "record_manual_memory",
    "record_metric",
]
