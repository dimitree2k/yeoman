"""Policy evaluation middleware.

Corresponds to orchestrator stage 6: evaluate the event against the policy
engine and store the decision in ``ctx.decision`` for downstream middleware.
"""

from __future__ import annotations

from nanobot.core.pipeline import NextFn, PipelineContext
from nanobot.core.ports import PolicyPort


class PolicyMiddleware:
    """Evaluate inbound event and set ``ctx.decision``."""

    def __init__(self, *, policy: PolicyPort) -> None:
        self._policy = policy

    async def __call__(self, ctx: PipelineContext, next: NextFn) -> None:
        ctx.decision = self._policy.evaluate(ctx.event)
        await next(ctx)
