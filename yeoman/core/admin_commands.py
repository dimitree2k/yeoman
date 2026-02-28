"""Deterministic chat admin command routing primitives."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass(frozen=True, slots=True)
class AdminMetricEvent:
    """Metric increment requested by deterministic admin execution."""

    name: str
    labels: tuple[tuple[str, str], ...] = ()
    value: int = 1


@dataclass(frozen=True, slots=True)
class AdminCommandContext:
    """Normalized context for deterministic admin command handling."""

    channel: str
    chat_id: str
    sender_id: str
    participant: str | None
    is_group: bool
    raw_text: str


@dataclass(frozen=True, slots=True)
class AdminCommandResult:
    """Result emitted by a deterministic admin command handler."""

    status: Literal["handled", "unknown", "ignored"]
    response: str | None = None
    command_name: str = ""
    outcome: str = ""
    source: str = ""
    dry_run: bool = False
    metric_events: tuple[AdminMetricEvent, ...] = ()

    @property
    def intercepts_normal_flow(self) -> bool:
        return self.status in {"handled", "unknown"}


class AdminCommandHandler(Protocol):
    """Namespace command handler used by :class:`AdminCommandRouter`."""

    def namespace(self) -> str:
        """Namespace name, e.g. ``policy`` for ``/policy ...``."""

    def is_applicable(self, ctx: AdminCommandContext) -> bool:
        """Whether this handler is applicable for the provided context."""

    def handle(self, ctx: AdminCommandContext, argv: list[str]) -> AdminCommandResult:
        """Handle one command namespace with argv after namespace token."""

    def help_hint(self) -> str:
        """Short help hint, e.g. ``/policy help``."""


class AdminCommandRouter:
    """Slash-first command router for deterministic non-LLM chat commands."""

    def __init__(self, handlers: list[AdminCommandHandler]):
        self._handlers = {handler.namespace().strip().lower(): handler for handler in handlers if handler.namespace().strip()}

    def route(self, ctx: AdminCommandContext) -> AdminCommandResult | None:
        compact = ctx.raw_text.strip()
        if not compact.startswith("/"):
            return None

        body = compact[1:].strip()
        if not body:
            return AdminCommandResult(status="ignored")

        try:
            tokens = shlex.split(body)
        except ValueError as e:
            if self._should_report_unknown(ctx):
                return AdminCommandResult(status="unknown", response=f"Invalid command syntax: {e}")
            return AdminCommandResult(status="ignored")

        if not tokens:
            return AdminCommandResult(status="ignored")

        namespace = tokens[0].strip().lower()
        if not namespace:
            return AdminCommandResult(status="ignored")

        handler = self._handlers.get(namespace)
        if handler is None:
            if self._should_report_unknown(ctx):
                return AdminCommandResult(
                    status="unknown",
                    response=self._unknown_command_message(namespace, ctx),
                )
            return AdminCommandResult(status="ignored")

        if not handler.is_applicable(ctx):
            return AdminCommandResult(status="ignored")
        return handler.handle(ctx, tokens[1:])

    def _should_report_unknown(self, ctx: AdminCommandContext) -> bool:
        return any(handler.is_applicable(ctx) for handler in self._handlers.values())

    def _unknown_command_message(self, namespace: str, ctx: AdminCommandContext) -> str:
        hints = [handler.help_hint() for handler in self._handlers.values() if handler.is_applicable(ctx)]
        if hints:
            hint = hints[0]
            return f"Unknown command '/{namespace}'. Try {hint}."
        return f"Unknown command '/{namespace}'."
