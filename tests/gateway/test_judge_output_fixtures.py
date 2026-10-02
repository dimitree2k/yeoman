"""S28: one tolerant extractor for every output mode; strict validation stays authoritative."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from yeoman_gateway.processing.model_route import RouteReply
from yeoman_gateway.processing.participation import (
    ParticipationDecisionError,
    ParticipationJudge,
    ParticipationOpportunity,
    _parse_payload,
)

FIXTURES = Path(__file__).parent / "fixtures" / "judge_outputs"
NOW = 1_791_000_000_000
CONTEXT = {
    "messages": [
        {"event_id": "b2", "sender": "Lisa", "text": "haha", "timestamp": NOW // 1000}
    ],
    "current_source_ids": ["b2"],
    "allowed_actions": ["silence", "react", "comment"],
    "allowed_intents": ["initiate"],
    "allowed_contribution_types": ["light_humor"],
    "guidance": "",
    "direct_addressed": False,
}


class _FixtureClient:
    route_key = "eval.fixture"
    structured_output = "json_schema_strict"

    def __init__(self, fixture: dict[str, Any]) -> None:
        self.fixture = fixture

    async def chat_with_usage(
        self, messages, *, max_tokens, response_format=None, max_retries=None
    ) -> RouteReply:
        return RouteReply(
            content=self.fixture["content"],
            model="fixture",
            latency_ms=1,
            finish_reason=self.fixture["finish_reason"],
            diagnostics={"native_finish_reason": self.fixture["native_finish_reason"]},
        )


def _opportunity() -> ParticipationOpportunity:
    return ParticipationOpportunity(
        opportunity_id="opp-fixture",
        channel="whatsapp",
        chat_id="b@g.us",
        trigger="inbound",
        source_event_ids=("b2",),
        observed_revision=1,
        activation_epoch=1,
        created_at_ms=NOW,
    )


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.json")), ids=lambda p: p.stem)
async def test_fixture_outcome(path: Path) -> None:
    fixture = json.loads(path.read_text(encoding="utf-8"))
    judge = ParticipationJudge(
        client=_FixtureClient(fixture), allowed_emojis=("👍",)
    )  # type: ignore[arg-type]
    expect = fixture["expect"]
    if expect["outcome"] == "decision":
        decision = await judge.decide(_opportunity(), CONTEXT)
        assert decision.action == expect["action"]
    else:
        with pytest.raises(ParticipationDecisionError) as info:
            await judge.decide(_opportunity(), CONTEXT)
        assert info.value.reason == expect["reason"]
        assert info.value.detail == expect["detail"]


def test_braces_in_prose_fail_cleanly() -> None:
    assert _parse_payload('I think {maybe} … {"action": "silence"} and {more}') is None


def test_first_valid_candidate_wins() -> None:
    raw = '```json\n{"action": "react"}\n```\n{"action": "silence"}'
    assert _parse_payload(raw) == {"action": "react"}
