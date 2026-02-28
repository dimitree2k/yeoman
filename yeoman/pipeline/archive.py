"""Archive middleware — persist inbound events for reply-context lookups.

Corresponds to orchestrator stage 3: record the event (and its quoted
reply, if present) in the reply archive for later ambient/reply window
construction.
"""

from __future__ import annotations

from dataclasses import replace

from nanobot.core.pipeline import NextFn, PipelineContext
from nanobot.core.ports import ReplyArchivePort


class ArchiveMiddleware:
    """Record inbound events in the reply archive (side-effect, never halts)."""

    def __init__(self, *, archive: ReplyArchivePort | None = None) -> None:
        self._archive = archive

    async def __call__(self, ctx: PipelineContext, next: NextFn) -> None:
        if self._archive is not None:
            event = ctx.event
            self._archive.record_inbound(event)
            # Also seed the archive with the quoted message if available.
            if event.reply_to_message_id and event.reply_to_text:
                seeded = replace(
                    event,
                    message_id=event.reply_to_message_id,
                    content=event.reply_to_text,
                )
                self._archive.record_inbound(seeded)

        await next(ctx)
