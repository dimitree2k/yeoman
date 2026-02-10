"""Port interfaces for the vNext orchestration core."""

from __future__ import annotations

from typing import Protocol

from nanobot.core.models import ArchivedMessage, InboundEvent, PolicyDecision


class ReplyArchivePort(Protocol):
    """Storage port for inbound archive lookups."""

    def record_inbound(self, event: InboundEvent) -> None:
        """Persist one inbound event."""

    def lookup_message(self, channel: str, chat_id: str, message_id: str) -> ArchivedMessage | None:
        """Look up archived message in a specific chat."""

    def lookup_message_any_chat(
        self,
        channel: str,
        message_id: str,
        *,
        preferred_chat_id: str | None = None,
    ) -> ArchivedMessage | None:
        """Look up archived message across channel chats."""

    def lookup_messages_before(
        self,
        channel: str,
        chat_id: str,
        anchor_message_id: str,
        *,
        limit: int,
    ) -> list[ArchivedMessage]:
        """Look up archived messages before one anchor message in a chat."""


class PolicyPort(Protocol):
    """Policy evaluation port."""

    def evaluate(self, event: InboundEvent) -> PolicyDecision:
        """Evaluate one inbound event and return a typed decision."""


class ResponderPort(Protocol):
    """LLM/tool execution port."""

    async def generate_reply(self, event: InboundEvent, decision: PolicyDecision) -> str | None:
        """Return assistant text for one event, or None for no response."""


class TelemetryPort(Protocol):
    """Counter and event telemetry sink."""

    def incr(self, name: str, value: int = 1, labels: tuple[tuple[str, str], ...] = ()) -> None:
        """Increase named counter with optional labels."""


class RuntimeSupervisorPort(Protocol):
    """Out-of-process runtime readiness and lifecycle port."""

    def ensure_ready(self, *, auto_repair: bool, start_if_needed: bool) -> object:
        """Ensure runtime process is healthy and ready."""
