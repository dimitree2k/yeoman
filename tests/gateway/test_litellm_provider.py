from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
import yeoman_gateway.providers.litellm_provider as litellm_provider


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response_format", "requires_parameters"),
    [
        (
            {
                "type": "json_schema",
                "json_schema": {"name": "decision", "strict": True, "schema": {}},
            },
            True,
        ),
        ({"type": "json_object"}, False),
    ],
)
async def test_openrouter_requires_parameters_only_for_json_schema(
    monkeypatch: pytest.MonkeyPatch,
    response_format: dict[str, Any],
    requires_parameters: bool,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="{}", tool_calls=None),
                    finish_reason="stop",
                )
            ],
            usage=None,
        )

    monkeypatch.setattr(litellm_provider, "acompletion", fake_acompletion)
    provider = litellm_provider.LiteLLMProvider(
        api_base="https://openrouter.ai/api/v1",
        default_model="openai/gpt-6-luna",
    )

    await provider.chat(
        [{"role": "user", "content": "return a decision"}],
        temperature=0.0,
        response_format=response_format,
    )

    provider_options = captured.get("extra_body", {}).get("provider", {})
    assert provider_options.get("require_parameters", False) is requires_parameters
    if requires_parameters:
        assert "temperature" not in captured
    else:
        assert captured["temperature"] == 0.0


@pytest.mark.parametrize("native_finish_reason", ["stop", "length", "content_filter"])
def test_empty_completion_keeps_error_status_and_native_finish_reason(native_finish_reason):
    provider = litellm_provider.LiteLLMProvider(default_model="openai/gpt-6-luna")
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="", tool_calls=None),
                finish_reason=native_finish_reason,
            )
        ],
        usage=None,
    )

    result = provider._parse_response(response)

    assert result.finish_reason == "error"
    assert result.diagnostics["native_finish_reason"] == native_finish_reason


@pytest.mark.asyncio
async def test_schema_http_boundary_retains_safe_diagnostics(monkeypatch):
    import json

    import httpx
    from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler
    from yeoman_gateway.processing.participation import JUDGE_RESPONSE_FORMAT

    captured = {}

    def respond(request):
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "gen-test", "object": "chat.completion", "created": 1,
            "model": "openai/gpt-6-luna", "provider": "OpenAI",
            "choices": [{"index": 0, "finish_reason": "length",
                         "message": {"role": "assistant", "content": "",
                                     "reasoning": "PRIVATE REASONING"}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 256,
                      "total_tokens": 356,
                      "completion_tokens_details": {"reasoning_tokens": 256}},
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        handler = AsyncHTTPHandler()
        await handler.client.aclose()
        handler.client = client
        original = litellm_provider.acompletion

        async def completion(**kwargs):
            return await original(**kwargs, client=handler)

        monkeypatch.setattr(litellm_provider, "acompletion", completion)
        provider = litellm_provider.LiteLLMProvider(
            api_base="https://openrouter.ai/api/v1", api_key="synthetic-key",
            default_model="openai/gpt-6-luna",
        )
        result = await provider.chat(
            [{"role": "user", "content": "PRIVATE PROMPT"}],
            max_tokens=256, max_retries=0,
            response_format=JUDGE_RESPONSE_FORMAT,
        )
    assert captured["response_format"]["json_schema"]["strict"] is True
    assert captured["provider"]["require_parameters"] is True
    assert "temperature" not in captured
    assert captured["max_tokens"] == 256
    assert result.finish_reason == "error"
    assert result.diagnostics["native_finish_reason"] == "length"
    assert result.diagnostics["request_id"] == "gen-test"
    assert result.diagnostics["selected_provider"] == "OpenAI"
    assert result.diagnostics["reasoning_tokens"] == 256
    assert result.diagnostics["strict_schema"] is True
    assert result.diagnostics["require_parameters"] is True
    assert "PRIVATE" not in json.dumps(result.diagnostics)
