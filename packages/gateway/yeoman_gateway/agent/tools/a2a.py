"""Explicit Yeoman tool for delegating a task to a named A2A worker."""

from __future__ import annotations

from typing import Any

from loguru import logger

from yeoman_gateway.a2a.registry import A2AWorkerRegistry
from yeoman_gateway.agent.tools.base import Tool
from yeoman_gateway.observability import private_log_identifier, safe_log_token


class A2ADelegateTool(Tool):
    """Call one configured A2A worker and return its result to Yeoman."""

    def __init__(self, registry: A2AWorkerRegistry) -> None:
        self._registry = registry
        self._channel = ""
        self._chat_id = ""

    def set_context(self, channel: str, chat_id: str) -> None:
        """Attach the current Yeoman chat to observability records."""
        self._channel = str(channel or "")
        self._chat_id = str(chat_id or "")

    @property
    def name(self) -> str:
        return "a2a_delegate"

    @property
    def description(self) -> str:
        workers = ", ".join(self._registry.names) or "configured workers"
        return (
            "Delegate a bounded task to a configured A2A worker and use the returned result. "
            "This never sends directly to a user or chat. Choose one of: "
            f"{workers}."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "worker": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Registered A2A worker name.",
                },
                "message": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 12000,
                    "description": "The self-contained task to send to the worker.",
                },
                "context_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 200,
                    "description": "Optional A2A context ID for a follow-up task.",
                },
            },
            "required": ["worker", "message"],
            "additionalProperties": False,
        }

    async def execute(self, **kwargs: Any) -> str:
        worker = str(kwargs.get("worker") or "")
        message = str(kwargs.get("message") or "")
        context_id = kwargs.get("context_id")
        logger.info(
            "A2A delegation started channel={} chat={} worker={} context_id={} message_chars={}",
            safe_log_token(self._channel, max_length=40),
            private_log_identifier(self._chat_id),
            safe_log_token(worker),
            safe_log_token(context_id),
            len(message),
        )
        try:
            result = await self._registry.call(worker, message, context_id=context_id)
        except Exception as exc:
            logger.warning(
                "A2A delegation failed channel={} chat={} worker={} error_type={}",
                safe_log_token(self._channel, max_length=40),
                private_log_identifier(self._chat_id),
                safe_log_token(worker),
                safe_log_token(type(exc).__name__, max_length=80),
            )
            raise
        logger.info(
            "A2A delegation completed channel={} chat={} worker={} task_id={} context_id={} state={} result_chars={}",
            safe_log_token(self._channel, max_length=40),
            private_log_identifier(self._chat_id),
            safe_log_token(result.worker),
            safe_log_token(result.task_id),
            safe_log_token(result.context_id),
            safe_log_token(result.state, max_length=80),
            len(result.text or ""),
        )
        text = result.text or "(worker returned no textual output)"
        return f"[{result.worker} | {result.state} | {result.task_id or 'no-task-id'}]\n{text}"
