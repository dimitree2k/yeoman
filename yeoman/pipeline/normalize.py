"""Normalization middleware — empty-content guard.

Corresponds to orchestrator stage 1: strip whitespace and drop empty events.
"""

from __future__ import annotations

from dataclasses import replace

from nanobot.core.pipeline import NextFn, PipelineContext


class NormalizationMiddleware:
    """Drop events with empty content after whitespace stripping."""

    async def __call__(self, ctx: PipelineContext, next: NextFn) -> None:
        normalized = ctx.event.normalized_content()
        if not normalized:
            ctx.metric("event_drop_empty", labels=(("channel", ctx.event.channel),))
            ctx.halt()
            return

        ctx.event = replace(ctx.event, content=normalized)
        await next(ctx)
