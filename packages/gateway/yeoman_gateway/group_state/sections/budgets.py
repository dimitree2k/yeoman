"""Spend visible in the current window: judge calls, comments and reactions."""

from __future__ import annotations

from ..contract import BuildContext
from ..models import Section
from ._common import make_section

_MINUTE = 60_000
_HOUR = 60 * _MINUTE
_TTL_MS = 10 * _MINUTE


class BudgetsSection:
    key = "budgets"
    schema_version = 1
    trust = "runtime"
    layer = "live"

    def applies_to(self, channel: str, chat_kind: str) -> bool:
        return channel == "whatsapp" and chat_kind == "group"

    async def build(self, ctx: BuildContext) -> Section:
        attempts = []
        speakups = ctx.sources.speakups
        if speakups is not None:
            attempts = await speakups.judge_attempts_since(
                channel=ctx.channel, chat_id=ctx.chat_id, since_ms=ctx.now_ms - _HOUR
            )
        newest = max(attempts, key=lambda item: int(item.get("created_at_ms", 0)), default=None)
        comments = [
            int(item["created_at_ms"])
            for item in attempts
            if str(item.get("outcome") or "") == "comment"
            and int(item["created_at_ms"]) >= ctx.now_ms - 30 * _MINUTE
        ]
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
                "judge_hourly_limit": int(newest.get("hourly_limit", 0)) if newest else 0,
                "last_comment_ms": max(comments, default=None),
                "comments_last_30m": len(comments),
                "reactions_last_30m": reactions,
            },
        )

    def migrate(self, from_version: int, data):  # pragma: no cover - schema v1 only
        return data
