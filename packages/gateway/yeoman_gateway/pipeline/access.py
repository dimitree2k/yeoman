"""Access control middleware for policy-denied messages."""

from __future__ import annotations

from yeoman_gateway.core.pipeline import NextFn, PipelineContext


class AccessControlMiddleware:
    """Halt pipeline for messages denied by policy."""

    async def __call__(self, ctx: PipelineContext, next: NextFn) -> None:
        decision = ctx.decision
        if decision is None:
            await next(ctx)
            return

        if not decision.accept_message:
            ctx.metric(
                "policy_drop_access",
                labels=(("channel", ctx.event.channel), ("reason", decision.reason)),
            )
            ctx.halt()
            return

        await next(ctx)

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
