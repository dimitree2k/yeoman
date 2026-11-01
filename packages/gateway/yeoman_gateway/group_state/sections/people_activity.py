"""Per-person activity over the owner's people window."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from yeoman_gateway.history.conversations import ConversationBounds, read_conversations

from ..contract import BuildContext
from ..models import Section
from ._common import (
    make_section,
    names_for,
    newest_cursor,
    reactions_for,
    row_message_id,
    rows_in_window,
    rows_sources,
)

_HOUR = 3_600_000
_DAY = 24 * _HOUR
_TTL_MS = _DAY
_TOP_PEOPLE = 30


class PeopleActivitySection:
    key = "people_activity"
    schema_version = 1
    trust = "runtime"
    layer = "day"

    def applies_to(self, channel: str, chat_kind: str) -> bool:
        return channel == "whatsapp" and chat_kind == "group"

    def _bounds(self, ctx: BuildContext, window_ms: int) -> ConversationBounds:
        consciousness = ctx.consciousness
        return ConversationBounds(
            window_ms=window_ms,
            max_rows=500,
            lull_ms=max(1, int(getattr(consciousness, "lull_silence_minutes", 25))) * 60_000,
            burst_n=max(1, int(getattr(consciousness, "burst_threshold_messages", 8))),
            burst_ms=max(1, int(getattr(consciousness, "burst_window_minutes", 15))) * 60_000,
        )

    async def build(self, ctx: BuildContext) -> Section:
        days = max(1, int(getattr(ctx.settings, "people_window_days", 7)))
        window_ms = days * _DAY
        queries = ctx.history
        assistant = queries.assistant_contact_ids()
        window_rows, truncated = rows_in_window(
            queries,
            chat_id=ctx.chat_id,
            after_ms=ctx.now_ms - window_ms - 1,
            until_ms=ctx.now_ms,
        )
        rows = [
            row
            for row in window_rows
            if row.get("sent_ms") is not None
            and row.get("direction") != "out"
            and str(row.get("sender_contact_id") or "")
            and str(row["sender_contact_id"]) not in assistant
        ]
        by_id = {row_message_id(row): row for row in rows}
        messages_7d: dict[str, int] = defaultdict(int)
        messages_24h: dict[str, int] = defaultdict(int)
        last_seen: dict[str, int] = {}
        for row in rows:
            contact_id = str(row["sender_contact_id"])
            moment = int(row["sent_ms"])
            messages_7d[contact_id] += 1
            if moment >= ctx.now_ms - _DAY:
                messages_24h[contact_id] += 1
            last_seen[contact_id] = max(last_seen.get(contact_id, moment), moment)

        arvid_native_ids = {
            str(row.get("native_message_id") or "")
            for row in window_rows
            if row.get("direction") == "out" and row.get("native_message_id")
        }
        replies_to_arvid: dict[str, int] = defaultdict(int)
        for row in rows:
            if str(row.get("reply_to_native_id") or "") in arvid_native_ids:
                replies_to_arvid[str(row["sender_contact_id"])] += 1

        reactions_given: dict[str, int] = defaultdict(int)
        reactions_received: dict[str, int] = defaultdict(int)
        message_ids = tuple(by_id)
        if message_ids:
            latest: dict[tuple[str, str], dict[str, Any]] = {}
            events, reactions_truncated = reactions_for(queries, message_ids, until_ms=ctx.now_ms)
            truncated = truncated or reactions_truncated
            for event in events:
                target = str(event.get("target_message_id") or "")
                actor = str(event.get("actor_contact_id") or event.get("actor_identifier") or "")
                if not target or not actor or target not in by_id:
                    continue
                slot = (target, actor)
                previous = latest.get(slot)
                if previous is None or int(event.get("occurred_ms") or 0) >= int(
                    previous.get("occurred_ms") or 0
                ):
                    latest[slot] = event
            for (target, actor), event in latest.items():
                if event.get("removed"):
                    continue
                author = str(by_id[target].get("sender_contact_id") or "")
                reactions_given[actor] += 1
                if author:
                    reactions_received[author] += 1

        conversations_24h: dict[str, int] = defaultdict(int)
        bounds = self._bounds(ctx, _DAY)
        for conversation in read_conversations(queries, ctx.chat_id, ctx.now_ms, bounds):
            in_conversation = {
                str(by_id[message_id]["sender_contact_id"])
                for message_id in conversation.message_ids
                if message_id in by_id
            }
            for contact_id in in_conversation:
                conversations_24h[contact_id] += 1

        contacts = tuple(messages_7d)
        names = names_for(queries, contacts)
        people = [
            {
                "contact_id": contact_id,
                "name": names.get(contact_id, ""),
                "messages_24h": messages_24h.get(contact_id, 0),
                "messages_7d": messages_7d[contact_id],
                "last_seen_ms": last_seen.get(contact_id),
                "conversations_24h": conversations_24h.get(contact_id, 0),
                "reactions_given_7d": reactions_given.get(contact_id, 0),
                "reactions_received_7d": reactions_received.get(contact_id, 0),
                "replies_to_arvid_7d": replies_to_arvid.get(contact_id, 0),
            }
            for contact_id in contacts
        ]
        people.sort(key=lambda item: (-int(item["messages_7d"]), str(item["contact_id"])))
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
            data={"people": people[:_TOP_PEOPLE], "truncated": truncated},
        )

    def migrate(self, from_version: int, data):  # pragma: no cover - schema v1 only
        return data
