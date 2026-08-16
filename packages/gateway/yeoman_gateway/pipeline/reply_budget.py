"""Derive per-turn reply budget metadata after implicit addressing."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from yeoman_gateway.core.pipeline import NextFn, PipelineContext
from yeoman_gateway.reply_budget import derive_reply_budget


class ReplyBudgetMiddleware:
    """Attach trusted reply-budget metadata for context and final enforcement."""

    async def __call__(self, ctx: PipelineContext, next: NextFn) -> None:
        decision = ctx.decision
        if decision is None:
            await next(ctx)
            return

        raw = dict(ctx.event.raw_metadata or {})
        state_raw = raw.get("conversation_state")
        state = state_raw if isinstance(state_raw, dict) else {}
        answer_shape = str(state.get("answer_shape") or "short_take").strip() or "short_take"
        budget = derive_reply_budget(
            policy=decision.reply_budget,
            answer_shape=answer_shape,
            content=ctx.event.content,
            is_owner=bool(decision.is_owner),
        )
        if budget is None:
            await next(ctx)
            return

        raw["reply_budget"] = budget.as_metadata()
        raw = self._trim_ambient_context(raw, budget.ambient_window_limit)
        if budget.session_history_limit is not None and decision.session_history_limit is None:
            decision = replace(decision, session_history_limit=budget.session_history_limit)
            ctx.decision = decision
        ctx.event = replace(ctx.event, raw_metadata=raw)
        ctx.metric("reply_budget_derived", labels=(("channel", ctx.event.channel), ("shape", answer_shape)))
        await next(ctx)

    @staticmethod
    def _trim_ambient_context(
        metadata: dict[str, Any],
        limit: int | None,
    ) -> dict[str, Any]:
        if limit is None:
            return metadata
        ambient = metadata.get("ambient_context_window")
        if isinstance(ambient, list) and len(ambient) > limit:
            metadata["ambient_context_window"] = ambient[-limit:]
        return metadata
