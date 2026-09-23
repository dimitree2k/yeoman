from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from yeoman_gateway.processing.model_route import RouteClient, RouteReply


@dataclass
class _Response:
    content: str
    usage: dict[str, int] = field(default_factory=dict)
    finish_reason: str = "stop"


class _Provider:
    def __init__(self, response: object) -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []

    async def chat(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return self.response


def _client(response: object) -> RouteClient:
    client = RouteClient.__new__(RouteClient)
    client.route_key = "test.route"  # type: ignore[attr-defined]
    client.model = "openai/test-model"  # type: ignore[attr-defined]
    client.timeout_ms = 0  # type: ignore[attr-defined]
    client._provider = _Provider(response)  # type: ignore[attr-defined]
    return client


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
async def test_finish_reason_and_explicit_retry_limit_are_preserved() -> None:
    client = _client(_Response("", {}, "error"))
    reply = await client.chat_with_usage(
        [{"role": "user", "content": "x"}], max_tokens=48, max_retries=0
    )
    assert reply.finish_reason == "error"
    assert client._provider.calls[-1]["max_retries"] == 0
