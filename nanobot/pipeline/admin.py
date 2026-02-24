"""Admin command routing middleware.

Corresponds to orchestrator stage 5: intercept admin commands (``/policy``,
``/approve``, etc.) and return early with their response.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from nanobot.core.intents import RecordMetricIntent, SendOutboundIntent
from nanobot.core.models import OutboundEvent
from nanobot.core.pipeline import NextFn, PipelineContext

if TYPE_CHECKING:
    from nanobot.core.admin_commands import AdminCommandResult
    from nanobot.core.models import InboundEvent


class AdminCommandMiddleware:
    """Intercept admin commands before policy evaluation."""

    def __init__(
        self,
        *,
        handler: Callable[["InboundEvent"], "AdminCommandResult | str | None"] | None = None,
    ) -> None:
        self._handler = handler

    async def __call__(self, ctx: PipelineContext, next: NextFn) -> None:
        if self._handler is None:
            await next(ctx)
            return

        from nanobot.core.admin_commands import AdminCommandResult

        admin_result = self._handler(ctx.event)
        if isinstance(admin_result, str):
            admin_result = AdminCommandResult(status="handled", response=admin_result)
        if admin_result is None:
            await next(ctx)
            return

        # Emit any metric events attached to the admin result.
        for metric in admin_result.metric_events:
            ctx.intents.append(
                RecordMetricIntent(
                    name=metric.name,
                    value=metric.value,
                    labels=metric.labels,
                )
            )

        if admin_result.status == "ignored":
            ctx.metric(
                "admin_command_denied_or_ignored",
                labels=(
                    ("channel", ctx.event.channel),
                    ("command", admin_result.command_name or "unknown"),
                ),
            )
            # Not intercepting — fall through to normal flow.
            await next(ctx)
            return

        if admin_result.intercepts_normal_flow:
            metric_name = (
                "admin_command_handled"
                if admin_result.status == "handled"
                else "admin_command_unknown"
            )
            ctx.metric(
                metric_name,
                labels=(
                    ("channel", ctx.event.channel),
                    ("command", admin_result.command_name or "unknown"),
                ),
            )
            ctx.metric("policy_admin_command", labels=(("channel", ctx.event.channel),))

            if admin_result.response:
                ctx.intents.append(
                    SendOutboundIntent(
                        event=OutboundEvent(
                            channel=ctx.event.channel,
                            chat_id=ctx.event.chat_id,
                            content=admin_result.response,
                        )
                    )
                )
            ctx.halt()
            return

        # Non-intercepting result — continue normal pipeline.
        await next(ctx)
