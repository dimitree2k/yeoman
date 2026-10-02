"""Run synthetic scenarios through the real Participation context builder and Judge."""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from yeoman_gateway.evaluation.scenarios import Scenario
from yeoman_gateway.policy.schema import DEFAULT_PARTICIPATION_GUIDANCE
from yeoman_gateway.processing.model_route import RouteReply
from yeoman_gateway.processing.participation import ParticipationJudge, ParticipationOpportunity
from yeoman_gateway.processing.participation_context import (
    ParticipationContextBounds,
    ParticipationContextBuilder,
    ParticipationDecisionInputs,
)

CONTRIBUTION_TYPES: tuple[str, ...] = (
    "answer_open_question", "surface_memory", "correct_error", "share_opinion",
    "light_humor", "cold_joke",
)


class ScenarioArchive:
    """InboundArchive stand-in over synthetic messages."""

    def __init__(self, scenario: Scenario) -> None:
        self._rows = [
            {
                "message_id": message.id, "channel": "whatsapp",
                "chat_id": scenario.chats[message.chat].chat_id,
                "sender_id": scenario.people[message.sender].sender_id,
                "sender_name": scenario.people[message.sender].name, "text": message.text,
                "timestamp": message.at_ms // 1000, "reply_to_message_id": message.reply_to,
            }
            for message in scenario.messages
        ]

    def lookup_messages_in_range(
        self, channel: str, chat_id: str, since: datetime, until: datetime, *,
        limit: int = 50, latest: bool = True,
    ) -> list[dict[str, Any]]:
        low, high = int(since.timestamp()), int(until.timestamp())
        rows = sorted(
            (row for row in self._rows if row["channel"] == channel and row["chat_id"] == chat_id
             and low <= row["timestamp"] <= high),
            key=lambda row: row["timestamp"],
        )
        limit = max(1, min(int(limit), 300))
        selected = rows[-limit:] if latest else rows[:limit]
        return [dict(row) for row in selected]

    def lookup_message(self, channel: str, chat_id: str, message_id: str) -> dict[str, Any] | None:
        for row in self._rows:
            if (row["channel"], row["chat_id"], row["message_id"]) == (channel, chat_id, message_id):
                return dict(row)
        return None


async def build_today_judge_context(
    scenario: Scenario,
) -> tuple[ParticipationOpportunity, dict[str, object], float]:
    """Build the pre-V1 Judge view for one scenario trigger; include build time in ms."""
    chat = scenario.chats[scenario.trigger.chat]
    opportunity = ParticipationOpportunity(
        opportunity_id=f"eval-{scenario.id}", channel="whatsapp", chat_id=chat.chat_id,
        trigger="inbound", source_event_ids=scenario.trigger.source_ids, observed_revision=1,
        activation_epoch=1, created_at_ms=scenario.now_ms, lane="shadow",
    )
    inputs = ParticipationDecisionInputs(
        snapshot={  # type: ignore[arg-type]  # The builder reads this snapshot as a mapping.
            "allowed_contribution_types": CONTRIBUTION_TYPES,
            "direct_addressed": scenario.trigger.direct,
            "guidance": DEFAULT_PARTICIPATION_GUIDANCE,
        },
        bounds=ParticipationContextBounds(), allowed_actions=("silence", "react", "comment"),
        allowed_intents=frozenset({"initiate", "continue"}),
        remaining_budgets=(("comment", 10), ("react", 10)),
        reservation_limits_by_intent=(), approval_required=False, arbitration_revision=0,
        current_source_ids=scenario.trigger.source_ids, continuation_candidate=False,
    )
    builder = ParticipationContextBuilder(
        archive=ScenarioArchive(scenario), policy=None, source_authorizer=lambda _row: True,
    )
    started = time.perf_counter()
    context = await builder.build(opportunity, inputs=inputs, now_ms=scenario.now_ms)
    return opportunity, context, (time.perf_counter() - started) * 1000


class StaticClient:
    """Returns one fixed completion for offline runs."""

    route_key = "eval.stub"
    structured_output = "json_schema_strict"
    model = "stub"

    def __init__(self, content: str) -> None:
        self.content = content

    async def chat_with_usage(self, messages, *, max_tokens, response_format=None, max_retries=None):
        return RouteReply(content=self.content, model="stub", latency_ms=0, finish_reason="stop")


class RecordingClient:
    """Wrap a route client and retain its last reply."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.route_key = getattr(inner, "route_key", "")
        self.structured_output = getattr(inner, "structured_output", "json_schema_strict")
        self.model = getattr(inner, "model", "")
        self.last: RouteReply | None = None

    async def chat_with_usage(self, messages, *, max_tokens, response_format=None, max_retries=None):
        self.last = None
        self.last = await self._inner.chat_with_usage(
            messages, max_tokens=max_tokens, response_format=response_format, max_retries=max_retries,
        )
        return self.last


def make_judge(
    client: Any, *, allowed_emojis: tuple[str, ...], max_output_tokens: int = 1024,
    max_input_tokens: int = 6000, timeout_seconds: float = 30.0,
) -> ParticipationJudge:
    return ParticipationJudge(
        client=client, allowed_emojis=allowed_emojis, timeout_seconds=timeout_seconds,
        max_input_tokens=max_input_tokens, max_output_tokens=max_output_tokens,
    )
