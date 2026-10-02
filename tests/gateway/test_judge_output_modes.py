from __future__ import annotations

from typing import Any

import pytest
from yeoman_gateway.processing.model_route import RouteClient, RouteReply
from yeoman_gateway.processing.participation import (
    JUDGE_RESPONSE_FORMAT,
    ParticipationJudge,
    ParticipationOpportunity,
    response_format_for,
)
from yeoman_shared.config.loader import convert_keys
from yeoman_shared.config.schema import Config

NOW = 1_791_000_000_000


def _config(**profile: Any) -> Config:
    base = {"kind": "chat", "model": "groq/openai/gpt-oss-20b", "provider": "groq"}
    base.update(profile)
    return Config.model_validate(
        convert_keys(
            {
                "providers": {"groq": {"apiKey": "test-key"}},
                "models": {
                    "profiles": {"groq_judge": base},
                    "routes": {"eval.judge": "groq_judge"},
                },
            }
        )
    )


def test_structured_output_defaults_to_strict_schema() -> None:
    client = RouteClient(config=_config(), route_key="eval.judge")
    assert client.structured_output == "json_schema_strict"
    assert client.reasoning is None


def test_profile_settings_reach_the_route_client() -> None:
    client = RouteClient(
        config=_config(structuredOutput="prompt_only", reasoning={"effort": "low"}),
        route_key="eval.judge",
    )
    assert client.structured_output == "prompt_only"
    assert client.reasoning == {"effort": "low"}


async def test_reasoning_is_passed_to_the_provider() -> None:
    client = RouteClient(config=_config(reasoning={"effort": "low"}), route_key="eval.judge")
    seen: dict[str, Any] = {}

    async def chat(**kwargs: Any):
        seen.update(kwargs)

        class _R:
            content = "{}"
            usage: dict = {}
            finish_reason = "stop"
            diagnostics: dict = {}

        return _R()

    client._provider.chat = chat  # type: ignore[method-assign]
    await client.chat_with_usage([{"role": "user", "content": "x"}], max_tokens=10)
    assert seen["reasoning"] == {"effort": "low"}


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("json_schema_strict", JUDGE_RESPONSE_FORMAT),
        ("json_object", {"type": "json_object"}),
        ("prompt_only", None),
        ("unknown", JUDGE_RESPONSE_FORMAT),
    ],
)
def test_response_format_for(mode: str, expected: Any) -> None:
    assert response_format_for(mode) == expected


async def test_judge_uses_the_client_mode() -> None:
    captured: dict[str, Any] = {}

    class _Client:
        route_key = "eval.judge"
        structured_output = "prompt_only"

        async def chat_with_usage(
            self, messages, *, max_tokens, response_format=None, max_retries=None
        ):
            captured["response_format"] = response_format
            return RouteReply(
                content='{"action": "silence", "intent": "initiate", "reason": "q"}',
                finish_reason="stop",
            )

    judge = ParticipationJudge(client=_Client())  # type: ignore[arg-type]
    opportunity = ParticipationOpportunity(
        opportunity_id="o",
        channel="whatsapp",
        chat_id="c",
        trigger="inbound",
        source_event_ids=("m",),
        observed_revision=1,
        activation_epoch=1,
        created_at_ms=NOW,
    )
    context = {
        "messages": [{"event_id": "m", "sender": "A", "text": "hi", "timestamp": NOW // 1000}],
        "current_source_ids": ["m"],
        "allowed_actions": ["silence"],
    }
    await judge.decide(opportunity, context)
    assert captured["response_format"] is None
