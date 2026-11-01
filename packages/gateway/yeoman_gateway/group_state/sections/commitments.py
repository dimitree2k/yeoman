"""Open commitments: decisions, polls and reminders by id and time only."""

from __future__ import annotations

from typing import Any

from ..contract import BuildContext
from ..models import Section
from ._common import make_section

_HOUR = 3_600_000
_TTL_MS = _HOUR


class CommitmentsSection:
    key = "commitments"
    schema_version = 1
    trust = "runtime"
    layer = "live"

    def applies_to(self, channel: str, chat_kind: str) -> bool:
        return channel == "whatsapp" and chat_kind == "group"

    async def build(self, ctx: BuildContext) -> Section:
        decisions: list[dict[str, Any]] = []
        if ctx.sources.decisions is not None:
            for item in ctx.sources.decisions.list_open(ctx.chat_id):
                decisions.append(
                    {
                        "id": str(getattr(item, "id", "")),
                        "created_by": str(getattr(item, "created_by", "")),
                        "due_at_ms": getattr(item, "due_at_ms", None),
                        "status": str(getattr(item, "status", "")),
                    }
                )
        polls: list[dict[str, Any]] = []
        if ctx.sources.polls is not None:
            for item in ctx.sources.polls.open_for(ctx.chat_id, ctx.now_ms):
                if item.get("closed_ms") is not None:
                    continue
                polls.append(
                    {
                        "poll_id": item.get("poll_id"),
                        "opened_ms": item.get("opened_ms"),
                        "closed_ms": item.get("closed_ms"),
                    }
                )
        reminders: list[dict[str, Any]] = []
        if ctx.sources.cron is not None:
            for job in ctx.sources.cron.list_jobs(chat_id=ctx.chat_id):
                state = getattr(job, "state", None)
                schedule = getattr(job, "schedule", None)
                at_ms = getattr(state, "next_run_at_ms", None)
                if at_ms is None:
                    at_ms = getattr(schedule, "at_ms", None)
                reminders.append({"job_id": str(getattr(job, "id", "")), "at_ms": at_ms})
        return make_section(
            ctx,
            key=self.key,
            layer=self.layer,
            trust=self.trust,
            ttl_ms=_TTL_MS,
            data={"decisions": decisions, "polls": polls, "reminders": reminders},
        )

    def migrate(self, from_version: int, data):  # pragma: no cover - schema v1 only
        return data
