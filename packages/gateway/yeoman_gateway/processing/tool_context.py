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
    canonical_user_id: str = ""
    request_text: str = ""


CURRENT_TOOL_CONTEXT: ContextVar[ToolInvocationContext | None] = ContextVar(
    "yeoman_tool_invocation_context", default=None
)

#: Signal a tool publishes when it handed work off to run in the background. The turn
#: reads it to answer with an acknowledgement instead of an answer.
ASYNC_HANDOFF_SIGNAL = "async_handoff"

#: Per-turn signal bag. A tool publishes a fact about the turn it is running in (for
#: example "an asynchronous task was accepted"), and the turn reads it back after the
#: tool loop. The responder installs a fresh dict per turn, so a signal can never leak
#: into another turn; a tool outside a turn has no reader and publishing is a no-op.
CURRENT_TURN_SIGNALS: ContextVar[dict[str, object] | None] = ContextVar(
    "yeoman_turn_signals", default=None
)


def current_tool_context() -> ToolInvocationContext | None:
    """The context of the tool call currently running, if the caller set one."""
    return CURRENT_TOOL_CONTEXT.get()


def set_tool_context(context: ToolInvocationContext | None):
    """Install a context for this task; the returned token restores the previous one."""
    return CURRENT_TOOL_CONTEXT.set(context)


def reset_tool_context(token) -> None:
    CURRENT_TOOL_CONTEXT.reset(token)


def set_turn_signals(signals: dict[str, object]):
    """Install the signal bag of the current turn; the token restores the previous one."""
    return CURRENT_TURN_SIGNALS.set(signals)


def reset_turn_signals(token) -> None:
    CURRENT_TURN_SIGNALS.reset(token)


def current_turn_signals() -> dict[str, object] | None:
    """The signal bag of the turn currently running, if the caller installed one."""
    return CURRENT_TURN_SIGNALS.get()


def publish_turn_signal(name: str, value: object = True) -> None:
    """Publish one fact about the current turn. Silent when no turn installed a bag."""
    signals = CURRENT_TURN_SIGNALS.get()
    if signals is not None:
        signals[str(name)] = value


def tool_target(*, default_channel: str = "", default_chat_id: str = "") -> tuple[str, str]:
    """Resolve (channel, chat_id) for a tool, preferring the per-turn context.

    Falls back to the shared instance defaults so legacy callers are unaffected.
    """
    context = current_tool_context()
    if context is not None:
        return context.channel, context.chat_id
    return default_channel, default_chat_id


__all__ = [
    "ASYNC_HANDOFF_SIGNAL",
    "CURRENT_TOOL_CONTEXT",
    "CURRENT_TURN_SIGNALS",
    "ToolInvocationContext",
    "current_tool_context",
    "current_turn_signals",
    "publish_turn_signal",
    "reset_tool_context",
    "reset_turn_signals",
    "set_tool_context",
    "set_turn_signals",
    "tool_target",
]
