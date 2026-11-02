"""Spend visible in the current window: judge calls, comments and reactions."""

from __future__ import annotations

from ..contract import BuildContext
from ..models import Section
from ._common import make_section

_MINUTE = 60_000
_HOUR = 60 * _MINUTE
#: How far back a quiet chat still knows its limit and its last comment.
_LOOKBACK_MS = 24 * _HOUR
_TTL_MS = 10 * _MINUTE


class BudgetsSection:
    key = "budgets"
    schema_version = 1
    trust = "runtime"
    layer = "live"

    def applies_to(self, channel: str, chat_kind: str) -> bool:
        return channel == "whatsapp" and chat_kind == "group"

    async def build(self, ctx: BuildContext) -> Section:
        recent_attempts: list[dict[str, object]] = []
        speakups = ctx.sources.speakups
        if speakups is not None:
            recent_attempts = await speakups.judge_attempts_since(
                channel=ctx.channel, chat_id=ctx.chat_id, since_ms=ctx.now_ms - _LOOKBACK_MS
            )
        attempts = [
            item for item in recent_attempts if int(item["created_at_ms"]) >= ctx.now_ms - _HOUR
        ]
        # A quiet hour does not make the limit zero: the newest known attempt carries it,
        # and with none in the lookback the limit is unknown (None), not 0.
        newest = max(
            recent_attempts, key=lambda item: int(item.get("created_at_ms", 0)), default=None
        )
        hourly_limit = newest.get("hourly_limit") if newest else None
        comment_times = [
            int(item["created_at_ms"])
            for item in recent_attempts
            if str(item.get("outcome") or "") == "comment"
        ]
        comments = [moment for moment in comment_times if moment >= ctx.now_ms - 30 * _MINUTE]
        reactions = 0
        processing = ctx.sources.processing
        if processing is not None:
            reader = getattr(processing, "recent_reactions", None)
            if callable(reader):
                reactions = len(
                    reader(
                        channel=ctx.channel,
                        chat_id=ctx.chat_id,
                        since_ms=ctx.now_ms - 30 * _MINUTE,
                        limit=500,
                    )
                )
        return make_section(
            ctx,
            key=self.key,
            layer=self.layer,
            trust=self.trust,
            ttl_ms=_TTL_MS,
            data={
                "judge_calls_last_hour": len(attempts),
                "judge_hourly_limit": int(hourly_limit) if hourly_limit is not None else None,
                "last_comment_ms": max(comment_times, default=None),
                "comments_last_30m": len(comments),
                "reactions_last_30m": reactions,
            },
        )

    def migrate(self, from_version: int, data):  # pragma: no cover - schema v1 only
        return data
