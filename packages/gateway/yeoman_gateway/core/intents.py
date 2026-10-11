"""Intent types emitted by the vNext orchestrator."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from yeoman_shared.reactions import MODEL_ORIGIN, ReactionOrigin

from yeoman_gateway.core.models import OutboundEvent


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


@dataclass(frozen=True, slots=True, kw_only=True)
class SendReactionIntent:
    """Deliver one reaction emoji to a specific message."""

    channel: str
    chat_id: str
    message_id: str
    emoji: str
    participant_jid: str | None = None
    #: "model" for an emoji the language model chose - it must be one the owner approved -
    #: or "system" for a confirmation the gateway decides itself.
    origin: ReactionOrigin = MODEL_ORIGIN
    #: Why this reaction exists. Stored on the effect for diagnosis; never sent to chat.
    reason: str = ""


type OrchestratorIntent = (
    SetTypingIntent
    | SendOutboundIntent
    | SendReactionIntent
    | PersistSessionIntent
    | RecordMetricIntent
)
type IntentKind = Literal[
    "typing",
    "send_outbound",
    "send_reaction",
    "persist_session",
    "record_metric",
]
