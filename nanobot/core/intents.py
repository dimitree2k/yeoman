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
class RecordMetricIntent:
    """Emit one structured counter metric."""

    name: str
    value: int = 1
    labels: tuple[tuple[str, str], ...] = ()


type OrchestratorIntent = (
    SetTypingIntent
    | SendOutboundIntent
    | PersistSessionIntent
    | RecordMetricIntent
)
type IntentKind = Literal["typing", "send_outbound", "persist_session", "record_metric"]
