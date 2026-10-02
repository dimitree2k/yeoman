from __future__ import annotations

from typing import Any

import pytest
from yeoman_gateway.processing.model_route import RouteReply
from yeoman_gateway.processing.participation import (
    ParticipationDecisionError,
    ParticipationJudge,
    ParticipationOpportunity,
)
from yeoman_shared.telemetry import tracing

NOW = 1_791_000_000_000
CONTEXT = {
    "messages": [{"event_id": "m", "sender": "A", "text": "hi", "timestamp": NOW // 1000}],
    "current_source_ids": ["m"],
    "allowed_actions": ["silence"],
}


class _Client:
    route_key = "participation.judge"
    structured_output = "json_schema_strict"

    def __init__(self, content: str) -> None:
        self.content = content

    async def chat_with_usage(
        self, messages, *, max_tokens, response_format=None, max_retries=None
    ):
        return RouteReply(
            content=self.content,
            usage={"prompt_tokens": 10, "completion_tokens": 3},
            model="m1",
            latency_ms=42,
            finish_reason="stop",
        )


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    recorded: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(tracing, "start_trace", lambda **kw: recorded.append(("start", kw)) or "T")
    monkeypatch.setattr(tracing, "log_generation", lambda **kw: recorded.append(("generation", kw)))
    monkeypatch.setattr(
        tracing,
        "end_span",
        lambda span, **kw: recorded.append(("end", {"span": span, **kw})),
    )
    return recorded


def _opportunity() -> ParticipationOpportunity:
    return ParticipationOpportunity(
        opportunity_id="opp-1",
        channel="whatsapp",
        chat_id="c",
        trigger="inbound",
        source_event_ids=("m",),
        observed_revision=1,
        activation_epoch=1,
        created_at_ms=NOW,
    )


async def test_successful_decision_is_traced(calls) -> None:
    judge = ParticipationJudge(
        client=_Client('{"action": "silence", "intent": "initiate", "reason": "q"}')
    )  # type: ignore[arg-type]
    await judge.decide(_opportunity(), CONTEXT)

    assert [kind for kind, _ in calls] == ["start", "generation", "end"]
    start, generation, end = (payload for _, payload in calls)
    assert start["name"] == "participation.judge"
    assert start["metadata"]["opportunity_id"] == "opp-1"
    assert generation["parent"] == "T" and generation["model"] == "m1"
    assert generation["usage"] == {"prompt_tokens": 10, "completion_tokens": 3}
    assert generation["metadata"]["latency_ms"] == 42
    assert end["output"] == {"action": "silence", "intent": "initiate"}


async def test_failed_decision_is_traced_with_its_class(calls) -> None:
    judge = ParticipationJudge(client=_Client("not json"))  # type: ignore[arg-type]
    with pytest.raises(ParticipationDecisionError):
        await judge.decide(_opportunity(), CONTEXT)

    end = calls[-1][1]
    assert end["output"] == {"error": "invalid_response", "detail": "not_json_object"}
    assert end["metadata"] == {"outcome": "error"}
