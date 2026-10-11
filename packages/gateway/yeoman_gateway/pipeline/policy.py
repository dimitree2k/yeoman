"""Policy evaluation middleware.

Corresponds to orchestrator stage 6: evaluate the event against the policy
engine and store the decision in ``ctx.decision`` for downstream middleware.
"""

from __future__ import annotations

from dataclasses import replace

from yeoman_gateway.core.pipeline import NextFn, PipelineContext
from yeoman_gateway.core.ports import PolicyPort


class PolicyMiddleware:
    """Evaluate inbound event and set ``ctx.decision``."""

    def __init__(self, *, policy: PolicyPort, legacy_history_disabled: bool = False) -> None:
        self._policy = policy
        self._legacy_history_disabled = legacy_history_disabled

    async def __call__(self, ctx: PipelineContext, next: NextFn) -> None:
        ctx.decision = self._policy.evaluate(ctx.event)
        if self._legacy_history_disabled:
            ctx.decision = replace(ctx.decision, notes_enabled=False)
        await next(ctx)
