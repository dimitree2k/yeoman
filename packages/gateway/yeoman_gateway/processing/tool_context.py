"""Per-turn tool invocation context.

Tool instances are shared across turns, so the responder used to switch their context
globally (`set_context(...)`) before each turn. That is safe while exactly one generation
runs, but it silently leaks one turn's target into another as soon as two generations
overlap.

This module carries the same information in a :class:`~contextvars.ContextVar`, which is
per task: a tool that finds a context here uses it and ignores the shared instance state.
Legacy callers that never set a context keep their previous behaviour.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ToolInvocationContext:
    """Immutable context of one turn's tool invocation."""

    channel: str
    chat_id: str
    session_key: str = ""
    is_owner: bool = False
    reply_to_message_id: str | None = None
    thread_id: str | None = None
    turn_id: str | None = None
    turn_revision: int | None = None


CURRENT_TOOL_CONTEXT: ContextVar[ToolInvocationContext | None] = ContextVar(
    "yeoman_tool_invocation_context", default=None
)


def current_tool_context() -> ToolInvocationContext | None:
    """The context of the tool call currently running, if the caller set one."""
    return CURRENT_TOOL_CONTEXT.get()


def set_tool_context(context: ToolInvocationContext | None):
    """Install a context for this task; the returned token restores the previous one."""
    return CURRENT_TOOL_CONTEXT.set(context)


def reset_tool_context(token) -> None:
    CURRENT_TOOL_CONTEXT.reset(token)


def tool_target(*, default_channel: str = "", default_chat_id: str = "") -> tuple[str, str]:
    """Resolve (channel, chat_id) for a tool, preferring the per-turn context.

    Falls back to the shared instance defaults so legacy callers are unaffected.
    """
    context = current_tool_context()
    if context is not None:
        return context.channel, context.chat_id
    return default_channel, default_chat_id


__all__ = [
    "CURRENT_TOOL_CONTEXT",
    "ToolInvocationContext",
    "current_tool_context",
    "reset_tool_context",
    "set_tool_context",
    "tool_target",
]
