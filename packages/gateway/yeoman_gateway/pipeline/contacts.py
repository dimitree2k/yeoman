"""Projected WhatsApp identity enrichment middleware."""

from __future__ import annotations

from dataclasses import replace

from yeoman_gateway.core.pipeline import NextFn, PipelineContext
from yeoman_gateway.history.live import HistoryPaused


class ContactsMiddleware:
    """Resolve WhatsApp sender identity from the active projected snapshot."""

    async def __call__(self, ctx: PipelineContext, next: NextFn) -> None:
        event = ctx.event
        if event.channel == "whatsapp":
            from yeoman_gateway.history.context import current_history_snapshot
            from yeoman_gateway.history.queries import HistoryQueries

            snapshot = current_history_snapshot()
            if snapshot is None:
                raise HistoryPaused("history_scope_required")
            queries = HistoryQueries(snapshot)
            values = (
                str(event.raw_metadata.get("sender_phone_jid") or ""),
                str(event.raw_metadata.get("participant_jid") or ""),
                event.participant or "",
                event.sender_id,
            )
            at_ms = int(event.timestamp.timestamp() * 1000)
            owners = {
                owner
                for value in values
                if value
                and (owner := queries.resolve_identifier(
                    value, at_ms=at_ms, time_basis="native"
                )) is not None
            }
            raw = {
                key: value
                for key, value in event.raw_metadata.items()
                if key not in {
                    "contact_id",
                    "identity_status",
                    "identity_reason",
                    "mentioned_person_candidates",
                }
            }
            if len(owners) == 1:
                (owner,) = owners
                raw.update(
                    contact_id=owner,
                    identity_status="resolved",
                    identity_reason="history",
                )
            ctx.event = replace(event, raw_metadata=raw)
        await next(ctx)
