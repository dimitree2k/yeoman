"""Explicit Yeoman tool for delegating a task to a named A2A worker."""

from __future__ import annotations

import hashlib
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

#: An identical delegation inside the same turn is a retry, not a new task. When the caller
#: has no turn identity, this window bounds how long a repeat counts as a retry.
DELEGATION_WINDOW_MS = 600_000
#: Worker id recorded on the effect, so the outbox shows who executed the delegation.
DELEGATION_WORKER_ID = "a2a_delegate"
#: A synchronous call holds its claim for longer than any worker timeout.
DELEGATION_LEASE_MS = 900_000


class A2ADelegateTool(Tool):
    """Call one configured A2A worker and return its result to Yeoman.

    A delegation is a remote write: the peer runs with its own tools and may act. The A2A
    protocol has no server-side idempotency, so the contract lives here. Before a task is
    sent, its identity is written to the processing journal; the journal is idempotent per
    key, so a retried call finds its own claim and is refused instead of being executed a
    second time on the peer. Without an open journal (legacy, non-processing runtime) there
    is nothing to claim against and the tool behaves as before.
    """

    def __init__(self, registry: A2AWorkerRegistry, *, store: Any | None = None) -> None:
        self._registry = registry
        self._store = store
        self._channel = ""
        self._chat_id = ""
        self._session_key = ""

    def set_context(self, channel: str, chat_id: str, *, session_key: str = "") -> None:
        """Attach the current Yeoman chat to observability and boundary controls."""
        self._channel = str(channel or "")
        self._chat_id = str(chat_id or "")
        self._session_key = str(session_key or "")

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
            },
            "required": ["worker", "message"],
            "additionalProperties": False,
        }

    def _effective_context_id(self, requested: Any) -> str | None:
        """Keep model-supplied context handles out of channel-bound calls.

        A bound Yeoman turn is deliberately stateless at the A2A boundary until
        an internal session store can namespace and authorize follow-ups. The
        client generates a fresh context when this returns ``None``. Unbound
        callers retain the old explicit-context compatibility path.
        """
        if self._channel or self._chat_id or self._session_key:
            return None
        return requested

    def _turn_identity(self) -> str:
        """What identifies the current turn for idempotency purposes."""
        context = current_tool_context()
        if context is not None:
            if context.reply_to_message_id:
                return f"turn:{context.reply_to_message_id}"
            if context.turn_id:
                return f"turn:{context.turn_id}"
        # No turn identity (unbound caller): a bounded window still refuses an immediate
        # retry without blocking a deliberate request made later.
        return f"window:{int(time.time() * 1000) // DELEGATION_WINDOW_MS}"

    def _claim_delegation(self, worker: str, message: str) -> tuple[bool, str, str]:
        """Durably claim this delegation in the effect outbox.

        Returns ``(allowed, note, effect_id)``. The outbox is the idempotency contract the
        processing mode requires: it returns the original id for an identical retry, refuses
        a different payload under the same operation key, and keeps its tombstones past
        retention - so one operation key can never silently fire twice. The key is derived
        from the turn, the worker and the normalised task, so a retry repeats it while a new
        request in a later turn does not.
        """
        store = self._store
        if store is None:
            return True, "no-outbox", ""
        # Normalise first: the same task with different spacing is the same delegation, and
        # the payload must be a deterministic function of it or the outbox would call it a
        # conflict instead of a retry.
        normalised = " ".join(message.split())
        digest = hashlib.sha256(normalised.encode("utf-8")).hexdigest()
        context = current_tool_context()
        channel = context.channel if context is not None else self._channel
        chat_id = context.chat_id if context is not None else self._chat_id
        operation_key = (
            f"a2a:{worker}:{channel}:{chat_id}:{self._turn_identity()}:{digest[:32]}"
        )
        effect_id = "a2a-" + hashlib.sha256(operation_key.encode("utf-8")).hexdigest()[:32]
        now = int(time.time() * 1000)
        try:
            stored = store.enqueue_effect(
                effect_id=effect_id,
                operation_key=operation_key,
                payload=ExternalActionPayload(
                    action="a2a_delegate",
                    arguments={
                        "worker": worker,
                        "message_sha256": digest,
                        "message_chars": len(normalised),
                    },
                ),
                now_ms=now,
                trace_id=operation_key,
                capability="a2a_delegate",
                target=EffectTarget(channel="a2a", chat_id=worker),
                state="queued",
            )
        except EffectConflictError:
            # Same key, different task: never treat that as a retry.
            return False, "conflict", effect_id
        except Exception as exc:
            # An unattributable delegation is worse than a refused one: fail closed.
            logger.warning(
                "A2A delegation could not be claimed worker={} error_type={}",
                safe_log_token(worker),
                type(exc).__name__,
            )
            return False, "claim-failed", ""
        if str(stored) != effect_id:
            return False, f"duplicate:{stored}", str(stored)
        if not store.claim_effect(
            effect_id, DELEGATION_WORKER_ID, now, DELEGATION_LEASE_MS
        ):
            return False, "busy", effect_id
        return True, "claimed", effect_id

    def _settle_delegation(self, effect_id: str, *, state: str, detail: str) -> None:
        """Record how the delegation ended.

        ``sent`` means the peer answered. ``unknown`` means the request may have reached it
        and the outcome is unproven - the processing rules never retry such an effect, and
        the reconciler owns it from here.
        """
        store = self._store
        if store is None or not effect_id:
            return
        try:
            recorded = store.transition(
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
            return
        if not recorded:
            logger.warning(
                "A2A delegation outcome not recorded effect_id={} state={} detail={}",
                safe_log_token(effect_id),
                state,
                safe_log_token(detail, max_length=48),
            )

    async def execute(self, **kwargs: Any) -> str:
        worker = str(kwargs.get("worker") or "")
        message = str(kwargs.get("message") or "")
        context_id = self._effective_context_id(kwargs.get("context_id"))
        allowed, note, effect_id = self._claim_delegation(worker, message)
        if not allowed:
            logger.warning(
                "A2A delegation refused channel={} chat={} worker={} note={}",
                safe_log_token(self._channel, max_length=40),
                private_log_identifier(self._chat_id),
                safe_log_token(worker),
                safe_log_token(note, max_length=48),
            )
            return (
                f"[{worker} | not-sent | {note}]\n"
                "This task was NOT sent to the worker. If the note says duplicate, the "
                "identical task already went out for this turn - use that result instead of "
                "repeating the call."
            )
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
            # The request may have reached the peer: an unresolved write, not a failure.
            self._settle_delegation(effect_id, state="unknown", detail=type(exc).__name__)
            logger.warning(
                "A2A delegation failed channel={} chat={} worker={} error_type={}",
                safe_log_token(self._channel, max_length=40),
                private_log_identifier(self._chat_id),
                safe_log_token(worker),
                safe_log_token(type(exc).__name__, max_length=80),
            )
            raise
        self._settle_delegation(effect_id, state="sent", detail=result.state)
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
