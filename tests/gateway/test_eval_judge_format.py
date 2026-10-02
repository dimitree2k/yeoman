from __future__ import annotations

import json
from pathlib import Path

import pytest
from yeoman_gateway.evaluation.discretion import DISCRETION_FILE
from yeoman_gateway.evaluation.harness import StaticClient
from yeoman_gateway.evaluation.judge_format import (
    build_eval_client,
    failure_class,
    run_judge_format,
)
from yeoman_gateway.evaluation.scenarios import load_scenario_file
from yeoman_gateway.processing.model_route import RouteReply, RouteUnavailableError
from yeoman_gateway.processing.participation import ParticipationDecisionError
from yeoman_shared.config.loader import convert_keys
from yeoman_shared.config.schema import Config

RUNNABLE = [s for s in load_scenario_file(DISCRETION_FILE) if not s.requires]


def _config() -> Config:
    return Config.model_validate(convert_keys({
        "providers": {"groq": {"apiKey": "synthetic-test-key"}},
        "models": {
            "profiles": {
                "security_classifier": {
                    "kind": "chat",
                    "model": "groq/openai/gpt-oss-20b",
                    "provider": "groq",
                }
            },
            "routes": {"security.classify": "security_classifier"},
        },
    }))


def _patch_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    class NoCallProvider:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def chat(self, **_kwargs: object) -> None:
            raise AssertionError("evaluation tests must not call a provider")

    monkeypatch.setattr(
        "yeoman_gateway.providers.litellm_provider.LiteLLMProvider", NoCallProvider
    )


def test_profile_override_is_applied_to_a_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_provider(monkeypatch)
    config = _config()
    client = build_eval_client(
        config, profile="security_classifier", structured_output="prompt_only"
    )
    assert client.route_key == "eval.judge" and client.structured_output == "prompt_only"
    assert "eval.judge" not in config.models.routes
    assert config.models.profiles["security_classifier"].structured_output == "json_schema_strict"


def test_unknown_profile_fails_before_provider_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    class NoCallProvider:
        def __init__(self, **_kwargs: object) -> None:
            nonlocal calls
            calls += 1

    monkeypatch.setattr(
        "yeoman_gateway.providers.litellm_provider.LiteLLMProvider", NoCallProvider
    )
    with pytest.raises(RouteUnavailableError, match="nope"):
        build_eval_client(_config(), profile="nope")
    assert calls == 0


@pytest.mark.parametrize(
    ("reason", "detail", "expected"),
    [
        ("invalid_response", "output_truncated", "output_truncated"),
        ("invalid_response", "not_json_object", "not_json_object"),
        ("invalid_response", "unknown_action", "schema_invalid:unknown_action"),
        ("unknown_evidence", "target_not_supplied", "schema_invalid:target_not_supplied"),
        ("empty_response", "", "empty_response"),
        ("provider_error", "x", "provider_error"),
    ],
)
def test_failure_classes(reason: str, detail: str, expected: str) -> None:
    assert failure_class(ParticipationDecisionError(reason, detail=detail)) == expected


async def test_harness_counts_failures_and_records_raw_outputs(tmp_path: Path) -> None:
    class Alternating:
        route_key = "eval.judge"
        structured_output = "prompt_only"
        model = "fake"

        def __init__(self) -> None:
            self.n = 0

        async def chat_with_usage(
            self, messages, *, max_tokens, response_format=None, max_retries=None
        ):
            self.n += 1
            content = (
                '{"action": "silence", "intent": "initiate", "reason": "q"}'
                if self.n % 2
                else "oops"
            )
            return RouteReply(
                content=content, model="fake", latency_ms=10 * self.n, finish_reason="stop"
            )

    summary = await run_judge_format(
        Alternating(), RUNNABLE, runs=2, allowed_emojis=("👍",), record_dir=tmp_path
    )
    assert summary.attempts == len(RUNNABLE) * 2
    assert summary.failures == summary.attempts // 2
    assert summary.by_class == {"not_json_object": summary.failures}
    recorded = sorted(tmp_path.glob("*.json"))
    assert len(recorded) == summary.attempts
    assert json.loads(recorded[0].read_text())["raw"]


async def test_static_success_has_zero_failure_rate() -> None:
    client = StaticClient('{"action": "silence", "intent": "initiate", "reason": "q"}')
    summary = await run_judge_format(client, RUNNABLE, runs=1, allowed_emojis=("👍",))
    assert summary.failure_rate == 0.0
