"""Live presence: who wrote in the last 30 and 120 minutes."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from ..contract import BuildContext
from ..models import Section
from ._common import make_section, names_for, newest_cursor, rows_sources

_MINUTE = 60_000
_TTL_MS = 10 * _MINUTE


class PresenceSection:
    key = "presence"
    schema_version = 1
    trust = "runtime"
    layer = "live"

    def applies_to(self, channel: str, chat_kind: str) -> bool:
        return channel == "whatsapp" and chat_kind == "group"

    async def build(self, ctx: BuildContext) -> Section:
        queries = ctx.history
        rows = [
            row
            for row in queries.recent(
                chat_id=ctx.chat_id,
                limit=500,
                after_ms=ctx.now_ms - 120 * _MINUTE - 1,
                until_ms=ctx.now_ms,
            )
            if row.get("sent_ms") is not None
        ]
        assistant = queries.assistant_contact_ids()
        counts_30: dict[str, int] = defaultdict(int)
        counts_120: dict[str, int] = defaultdict(int)
        messages_30 = messages_120 = 0
        last_message_ms: int | None = None
        arvid_last_ms: int | None = None
        for row in rows:
            moment = int(row["sent_ms"])
            last_message_ms = moment if last_message_ms is None else max(last_message_ms, moment)
            if row.get("direction") == "out":
                arvid_last_ms = moment if arvid_last_ms is None else max(arvid_last_ms, moment)
                continue
            contact_id = str(row.get("sender_contact_id") or "")
            if not contact_id or contact_id in assistant:
                continue
            if moment >= ctx.now_ms - 120 * _MINUTE:
                counts_120[contact_id] += 1
                messages_120 += 1
            if moment >= ctx.now_ms - 30 * _MINUTE:
                counts_30[contact_id] += 1
                messages_30 += 1

        names = names_for(queries, (*counts_30, *counts_120))

        def people(counts: dict[str, int]) -> list[dict[str, Any]]:
            ordered = sorted(
                counts.items(),
                key=lambda item: (-item[1], names.get(item[0], "").casefold(), item[0]),
            )
            return [
                {"contact_id": contact_id, "name": names.get(contact_id, ""), "messages": count}
                for contact_id, count in ordered
            ]

        return make_section(
            ctx,
            key=self.key,
            layer=self.layer,
            trust=self.trust,
            ttl_ms=_TTL_MS,
            sources=rows_sources(rows),
            source_cursor=newest_cursor(rows),
            data={
                "active_30m": people(counts_30),
                "active_120m": people(counts_120),
                "messages_30m": messages_30,
                "messages_120m": messages_120,
                "last_message_ms": last_message_ms,
                "arvid_last_ms": arvid_last_ms,
            },
        )

    def migrate(self, from_version: int, data):  # pragma: no cover - schema v1 only
        return data
