"""Run synthetic scenarios through the real Participation context builder and Judge."""

from __future__ import annotations

import asyncio
import tempfile
import time
from pathlib import Path
from typing import Any

from yeoman_shared.raw_archive.writer import RawArchive, RawEvent

from yeoman_gateway.evaluation.scenarios import Scenario
from yeoman_gateway.history.context import history_turn
from yeoman_gateway.history.live import HistoryProjector
from yeoman_gateway.history.project import project as project_history
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


def _project_scenario(scenario: Scenario, root: Path) -> tuple[RawArchive, Path]:
    raw_root = root / "raw"
    archive = RawArchive(
        root=raw_root, spool=root / "spool", status_path=root / "raw-status.json",
        clock=lambda: scenario.now_ms, media_enabled=False,
    )
    for message in scenario.messages:
        chat = scenario.chats[message.chat]
        person = scenario.people[message.sender]
        native = {
            "type": "message",
            "payload": {
                "chatJid": chat.chat_id, "timestamp": message.at_ms,
                "messageId": message.id, "text": message.text,
                "senderId": person.sender_id, "participantJid": person.sender_id,
                "senderName": person.name, "replyToMessageId": message.reply_to or None,
                "fromAssistant": False,
            },
        }
        event = RawEvent(
            channel="whatsapp", kind="message", direction="in", native=native,
            native_id=message.id, chat_id=chat.chat_id, received_ms=message.at_ms,
        )
        if not archive.append_durable(event):
            raise RuntimeError(f"could not archive evaluation event {message.id}")
    db_path = root / "history.db"
    project_history((raw_root,), db_path, publish_lineage_root=raw_root)
    return archive, db_path


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
        snapshot={
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
        policy=None, source_authorizer=lambda _row: True,
    )
    started = time.perf_counter()
    cache_dir = Path.home() / ".cache" / "yeoman" / "evaluation"
    cache_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="history-", dir=cache_dir) as temp:
        archive, db_path = _project_scenario(scenario, Path(temp))
        projector = HistoryProjector(archive.root, db_path, archive)
        await projector.start()
        try:
            async with asyncio.timeout(30):
                while True:
                    health = projector.health()
                    if health["status"] in {"ready", "backlog"}:
                        break
                    if health["status"] == "failed":
                        raise RuntimeError(f"evaluation history projection failed: {health['reason']}")
                    await asyncio.sleep(0.01)
            async with history_turn(projector):
                context = await builder.build(opportunity, inputs=inputs, now_ms=scenario.now_ms)
        finally:
            await projector.stop()
    return opportunity, context, (time.perf_counter() - started) * 1000


class StaticClient:
    """Returns one fixed completion for offline runs."""

    route_key = "eval.stub"
    structured_output = "json_schema_strict"
    model = "stub"

    def __init__(self, content: str) -> None:
        self.content = content

    async def chat_with_usage(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int,
        response_format: dict[str, Any] | None = None,
        max_retries: int | None = None,
    ) -> RouteReply:
        return RouteReply(content=self.content, model="stub", latency_ms=0, finish_reason="stop")


class RecordingClient:
    """Wrap a route client and retain its last reply."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.route_key = getattr(inner, "route_key", "")
        self.structured_output = getattr(inner, "structured_output", "json_schema_strict")
        self.model = getattr(inner, "model", "")
        self.last: RouteReply | None = None

    async def chat_with_usage(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int,
        response_format: dict[str, Any] | None = None,
        max_retries: int | None = None,
    ) -> RouteReply:
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
