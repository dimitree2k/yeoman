"""Reply-context enrichment middleware.

Corresponds to orchestrator stage 4: build ambient window (last N messages)
and reply-context window (messages around the replied-to anchor), injecting
them into ``event.raw_metadata``.
"""

from __future__ import annotations

from dataclasses import replace

from yeoman_gateway.core.models import ArchivedMessage, InboundEvent
from yeoman_gateway.core.pipeline import NextFn, PipelineContext
from yeoman_gateway.history.live import HistoryPaused

#: How much of a replied-to message a window line quotes to name the reply's target.
_REPLY_EXCERPT_CHARS = 60


class ReplyContextMiddleware:
    """Enrich events with ambient and reply context windows."""

    def __init__(
        self,
        *,
        reply_context_window_limit: int = 6,
        reply_context_line_max_chars: int = 500,
        ambient_window_limit: int = 30,
    ) -> None:
        self._reply_window_limit = max(1, int(reply_context_window_limit))
        self._line_max_chars = max(32, int(reply_context_line_max_chars))
        self._ambient_limit = max(0, int(ambient_window_limit))

    @property
    def _archive(self):
        from yeoman_gateway.adapters.reply_archive_history import HistoryReplyArchiveAdapter
        from yeoman_gateway.history.context import current_history_snapshot
        from yeoman_gateway.history.live import HistoryPaused
        from yeoman_gateway.history.queries import HistoryQueries
        snapshot = current_history_snapshot()
        if snapshot is None:
            raise HistoryPaused('history_scope_required')
        return HistoryReplyArchiveAdapter(HistoryQueries(snapshot))

    async def __call__(self, ctx: PipelineContext, next: NextFn) -> None:
        event, lookup_attempted, archive_hit = self._resolve_reply_context(ctx.event)
        ctx.event = event

        if ctx.event.channel == "whatsapp" and lookup_attempted:
            metric_name = "reply_ctx_archive_hit" if archive_hit else "reply_ctx_archive_miss"
            ctx.metric(metric_name, labels=(("channel", ctx.event.channel),))

        await next(ctx)

    # ── Reply context resolution (ported from Orchestrator) ──────────

    def _resolve_reply_context(
        self, event: InboundEvent
    ) -> tuple[InboundEvent, bool, bool]:
        if event.channel != "whatsapp":
            return event, False, False

        reply_to_message_id = (event.reply_to_message_id or "").strip()
        raw = {key: value for key, value in event.raw_metadata.items() if key not in {
            "reply_context_source", "reply_context_window", "ambient_context_window", "ambient_context_rows"}}
        event = replace(event, reply_to_text=None, raw_metadata=raw)
        ambient_lines = self._build_ambient_window(event)
        if ambient_lines:
            raw["ambient_context_window"] = ambient_lines
            raw["ambient_context_rows"] = self._build_ambient_context_rows(event)
        if not reply_to_message_id:
            return replace(event, raw_metadata=raw), False, False

        row = self._archive.lookup_message(event.channel, event.chat_id, reply_to_message_id)
        if row is None:
            return replace(event, raw_metadata=raw), True, False
        raw["reply_context_source"] = "history"
        window_lines = self._build_reply_context_window(event, row)
        if window_lines:
            raw["reply_context_window"] = window_lines
        text = row.text.strip()
        return replace(event, reply_to_text=text or None, raw_metadata=raw), True, bool(text)

    # ── Window builders ──────────────────────────────────────────────

    def _build_reply_context_window(
        self, event: InboundEvent, anchor: ArchivedMessage
    ) -> list[str]:
        if not anchor.message_id or not anchor.sender_id:
            return []
        try:
            before = self._archive.lookup_messages_before(
                event.channel,
                anchor.chat_id or event.chat_id,
                anchor.message_id,
                limit=self._reply_window_limit,
            )
        except HistoryPaused:
            raise
        except Exception:
            return []
        return self._format_lines(before)

    def _build_ambient_window(self, event: InboundEvent) -> list[str]:
        if self._ambient_limit <= 0 or not event.message_id:
            return []
        # Skip ambient window for DMs — session history already covers these messages.
        if not event.is_group:
            return []
        try:
            before = self._archive.lookup_messages_before(
                event.channel,
                event.chat_id,
                event.message_id,
                limit=self._ambient_limit,
            )
        except HistoryPaused:
            raise
        except Exception:
            return []
        return self._format_lines(_without_own_thread(before, event))

    def _build_ambient_context_rows(self, event: InboundEvent) -> list[dict[str, str | None]]:
        if self._ambient_limit <= 0 or not event.message_id:
            return []
        if not event.is_group:
            return []
        try:
            before = self._archive.lookup_messages_before(
                event.channel,
                event.chat_id,
                event.message_id,
                limit=self._ambient_limit,
            )
        except HistoryPaused:
            raise
        except Exception:
            return []
        rows: list[dict[str, str | None]] = []
        for row in reversed(_without_own_thread(before, event)):
            rows.append(
                {
                    "sender_id": row.sender_id,
                    "sender_name": row.sender_name,
                    "participant": row.participant,
                    "text": row.text,
                }
            )
        return rows

    def _format_lines(self, rows: list[ArchivedMessage]) -> list[str]:
        by_id = {row.message_id: row for row in rows if row.message_id}
        lines: list[str] = []
        for row in reversed(rows):
            compact = " ".join(row.text.split())
            if not compact:
                continue
            if len(compact) > self._line_max_chars:
                compact = compact[: self._line_max_chars] + "..."
            speaker = self._resolve_speaker(row)
            replied = self._replied_to(row, by_id)
            if replied is not None:
                speaker += f", replying to {replied}"
            lines.append(f"[{speaker}] {compact}")
        return lines[: max(self._reply_window_limit, self._ambient_limit)]

    def _replied_to(self, row: ArchivedMessage, by_id: dict[str, ArchivedMessage]) -> str | None:
        """Who and what a quoted reply answers, so a flat window keeps its reply chains.

        Without it "Gab Umfragen dazu" reads as an answer to whatever line sits above it.
        """
        target_id = (row.reply_to_message_id or "").strip()
        if not target_id:
            return None
        target = by_id.get(target_id)
        if target is None:
            try:
                target = self._archive.lookup_message(row.channel, row.chat_id, target_id)
            except HistoryPaused:
                raise
            except Exception:
                return None
        if target is None:
            return None
        excerpt = " ".join(target.text.split())
        if not excerpt:
            return self._resolve_speaker(target)
        if len(excerpt) > _REPLY_EXCERPT_CHARS:
            excerpt = excerpt[:_REPLY_EXCERPT_CHARS] + "..."
        return f'{self._resolve_speaker(target)}: "{excerpt}"'

    def _resolve_speaker(self, row: ArchivedMessage) -> str:
        return row.sender_name or row.sender_id or "unknown"


def _own_thread_message_ids(event: InboundEvent) -> frozenset[str]:
    """Message ids of the thread this event belongs to (spec R03: no own duplicates)."""
    raw = dict(getattr(event, "raw_metadata", {}) or {})
    values = raw.get("thread_source_message_ids") or ()
    if isinstance(values, str):
        values = (values,)
    try:
        return frozenset(str(value) for value in values if value)
    except TypeError:
        return frozenset()


def _without_own_thread(rows: list[ArchivedMessage], event: InboundEvent) -> list[ArchivedMessage]:
    """Ambient is background for *other* threads; the own thread is already in context."""
    own = _own_thread_message_ids(event)
    if not own:
        return rows
    return [row for row in rows if not row.message_id or row.message_id not in own]
