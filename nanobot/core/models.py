"""Domain models for the vNext typed orchestration core."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

type ChannelName = Literal[
    "whatsapp",
    "telegram",
    "discord",
    "feishu",
    "system",
    "cli",
    "cron",
    "heartbeat",
]
type MessageId = str
type ChatId = str
type SenderId = str
type SecurityStage = Literal["input", "tool", "output"]
type SecurityAction = Literal["allow", "warn", "block", "sanitize"]
type SecuritySeverity = Literal["safe", "low", "medium", "high", "critical"]


@dataclass(frozen=True, slots=True, kw_only=True)
class ArchivedMessage:
    """Archived inbound row used for reply-context lookups."""

    channel: str
    chat_id: str
    message_id: str
    participant: str | None
    sender_id: str | None
    text: str
    timestamp: int | None
    created_at: str


@dataclass(frozen=True, slots=True, kw_only=True)
class PolicyDecision:
    """Typed policy output consumed by the orchestrator."""

    accept_message: bool
    should_respond: bool
    allowed_tools: frozenset[str]
    reason: str
    persona_text: str | None = None
    persona_file: str | None = None
    notes_enabled: bool = False
    notes_mode: Literal["adaptive", "heuristic", "hybrid"] = "adaptive"
    notes_allow_blocked_senders: bool = False
    notes_batch_interval_seconds: int = 1800
    notes_batch_max_messages: int = 100
    source: str = "disabled"


@dataclass(frozen=True, slots=True, kw_only=True)
class SecurityDecision:
    """Decision emitted by the security middleware for one stage."""

    action: SecurityAction
    reason: str
    severity: SecuritySeverity = "safe"
    tags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class SecurityResult:
    """Security middleware result for one stage check."""

    stage: SecurityStage
    decision: SecurityDecision
    sanitized_text: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class InboundEvent:
    """Normalized inbound event consumed by the orchestrator."""

    channel: str
    chat_id: ChatId
    sender_id: SenderId
    content: str
    message_id: MessageId | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    participant: str | None = None
    is_group: bool = False
    mentioned_bot: bool = False
    reply_to_bot: bool = False
    reply_to_message_id: MessageId | None = None
    reply_to_participant: str | None = None
    reply_to_text: str | None = None
    media: tuple[str, ...] = ()
    raw_metadata: dict[str, object] = field(default_factory=dict)

    def normalized_content(self) -> str:
        """Normalized text used for dedupe and downstream processing."""
        return self.content.strip()


@dataclass(frozen=True, slots=True, kw_only=True)
class OutboundEvent:
    """Typed outbound event emitted by the orchestrator."""

    channel: str
    chat_id: ChatId
    content: str
    reply_to: str | None = None
    media: tuple[str, ...] = ()
