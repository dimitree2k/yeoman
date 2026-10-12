"""Access control middleware — policy-denied messages.

Corresponds to orchestrator stages 7B and 9: halt messages where
``accept_message`` or ``should_respond`` is ``False``.
"""

from __future__ import annotations

from yeoman_gateway.core.pipeline import NextFn, PipelineContext


class AccessControlMiddleware:
    """Halt pipeline for messages denied by policy."""

    async def __call__(self, ctx: PipelineContext, next: NextFn) -> None:
        decision = ctx.decision
        if decision is None or decision.accept_message:
            await next(ctx)
            return

        ctx.metric(
            "policy_drop_access",
            labels=(("channel", ctx.event.channel), ("reason", decision.reason)),
        )
        ctx.halt()


class NoReplyFilterMiddleware:
    """Halt pipeline for accepted messages where ``should_respond`` is False.

    Corresponds to orchestrator stage 9.  Message is visible but no LLM
    reply is generated.
    """

    async def __call__(self, ctx: PipelineContext, next: NextFn) -> None:
        decision = ctx.decision
        if decision is None or decision.should_respond:
            await next(ctx)
            return

        ctx.metric(
            "policy_drop_reply",
            labels=(("channel", ctx.event.channel), ("reason", decision.reason)),
        )
        ctx.halt()
