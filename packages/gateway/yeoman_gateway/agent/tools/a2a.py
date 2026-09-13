"""Structured A2A delegation tool."""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import time
from typing import Any

from loguru import logger

from yeoman_gateway.a2a.client import A2APollTimeoutError, A2AProtocolError, A2ATransportError
from yeoman_gateway.a2a.registry import A2AWorkerRegistry
from yeoman_gateway.agent.tools.base import Tool
from yeoman_gateway.observability import private_log_identifier, safe_log_token
from yeoman_gateway.processing.models import (
    EffectConflictError,
    EffectTarget,
    ExternalActionPayload,
)
from yeoman_gateway.processing.tool_context import current_tool_context

from .a2a_research import A2AResearchStore, PendingResearch, sibling_path

DELEGATION_WINDOW_MS = 600_000
DELEGATION_WORKER_ID = "a2a_delegate"
DELEGATION_LEASE_MS = 900_000

#: One ``poll_task`` call already waits the client's maximum window (1800s). Deep
#: research can legitimately outlive a single window, so a poll timeout extends the
#: wait by another round instead of reporting POLL_TIMEOUT for a task that is still
#: making progress. The budget resets on restart, where durable pending entries are
#: resumed by ``resume_pending_research``.
RESEARCH_POLL_EXTENSIONS = 3
POLL_TIMEOUT_CONTENT = "error=POLL_TIMEOUT retryable=True"


