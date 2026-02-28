"""Reply-context enrichment middleware.

Corresponds to orchestrator stage 4: build ambient window (last N messages)
and reply-context window (messages around the replied-to anchor), injecting
them into ``event.raw_metadata``.
"""

from __future__ import annotations

from dataclasses import replace

from yeoman.core.models import ArchivedMessage, InboundEvent
from yeoman.core.pipeline import NextFn, PipelineContext
from yeoman.core.ports import ReplyArchivePort


class ReplyContextMiddleware:
    """Enrich events with ambient and reply context windows."""

    def __init__(
        self,
        *,
        archive: ReplyArchivePort | None = None,
        reply_context_window_limit: int = 6,
        reply_context_line_max_chars: int = 500,
        ambient_window_limit: int = 8,
    ) -> None:
        self._archive = archive
        self._reply_window_limit = max(1, int(reply_context_window_limit))
        self._line_max_chars = max(32, int(reply_context_line_max_chars))
        self._ambient_limit = max(0, int(ambient_window_limit))

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
        if self._archive is None or event.channel != "whatsapp":
            return event, False, False

        reply_to_message_id = (event.reply_to_message_id or "").strip()
        has_payload_reply_text = bool((event.reply_to_text or "").strip())
        ambient_lines = self._build_ambient_window(event)

        if not reply_to_message_id:
            if has_payload_reply_text or ambient_lines:
                raw = dict(event.raw_metadata)
                if has_payload_reply_text:
                    raw.setdefault("reply_context_source", "payload")
                if ambient_lines:
                    raw["ambient_context_window"] = ambient_lines
                return replace(event, raw_metadata=raw), False, False
            return event, False, False

        row = self._archive.lookup_message(
            event.channel, event.chat_id, reply_to_message_id
        )
        if row is None:
            row = self._archive.lookup_message_any_chat(
                event.channel,
                reply_to_message_id,
                preferred_chat_id=event.chat_id,
            )
        if row is None:
            if has_payload_reply_text or ambient_lines:
                raw = dict(event.raw_metadata)
                if has_payload_reply_text:
                    raw.setdefault("reply_context_source", "payload")
                if ambient_lines:
                    raw["ambient_context_window"] = ambient_lines
                return replace(event, raw_metadata=raw), True, False
            return event, True, False

        raw = dict(event.raw_metadata)
        raw.setdefault(
            "reply_context_source",
            "payload" if has_payload_reply_text else "archive",
        )
        window_lines = self._build_reply_context_window(event, row)
        if window_lines:
            raw["reply_context_window"] = window_lines
        if ambient_lines:
            raw["ambient_context_window"] = ambient_lines

        if has_payload_reply_text:
            return replace(event, raw_metadata=raw), True, True

        text = row.text.strip()
        if not text:
            return replace(event, raw_metadata=raw), True, False
        raw["reply_context_source"] = "archive"
        return replace(event, reply_to_text=text, raw_metadata=raw), True, True

    # ── Window builders ──────────────────────────────────────────────

    def _build_reply_context_window(
        self, event: InboundEvent, anchor: ArchivedMessage
    ) -> list[str]:
        if self._archive is None or not anchor.message_id or not anchor.sender_id:
            return []
        try:
            before = self._archive.lookup_messages_before(
                event.channel,
                anchor.chat_id or event.chat_id,
                anchor.message_id,
                limit=self._reply_window_limit,
            )
        except Exception:
            return []
        return self._format_lines(before)

    def _build_ambient_window(self, event: InboundEvent) -> list[str]:
        if self._archive is None or self._ambient_limit <= 0 or not event.message_id:
            return []
        try:
            before = self._archive.lookup_messages_before(
                event.channel,
                event.chat_id,
                event.message_id,
                limit=self._ambient_limit,
            )
        except Exception:
            return []
        return self._format_lines(before)

    def _format_lines(self, rows: list[ArchivedMessage]) -> list[str]:
        lines: list[str] = []
        for row in reversed(rows):
            compact = " ".join(row.text.split())
            if not compact:
                continue
            if len(compact) > self._line_max_chars:
                compact = compact[: self._line_max_chars] + "..."
            speaker = (row.sender_id or row.participant or "unknown").strip() or "unknown"
            lines.append(f"[{speaker}] {compact}")
        return lines[: max(self._reply_window_limit, self._ambient_limit)]
