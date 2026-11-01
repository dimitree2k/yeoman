"""Recent conversations: ids, participants and Arvid's registry threads."""

from __future__ import annotations

from typing import Any

from yeoman_gateway.history.conversations import ConversationBounds, read_conversations

from ..contract import BuildContext
from ..models import Section
from ._common import make_section, newest_cursor, row_message_id, rows_sources

_HOUR = 3_600_000
_TTL_MS = 2 * _HOUR


def _thread_id(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (tuple, list)):
        return str(value[0]) if value and value[0] else None
    attribute = getattr(value, "thread_id", None)
    if attribute:
        return str(attribute)
    return str(value) or None


class ConversationsSection:
    key = "conversations"
    schema_version = 1
    trust = "runtime"
    layer = "conversation"

    def applies_to(self, channel: str, chat_kind: str) -> bool:
        return channel == "whatsapp" and chat_kind == "group"

    async def build(self, ctx: BuildContext) -> Section:
        window_ms = max(1, int(getattr(ctx.settings, "window_hours", 24))) * _HOUR
        consciousness = ctx.consciousness
        bounds = ConversationBounds(
            window_ms=window_ms,
            max_rows=500,
            lull_ms=max(1, int(getattr(consciousness, "lull_silence_minutes", 25))) * 60_000,
            burst_n=max(1, int(getattr(consciousness, "burst_threshold_messages", 8))),
            burst_ms=max(1, int(getattr(consciousness, "burst_window_minutes", 15))) * 60_000,
        )
        queries = ctx.history
        rows = queries.recent(
            chat_id=ctx.chat_id,
            limit=500,
            after_ms=ctx.now_ms - window_ms - 1,
            until_ms=ctx.now_ms,
        )
        by_id = {row_message_id(row): row for row in rows}
        processing = ctx.sources.processing
        thread_lookup = getattr(processing, "thread_for_message", None)
        assistant = queries.assistant_contact_ids()
        conversations = read_conversations(
            queries,
            ctx.chat_id,
            ctx.now_ms,
            bounds,
            thread_lookup=thread_lookup if callable(thread_lookup) else None,
        )
        data: list[dict[str, Any]] = []
        for conversation in conversations:
            participants: list[str] = []
            thread_ids: list[str] = []
            for message_id in conversation.message_ids:
                row = by_id.get(message_id)
                if row is None:
                    continue
                contact_id = str(row.get("sender_contact_id") or "")
                if (
                    row.get("direction") != "out"
                    and contact_id
                    and contact_id not in assistant
                    and contact_id not in participants
                ):
                    participants.append(contact_id)
                native_id = str(row.get("native_message_id") or "")
                if row.get("direction") == "out" and native_id and callable(thread_lookup):
                    thread_id = _thread_id(thread_lookup(native_id))
                    if thread_id and thread_id not in thread_ids:
                        thread_ids.append(thread_id)
            data.append(
                {
                    "conversation_id": conversation.id,
                    "start_ms": conversation.start_ms,
                    "last_ms": conversation.last_ms,
                    "state": conversation.state,
                    "messages": conversation.message_count,
                    "participants": participants,
                    "burst": conversation.burst,
                    "arvid_messages": conversation.arvid_messages,
                    "arvid_addressed": conversation.arvid_addressed,
                    "thread_ids": thread_ids,
                    "first_message_id": conversation.message_ids[0],
                    "last_message_id": conversation.message_ids[-1],
                }
            )
        return make_section(
            ctx,
            key=self.key,
            layer=self.layer,
            trust=self.trust,
            ttl_ms=_TTL_MS,
            sources=rows_sources(rows),
            source_cursor=newest_cursor(rows),
            period_start_ms=ctx.now_ms - window_ms,
            period_end_ms=ctx.now_ms,
            data={"conversations": data},
        )

    def migrate(self, from_version: int, data):  # pragma: no cover - schema v1 only
        return data
