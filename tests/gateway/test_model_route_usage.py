from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest
from loguru import logger
from yeoman_gateway.processing.model_route import (
    RouteCallError,
    RouteClient,
    RouteReply,
    _RouteTarget,
)
from yeoman_shared.config.loader import convert_keys
from yeoman_shared.config.schema import Config


@dataclass
class _Response:
    content: str
    usage: dict[str, int] = field(default_factory=dict)
    finish_reason: str = "stop"


class _Provider:
    def __init__(
        self,
        response: object,
        *,
        delay: float = 0.0,
        error: BaseException | None = None,
    ) -> None:
        self.response = response
        self.delay = delay
        self.error = error
        self.calls: list[dict[str, object]] = []

    async def chat(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.response


def _client(
    response: object,
    *,
    delay: float = 0.0,
    error: BaseException | None = None,
    timeout_ms: int = 0,
    fallback_names: tuple[str, ...] = (),
) -> RouteClient:
    client = RouteClient.__new__(RouteClient)
    client.route_key = "test.route"  # type: ignore[attr-defined]
    client.model = "openai/test-model"  # type: ignore[attr-defined]
    client.timeout_ms = timeout_ms  # type: ignore[attr-defined]
    client.reasoning = None  # type: ignore[attr-defined]
    provider = _Provider(response, delay=delay, error=error)
    client._provider = provider  # type: ignore[attr-defined]
    client._primary = _RouteTarget(  # type: ignore[attr-defined]
        profile_name="test_primary",
        model="openai/test-model",
        timeout_ms=timeout_ms,
        reasoning=None,
        provider=provider,
    )
    client._fallback_names = fallback_names  # type: ignore[attr-defined]
    client._config = None  # type: ignore[attr-defined]
    return client


def _config(*, profiles: dict[str, dict[str, Any]] | None = None) -> Config:
    primary: dict[str, Any] = {
        "kind": "chat",
        "model": "groq/openai/gpt-oss-20b",
        "provider": "groq",
    }
    profile_map = {"test_primary": primary}
    profile_map.update(profiles or {})
    return Config.model_validate(
        convert_keys(
            {
                "providers": {"groq": {"apiKey": "test-key"}},
                "models": {
                    "profiles": profile_map,
                    "routes": {"test.route": "test_primary"},
                },
            }
        )
    )


@pytest.mark.asyncio
async def test_chat_with_usage_returns_content_usage_and_model() -> None:
    client = _client(_Response('{"action":"none"}', {"prompt_tokens": 120, "completion_tokens": 9}))
    reply = await client.chat_with_usage([{"role": "user", "content": "x"}], max_tokens=48)

    assert isinstance(reply, RouteReply)
    assert reply.content == '{"action":"none"}'
    assert reply.usage == {"prompt_tokens": 120, "completion_tokens": 9}
    assert reply.model == "openai/test-model"
    assert reply.latency_ms >= 0
    assert reply.finish_reason == "stop"
    assert client._provider.calls[-1].get("max_retries") is None


@pytest.mark.asyncio
async def test_missing_usage_is_an_empty_mapping_and_chat_still_returns_text() -> None:
    client = _client(_Response("hallo"))
    reply = await client.chat_with_usage([{"role": "user", "content": "x"}], max_tokens=8)
    assert reply.usage == {}
    assert await client.chat([{"role": "user", "content": "x"}], max_tokens=8) == "hallo"


@pytest.mark.asyncio
async def test_non_integer_usage_values_are_dropped() -> None:
    client = _client(_Response("x", {"prompt_tokens": 5, "cost": "n/a"}))  # type: ignore[dict-item]
    reply = await client.chat_with_usage([{"role": "user", "content": "x"}], max_tokens=8)
    assert reply.usage == {"prompt_tokens": 5}


@pytest.mark.asyncio
async def test_profile_timeout_applied() -> None:
    client = _client(_Response("late"), delay=0.2, timeout_ms=50)

    with pytest.raises(RouteCallError) as error:
        await client.chat_with_usage([{"role": "user", "content": "x"}], max_tokens=8)

    assert error.value.reason == "timeout"


@pytest.mark.asyncio
async def test_call_timeout_overrides_profile_timeout() -> None:
    # The weekly digest shares the judge's profile but has its own, longer budget.
    client = _client(_Response("slow but in budget"), delay=0.1, timeout_ms=50)

    reply = await client.chat_with_usage(
        [{"role": "user", "content": "x"}], max_tokens=8, timeout_ms=1000
    )

    assert reply.content == "slow but in budget"


@pytest.mark.asyncio
async def test_no_timeout_when_null() -> None:
    client = _client(_Response("hallo"), delay=0.05, timeout_ms=0)

    reply = await client.chat_with_usage([{"role": "user", "content": "x"}], max_tokens=8)

    assert reply.content == "hallo"


@pytest.mark.asyncio
async def test_error_finish_reason_raises() -> None:
    client = _client(_Response("", {}, "error"))

    with pytest.raises(RouteCallError) as error:
        await client.chat_with_usage(
            [{"role": "user", "content": "x"}], max_tokens=48, max_retries=0
        )

    assert error.value.reason == "provider_error"
    assert client._provider.calls[-1]["max_retries"] == 0


@pytest.mark.asyncio
async def test_fallback_used_after_primary_error() -> None:
    fallback_provider = _Provider(_Response("fallback answer"))
    client = _client(_Response("", {}, "error"), fallback_names=("fallback",))
    client._resolve_fallback = lambda name: _RouteTarget(  # type: ignore[method-assign]
        profile_name=name,
        model="openai/fallback-model",
        timeout_ms=0,
        reasoning={"effort": "low"},
        provider=fallback_provider,
    )

    reply = await client.chat_with_usage([{"role": "user", "content": "x"}], max_tokens=8)

    assert reply.content == "fallback answer"
    assert reply.model == "openai/fallback-model"
    assert reply.fallback_used is True
    assert fallback_provider.calls[0]["model"] == "openai/fallback-model"
    assert fallback_provider.calls[0]["reasoning"] == {"effort": "low"}


@pytest.mark.asyncio
async def test_unknown_fallback_profile_skipped() -> None:
    messages: list[str] = []
    sink = logger.add(lambda message: messages.append(message.record["message"]), level="WARNING")
    client = _client(_Response("", {}, "error"), fallback_names=("does_not_exist",))
    client._config = _config()
    try:
        with pytest.raises(RouteCallError) as error:
            await client.chat_with_usage([{"role": "user", "content": "x"}], max_tokens=8)
    finally:
        logger.remove(sink)

    assert error.value.reason == "provider_error"
    assert any("route_fallback_unavailable" in line for line in messages)
    assert any("does_not_exist" in line for line in messages)


@pytest.mark.asyncio
async def test_cancelled_propagates() -> None:
    client = _client(_Response("x"), error=asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await client.chat_with_usage([{"role": "user", "content": "x"}], max_tokens=8)
