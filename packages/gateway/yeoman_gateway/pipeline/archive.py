"""Archive middleware - persist inbound events for reply-context lookups."""

from __future__ import annotations

from yeoman_gateway.core.pipeline import NextFn, PipelineContext
from yeoman_gateway.core.ports import ReplyArchivePort


class ArchiveMiddleware:
    """Record inbound events in the reply archive (side-effect, never halts)."""

    def __init__(self, *, archive: ReplyArchivePort | None = None) -> None:
        self._archive = archive

    async def __call__(self, ctx: PipelineContext, next: NextFn) -> None:
        if self._archive is not None:
            self._archive.record_inbound(ctx.event)

        await next(ctx)
