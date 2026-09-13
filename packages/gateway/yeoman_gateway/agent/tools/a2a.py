"""Structured A2A delegation tool."""

from __future__ import annotations

import asyncio
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
        self._delivery = delivery
        self._background: set[asyncio.Task[None]] = set()
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
                "skill": {"type": "string", "enum": ["search.web", "research.deep"], "description": "Supported delegated skill."},
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
        if skill not in {"search.web", "research.deep"}:
            raise ValueError("a2a_delegate supports only search.web and research.deep")
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
        if skill == "research.deep" and result.state in {"TASK_STATE_SUBMITTED", "TASK_STATE_WORKING"}:
            turn = current_tool_context()
            channel = turn.channel if turn is not None else self._channel
            chat_id = turn.chat_id if turn is not None else self._chat_id
            task = asyncio.create_task(self._poll_research(worker, result, effect_id, channel, chat_id))
            self._background.add(task)
            task.add_done_callback(self._background.discard)
        logger.info("A2A delegation completed channel={} chat={} worker={} skill={} task_id={} context_id={} state={}", safe_log_token(self._channel, max_length=40), private_log_identifier(self._chat_id), safe_log_token(result.worker), safe_log_token(result.skill), safe_log_token(result.task_id), safe_log_token(result.context_id), safe_log_token(result.state, max_length=80))
        output = json.dumps(result.output, ensure_ascii=False, sort_keys=True) if result.output is not None else (f"error={result.error_code} retryable={result.retryable}" if result.error_code else "")
        return f"[{result.worker} | {result.skill} | {result.state} | {result.task_id}]\n{output}".rstrip()

    async def _poll_research(self, worker: str, result: Any, effect_id: str, channel: str, chat_id: str) -> None:
        try:
            final = await self._registry.poll_task(worker, result.task_id, skill=result.skill, context_id=result.context_id, reference_task_ids=result.reference_task_ids)
        except Exception as exc:
            logger.warning("A2A research polling failed worker={} error_type={}", safe_log_token(worker), type(exc).__name__)
            final_content = "error=POLL_TIMEOUT retryable=True"
        else:
            final_content = json.dumps(final.output, ensure_ascii=False, sort_keys=True) if final.output is not None else f"error={final.error_code} retryable={final.retryable}"
        if self._delivery is not None and channel and chat_id:
            try:
                await self._delivery.send(source="a2a", operation_ref=f"a2a-result:{effect_id}", channel=channel, chat_id=chat_id, content=final_content)
            except Exception as exc:
                logger.warning("A2A research result delivery failed worker={} error_type={}", safe_log_token(worker), type(exc).__name__)