class A2ADelegateTool(Tool):
    """Invoke an advertised Hermes profile skill; never sends a text conversation."""

    def __init__(
        self,
        registry: A2AWorkerRegistry,
        *,
        store: Any | None = None,
        delivery: Any | None = None,
        pending_store: A2AResearchStore | None = None,
        research_poll_extensions: int = RESEARCH_POLL_EXTENSIONS,
    ) -> None:
        self._registry = registry
        self._store = store
        self._delivery = delivery
        self._research_poll_extensions = max(0, int(research_poll_extensions))
        self._research_store: A2AResearchStore | None
        if pending_store is not None:
            self._research_store = pending_store
        else:
            path = sibling_path(store)
            self._research_store = A2AResearchStore(path) if path else None
        self._background: set[asyncio.Task[None]] = set()
        self._background_task_ids: set[str] = set()
        self._channel = ""
        self._chat_id = ""
        self._session_key = ""
        self.resume_pending_research()

    def set_context(self, channel: str, chat_id: str, *, session_key: str = "") -> None:
        self._channel, self._chat_id, self._session_key = (
            str(channel or ""),
            str(chat_id or ""),
            str(session_key or ""),
        )

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
                "worker": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Registered A2A worker name.",
                },
                "skill": {
                    "type": "string",
                    "enum": ["search.web", "research.deep"],
                    "description": "Supported delegated skill.",
                },
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
        canonical = json.dumps(
            {"skill": skill, "input": input}, sort_keys=True, separators=(",", ":")
        )
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        context = current_tool_context()
        channel = context.channel if context else self._channel
        chat_id = context.chat_id if context else self._chat_id
        turn = (
            context.reply_to_message_id
            if context and context.reply_to_message_id
            else f"window:{int(time.time() * 1000) // DELEGATION_WINDOW_MS}"
        )
        operation_key = f"a2a:{worker}:{channel}:{chat_id}:{turn}"
        effect_id = "a2a-" + hashlib.sha256(operation_key.encode()).hexdigest()[:32]
        try:
            stored = self._store.enqueue_effect(
                effect_id=effect_id,
                operation_key=operation_key,
                payload=ExternalActionPayload(
                    action="a2a_delegate",
                    arguments={"worker": worker, "skill": skill, "input_sha256": digest},
                ),
                now_ms=int(time.time() * 1000),
                trace_id=operation_key,
                capability="a2a_delegate",
                target=EffectTarget(channel="a2a", chat_id=worker),
                state="queued",
            )
            if str(stored) != effect_id or not self._store.claim_effect(
                effect_id, DELEGATION_WORKER_ID, int(time.time() * 1000), DELEGATION_LEASE_MS
            ):
                return False, "duplicate", effect_id
        except EffectConflictError:
            return False, "conflict", effect_id
        except Exception as exc:
            logger.warning(
                "A2A delegation could not be claimed worker={} error_type={}",
                safe_log_token(worker),
                type(exc).__name__,
            )
            return False, "claim-failed", ""
        return True, "claimed", effect_id

    def _settle(self, effect_id: str, state: str) -> None:
        if self._store is None or not effect_id:
            return
        try:
            self._store.transition(
                effect_id,
                expected="executing",
                target=state,
                now_ms=int(time.time() * 1000),
                worker_id=DELEGATION_WORKER_ID,
            )
        except Exception as exc:
            logger.warning(
                "A2A delegation outcome not recorded state={} error_type={}",
                state,
                type(exc).__name__,
            )

    @staticmethod
    def _detached_task(coro: Any) -> asyncio.Task[None]:
        """Start service work with a fresh context, never a closed turn context."""

        return asyncio.create_task(coro, context=contextvars.Context())

    def resume_pending_research(self) -> None:
        """Resume durable polling when the tool is built during gateway startup."""

        if self._research_store is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # A synchronous construction is supported by tests and tooling. The first
            # async invocation will retry the resume scan.
            return
        for pending in self._research_store.pending():
            self._schedule_research_poll(pending, loop=loop)

    def _schedule_research_poll(
        self,
        pending: PendingResearch,
        *,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        if pending.task_id in self._background_task_ids:
            return
        loop = loop or asyncio.get_running_loop()
        task = self._detached_task(
            self._poll_research(
                pending.worker,
                pending.task_id,
                pending.skill,
                pending.context_id,
                pending.reference_task_ids,
                pending.effect_id,
                pending.channel,
                pending.chat_id,
            )
        )
        self._background.add(task)
        self._background_task_ids.add(pending.task_id)

        def _finished(done: asyncio.Task[None]) -> None:
            self._background.discard(done)
            self._background_task_ids.discard(pending.task_id)
            if not done.cancelled():
                try:
                    done.exception()
                except Exception as exc:  # pragma: no cover - defensive callback
                    logger.warning(
                        "A2A research worker ended unexpectedly error_type={}", type(exc).__name__
                    )

        task.add_done_callback(_finished)

    async def execute(self, **kwargs: Any) -> str:
        # A tool can be created before the event loop exists; make restart recovery
        # deterministic at the first real call as well as at normal async startup.
        self.resume_pending_research()
        worker, skill, input = (
            str(kwargs.get("worker") or ""),
            str(kwargs.get("skill") or ""),
            kwargs.get("input"),
        )
        if not worker or not skill or not isinstance(input, dict):
            raise ValueError("worker, skill, and structured input are required")
        if skill not in {"search.web", "research.deep"}:
            raise ValueError("a2a_delegate supports only search.web and research.deep")
        allowed, note, effect_id = self._claim(worker, skill, input)
        if not allowed:
            return f"[{worker} | not-sent | {note}]"
        logger.info(
            "A2A delegation started channel={} chat={} worker={} skill={}",
            safe_log_token(self._channel, max_length=40),
            private_log_identifier(self._chat_id),
            safe_log_token(worker),
            safe_log_token(skill),
        )
        try:
            result = await self._registry.invoke_skill(
                worker, skill, input, context_id=self._context_id()
            )
        except Exception as exc:
            self._settle(effect_id, "unknown")
            logger.warning(
                "A2A delegation failed channel={} chat={} worker={} skill={} error_type={}",
                safe_log_token(self._channel, max_length=40),
                private_log_identifier(self._chat_id),
                safe_log_token(worker),
                safe_log_token(skill),
                type(exc).__name__,
            )
            raise
        self._settle(effect_id, "sent")
        if skill == "research.deep" and result.state in {
            "TASK_STATE_SUBMITTED",
            "TASK_STATE_WORKING",
        }:
            turn = current_tool_context()
            channel = turn.channel if turn is not None else self._channel
            chat_id = turn.chat_id if turn is not None else self._chat_id
            pending = PendingResearch(
                task_id=result.task_id,
                worker=worker,
                skill=result.skill,
                context_id=result.context_id,
                reference_task_ids=tuple(result.reference_task_ids),
                channel=channel,
                chat_id=chat_id,
                effect_id=effect_id,
            )
            if self._research_store is not None:
                self._research_store.put(pending)
            self._schedule_research_poll(pending)
        logger.info(
            "A2A delegation completed channel={} chat={} worker={} skill={} task_id={} context_id={} state={}",
            safe_log_token(self._channel, max_length=40),
            private_log_identifier(self._chat_id),
            safe_log_token(result.worker),
            safe_log_token(result.skill),
            safe_log_token(result.task_id),
            safe_log_token(result.context_id),
            safe_log_token(result.state, max_length=80),
        )
        output = (
            json.dumps(result.output, ensure_ascii=False, sort_keys=True)
            if result.output is not None
            else (
                f"error={result.error_code} retryable={result.retryable}"
                if result.error_code
                else ""
            )
        )
        return f"[{result.worker} | {result.skill} | {result.state} | {result.task_id}]\n{output}".rstrip()

    async def _poll_research(
        self,
        worker: str,
        task_id: str,
        skill: str,
        context_id: str,
        reference_task_ids: tuple[str, ...],
        effect_id: str,
        channel: str,
        chat_id: str,
    ) -> None:
        final_content: str
        extensions = 0
        while True:
            try:
                result = await self._registry.poll_task(
                    worker,
                    task_id,
                    skill=skill,
                    context_id=context_id,
                    reference_task_ids=reference_task_ids,
                )
            except A2APollTimeoutError:
                # A single poll already covers the client's maximum window. Extend it a
                # bounded number of times before reporting a timeout, so long research
                # is not failed while it is still progressing.
                if extensions < self._research_poll_extensions:
                    extensions += 1
                    logger.info(
                        "A2A research poll extended worker={} skill={} extension={}",
                        safe_log_token(worker),
                        safe_log_token(skill),
                        extensions,
                    )
                    continue
                final_content = POLL_TIMEOUT_CONTENT
            except A2ATransportError:
                final_content = "error=TRANSPORT_FAILURE retryable=True"
            except A2AProtocolError:
                final_content = "error=PROTOCOL_FAILURE retryable=False"
            except Exception as exc:
                logger.warning(
                    "A2A research polling failed worker={} error_type={}",
                    safe_log_token(worker),
                    type(exc).__name__,
                )
                final_content = "error=POLL_FAILURE retryable=False"
            else:
                final_content = (
                    json.dumps(result.output, ensure_ascii=False, sort_keys=True)
                    if result.output is not None
                    else f"error={result.error_code} retryable={result.retryable}"
                )
            break
        if self._delivery is not None and channel and chat_id:
            try:
                receipt = await self._delivery.send(
                    source="a2a",
                    operation_ref=f"a2a-result:{effect_id}",
                    channel=channel,
                    chat_id=chat_id,
                    content=final_content,
                )
            except Exception as exc:
                logger.warning(
                    "A2A research result delivery failed worker={} error_type={}",
                    safe_log_token(worker),
                    type(exc).__name__,
                )
            else:
                state = str(getattr(receipt, "state", "") or "")
                if receipt is not None and state not in {"sent", "delivered"}:
                    logger.warning(
                        "A2A research result remains pending worker={} state={}",
                        safe_log_token(worker),
                        safe_log_token(state or "unknown"),
                    )
                    return
                if self._research_store is not None:
                    self._research_store.delete(task_id, effect_id=effect_id)
