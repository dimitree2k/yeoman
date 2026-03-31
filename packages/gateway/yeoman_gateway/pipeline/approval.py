"""Middleware to intercept workflow approval codes from owner messages."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from yeoman_gateway.core.pipeline import NextFn, PipelineContext
    from yeoman_gateway.cron.workflow_state import PendingApproval, WorkflowState


class ApprovalMiddleware:
    """Intercepts owner messages that match a pending workflow approval code.

    If the message content exactly matches an approval_id, the approval
    is consumed and the trigger callback fires. The message is halted
    (not passed to subsequent middleware).
    """

    def __init__(
        self,
        workflow_state: "WorkflowState",
        trigger_callback: Callable[["PendingApproval"], Awaitable[None]],
    ) -> None:
        self._state = workflow_state
        self._trigger = trigger_callback

    async def __call__(self, ctx: "PipelineContext", next: "NextFn") -> None:
        if not getattr(ctx.decision, "is_owner", False):
            await next(ctx)
            return

        content = ctx.event.content.strip()
        if not content.startswith("wf-approve-"):
            await next(ctx)
            return

        approval = await self._state.match_and_consume(content)
        if approval is None:
            await next(ctx)
            return

        logger.info("Workflow approval matched: {} -> job {}", approval.approval_id, approval.next_job_id)
        await self._trigger(approval)
        ctx.halt()
