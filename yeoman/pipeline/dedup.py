"""Deduplication middleware — TTL-based message ID cache.

Corresponds to orchestrator stage 2: drop duplicate messages within a
configurable time window.
"""

from __future__ import annotations

import time

from yeoman.core.pipeline import NextFn, PipelineContext


class DeduplicationMiddleware:
    """Reject events whose ``(channel, chat_id, message_id)`` key was seen recently."""

    def __init__(self, *, ttl_seconds: int = 20 * 60) -> None:
        self._ttl_seconds = max(1, int(ttl_seconds))
        self._recent_keys: dict[str, float] = {}
        self._next_cleanup_at = 0.0

    async def __call__(self, ctx: PipelineContext, next: NextFn) -> None:
        key = self._dedupe_key(ctx)
        if key is None:
            await next(ctx)
            return

        now = time.monotonic()
        self._maybe_cleanup(now)

        if key in self._recent_keys:
            ctx.metric("event_drop_duplicate", labels=(("channel", ctx.event.channel),))
            ctx.halt()
            return

        self._recent_keys[key] = now + float(self._ttl_seconds)
        await next(ctx)

    # ── Internals ────────────────────────────────────────────────────

    @staticmethod
    def _dedupe_key(ctx: PipelineContext) -> str | None:
        event = ctx.event
        if not event.message_id:
            return None
        return f"{event.channel}:{event.chat_id}:{event.message_id}"

    def _maybe_cleanup(self, now: float) -> None:
        if now < self._next_cleanup_at:
            return
        expired = [k for k, exp in self._recent_keys.items() if exp <= now]
        for k in expired:
            self._recent_keys.pop(k, None)
        self._next_cleanup_at = now + 30.0
