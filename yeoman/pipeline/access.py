"""Access control middleware — policy-denied messages.

Corresponds to orchestrator stages 7B and 9: handle messages where
``accept_message`` or ``should_respond`` is ``False``, with optional
background notes capture for memory.
"""

from __future__ import annotations

from nanobot.core.intents import QueueMemoryNotesCaptureIntent
from nanobot.core.pipeline import NextFn, PipelineContext
from nanobot.core.ports import SecurityPort


class AccessControlMiddleware:
    """Halt pipeline for messages denied by policy, with optional notes capture."""

    def __init__(self, *, security: SecurityPort | None = None) -> None:
        self._security = security

    async def __call__(self, ctx: PipelineContext, next: NextFn) -> None:
        decision = ctx.decision
        if decision is None:
            await next(ctx)
            return

        if not decision.accept_message:
            self._maybe_capture_notes(ctx, path="blocked")
            ctx.metric(
                "policy_drop_access",
                labels=(("channel", ctx.event.channel), ("reason", decision.reason)),
            )
            ctx.halt()
            return

        await next(ctx)

    def _maybe_capture_notes(
        self,
        ctx: PipelineContext,
        *,
        path: str,
    ) -> None:
        """Enqueue background memory capture for blocked/silent messages."""
        decision = ctx.decision
        if decision is None or not decision.notes_enabled:
            return

        if path == "blocked" and not decision.notes_allow_blocked_senders:
            ctx.metric("memory_notes_dropped_policy", labels=(("channel", ctx.event.channel),))
            return

        event = ctx.event
        if self._security is not None:
            result = self._security.check_input(
                event.content,
                context={
                    "channel": event.channel,
                    "chat_id": event.chat_id,
                    "sender_id": event.sender_id,
                    "message_id": event.message_id or "",
                    "path": "memory_notes_background",
                },
            )
            if result.decision.action == "block":
                ctx.metric(
                    "security_input_blocked",
                    labels=(("channel", event.channel), ("reason", result.decision.reason)),
                )
                ctx.metric(
                    "memory_notes_dropped_security",
                    labels=(("channel", event.channel),),
                )
                return

        ctx.intents.append(
            QueueMemoryNotesCaptureIntent(
                channel=event.channel,
                chat_id=event.chat_id,
                sender_id=event.sender_id,
                message_id=event.message_id,
                content=event.content,
                is_group=event.is_group,
                mode=decision.notes_mode,
                batch_interval_seconds=decision.notes_batch_interval_seconds,
                batch_max_messages=decision.notes_batch_max_messages,
            )
        )
        ctx.metric("memory_notes_enqueued", labels=(("channel", event.channel),))


class NoReplyFilterMiddleware:
    """Halt pipeline for accepted messages where ``should_respond`` is False.

    Corresponds to orchestrator stage 9.  Message is visible but no LLM
    reply is generated.  Background notes capture still happens.
    """

    def __init__(self, *, security: SecurityPort | None = None) -> None:
        self._security = security

    async def __call__(self, ctx: PipelineContext, next: NextFn) -> None:
        decision = ctx.decision
        if decision is None or decision.should_respond:
            await next(ctx)
            return

        # Capture notes for silent messages.
        _enqueue_notes(ctx, self._security)
        ctx.metric(
            "policy_drop_reply",
            labels=(("channel", ctx.event.channel), ("reason", decision.reason)),
        )
        ctx.halt()


def _enqueue_notes(ctx: PipelineContext, security: SecurityPort | None) -> None:
    """Shared notes-capture logic for access control and no-reply filters."""
    decision = ctx.decision
    if decision is None or not decision.notes_enabled:
        return

    event = ctx.event
    if security is not None:
        result = security.check_input(
            event.content,
            context={
                "channel": event.channel,
                "chat_id": event.chat_id,
                "sender_id": event.sender_id,
                "message_id": event.message_id or "",
                "path": "memory_notes_background",
            },
        )
        if result.decision.action == "block":
            ctx.metric(
                "security_input_blocked",
                labels=(("channel", event.channel), ("reason", result.decision.reason)),
            )
            ctx.metric(
                "memory_notes_dropped_security",
                labels=(("channel", event.channel),),
            )
            return

    ctx.intents.append(
        QueueMemoryNotesCaptureIntent(
            channel=event.channel,
            chat_id=event.chat_id,
            sender_id=event.sender_id,
            message_id=event.message_id,
            content=event.content,
            is_group=event.is_group,
            mode=decision.notes_mode,
            batch_interval_seconds=decision.notes_batch_interval_seconds,
            batch_max_messages=decision.notes_batch_max_messages,
        )
    )
    ctx.metric("memory_notes_enqueued", labels=(("channel", event.channel),))
