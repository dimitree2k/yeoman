"""Structured A2A delegation tool."""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from loguru import logger

from yeoman_gateway.a2a.registry import A2AWorkerRegistry
from yeoman_gateway.agent.tools.base import Tool
from yeoman_gateway.observability import private_log_identifier, safe_log_token
from yeoman_gateway.processing.models import (
    EffectConflictError,
    EffectTarget,
    ExternalActionPayload,
)
from yeoman_gateway.processing.tool_context import current_tool_context

DELEGATION_WINDOW_MS = 600_000
DELEGATION_WORKER_ID = "a2a_delegate"
DELEGATION_LEASE_MS = 900_000


class A2ADelegateTool(Tool):
    """Invoke an advertised Hermes profile skill; never sends a text conversation."""

    def __init__(self, registry: A2AWorkerRegistry, *, store: Any | None = None, delivery: Any | None = None) -> None:
        self._registry = registry
        self._store = store
        del delivery  # A2A v1 profile has no push/detached delivery contract.
        self._channel = ""
        self._chat_id = ""
        self._session_key = ""

    def set_context(self, channel: str, chat_id: str, *, session_key: str = "") -> None:
        self._channel, self._chat_id, self._session_key = str(channel or ""), str(chat_id or ""), str(session_key or "")

    @property
    def name(self) -> str:
        return "a2a_delegate"

    @property
    def description(self) -> str:
        return f"Invoke one structured skill on: {', '.join(self._registry.names) or 'configured workers'}."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "worker": {"type": "string", "minLength": 1, "description": "Registered A2A worker name."},
                "skill": {"type": "string", "minLength": 1, "description": "Advertised Hermes profile skill."},
                "input": {"type": "object", "description": "Skill-specific structured input."},
            },
            "required": ["worker", "skill", "input"],
            "additionalProperties": False,
        }

    def _context_id(self) -> str | None:
        return None if self._channel or self._chat_id or self._session_key else None

    def _claim(self, worker: str, skill: str, input: dict[str, Any]) -> tuple[bool, str, str]:
        if self._store is None:
            return True, "no-outbox", ""
        canonical = json.dumps({"skill": skill, "input": input}, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        context = current_tool_context()
        channel = context.channel if context else self._channel
        chat_id = context.chat_id if context else self._chat_id
        turn = context.reply_to_message_id if context and context.reply_to_message_id else f"window:{int(time.time() * 1000) // DELEGATION_WINDOW_MS}"
        operation_key = f"a2a:{worker}:{channel}:{chat_id}:{turn}"
        effect_id = "a2a-" + hashlib.sha256(operation_key.encode()).hexdigest()[:32]
        try:
            stored = self._store.enqueue_effect(
                effect_id=effect_id,
                operation_key=operation_key,
                payload=ExternalActionPayload(action="a2a_delegate", arguments={"worker": worker, "skill": skill, "input_sha256": digest}),
                now_ms=int(time.time() * 1000),
                trace_id=operation_key,
                capability="a2a_delegate",
                target=EffectTarget(channel="a2a", chat_id=worker),
                state="queued",
            )
            if str(stored) != effect_id or not self._store.claim_effect(effect_id, DELEGATION_WORKER_ID, int(time.time() * 1000), DELEGATION_LEASE_MS):
                return False, "duplicate", effect_id
        except EffectConflictError:
            return False, "conflict", effect_id
        except Exception as exc:
            logger.warning("A2A delegation could not be claimed worker={} error_type={}", safe_log_token(worker), type(exc).__name__)
            return False, "claim-failed", ""
        return True, "claimed", effect_id

    def _settle(self, effect_id: str, state: str) -> None:
        if self._store is None or not effect_id:
            return
        try:
            self._store.transition(effect_id, expected="executing", target=state, now_ms=int(time.time() * 1000), worker_id=DELEGATION_WORKER_ID)
        except Exception as exc:
            logger.warning("A2A delegation outcome not recorded state={} error_type={}", state, type(exc).__name__)

    async def execute(self, **kwargs: Any) -> str:
        worker, skill, input = str(kwargs.get("worker") or ""), str(kwargs.get("skill") or ""), kwargs.get("input")
        if not worker or not skill or not isinstance(input, dict):
            raise ValueError("worker, skill, and structured input are required")
        allowed, note, effect_id = self._claim(worker, skill, input)
        if not allowed:
            return f"[{worker} | not-sent | {note}]"
        logger.info("A2A delegation started channel={} chat={} worker={} skill={}", safe_log_token(self._channel, max_length=40), private_log_identifier(self._chat_id), safe_log_token(worker), safe_log_token(skill))
        try:
            result = await self._registry.invoke_skill(worker, skill, input, context_id=self._context_id())
        except Exception as exc:
            self._settle(effect_id, "unknown")
            logger.warning("A2A delegation failed channel={} chat={} worker={} skill={} error_type={}", safe_log_token(self._channel, max_length=40), private_log_identifier(self._chat_id), safe_log_token(worker), safe_log_token(skill), type(exc).__name__)
            raise
        self._settle(effect_id, "sent")
        logger.info("A2A delegation completed channel={} chat={} worker={} skill={} task_id={} context_id={} state={}", safe_log_token(self._channel, max_length=40), private_log_identifier(self._chat_id), safe_log_token(result.worker), safe_log_token(result.skill), safe_log_token(result.task_id), safe_log_token(result.context_id), safe_log_token(result.state, max_length=80))
        output = json.dumps(result.output, ensure_ascii=False, sort_keys=True) if result.output is not None else ""
        return f"[{result.worker} | {result.skill} | {result.state} | {result.task_id}]\n{output}".rstrip()
