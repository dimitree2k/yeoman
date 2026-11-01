"""Arvid's recent actions: judge attempts, delivered text and short replies."""

from __future__ import annotations

from typing import Any

from ..contract import BuildContext
from ..models import Section
from ._common import make_section, newest_cursor, rows_sources

_HOUR = 3_600_000
_TTL_MS = 24 * _HOUR
_ACTION_OUTCOMES = frozenset({"silence", "react", "comment", "meme", "sticker"})


class ArvidActionsSection:
    key = "arvid_actions"
    schema_version = 1
    trust = "runtime"
    layer = "live"

    def applies_to(self, channel: str, chat_kind: str) -> bool:
        return channel == "whatsapp" and chat_kind == "group"

    async def build(self, ctx: BuildContext) -> Section:
        since = ctx.now_ms - 24 * _HOUR
        rows = [
            row
            for row in ctx.history.recent(
                chat_id=ctx.chat_id, limit=500, after_ms=since - 1, until_ms=ctx.now_ms
            )
            if row.get("direction") == "out" and row.get("sent_ms") is not None
        ]
        items: list[dict[str, Any]] = []
        comments = silences = failures = 0
        speakups = ctx.sources.speakups
        attempts: list[dict[str, Any]] = []
        if speakups is not None:
            attempts = await speakups.judge_attempts_since(
                channel=ctx.channel, chat_id=ctx.chat_id, since_ms=since
            )
        for attempt in attempts:
            outcome = str(attempt.get("outcome") or "")
            if outcome == "comment":
                comments += 1
            elif outcome == "silence":
                silences += 1
            elif outcome and outcome not in _ACTION_OUTCOMES:
                failures += 1
            items.append(
                {
                    "kind": "judge",
                    "outcome": outcome,
                    "detail_code": str(attempt.get("detail_code") or ""),
                    "at_ms": int(attempt["created_at_ms"]),
                    "opportunity_id": str(attempt.get("opportunity_id") or ""),
                }
            )
        for row in rows:
            items.append(
                {
                    "kind": "sent",
                    "message_id": str(row["message_id"]),
                    "reply_to_native_id": row.get("reply_to_native_id"),
                    "at_ms": int(row["sent_ms"]),
                }
            )
        processing = ctx.sources.processing
        if processing is not None:
            reader = getattr(processing, "short_reply_decisions_since", None)
            if callable(reader):
                for decision in reader(ctx.channel, ctx.chat_id, since):
                    items.append(
                        {
                            "kind": "short_reply",
                            "status": str(decision.get("status") or ""),
                            "emoji": decision.get("emoji"),
                            "at_ms": int(decision.get("updated_ms") or decision["created_ms"]),
                        }
                    )
        limit = max(1, int(getattr(ctx.settings, "actions_limit", 20)))
        items.sort(key=lambda item: int(item["at_ms"]), reverse=True)
        reactions = 0
        if processing is not None:
            recent = getattr(processing, "recent_reactions", None)
            if callable(recent):
                reactions = len(
                    recent(channel=ctx.channel, chat_id=ctx.chat_id, since_ms=since, limit=500)
                )
        return make_section(
            ctx,
            key=self.key,
            layer=self.layer,
            trust=self.trust,
            ttl_ms=_TTL_MS,
            sources=rows_sources(rows),
            source_cursor=newest_cursor(rows),
            data={
                "items": items[:limit],
                "counts_24h": {
                    "sent": len(rows),
                    "comments": comments,
                    "reactions": reactions,
                    "silences": silences,
                    "failures": failures,
                },
            },
        )

    def migrate(self, from_version: int, data):  # pragma: no cover - schema v1 only
        return data
