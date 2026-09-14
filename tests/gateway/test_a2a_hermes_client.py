from __future__ import annotations

import json

import httpx
import pytest
from yeoman_gateway.a2a.client import A2AClient, A2AProtocolError, A2AWorker


def _card(*skills: str) -> dict[str, object]:
    return {
        "name": "Hermes",
        "version": "1.0.1",
        "supportedInterfaces": [
            {
                "url": "http://127.0.0.1:9900/a2a",
                "protocolBinding": "JSONRPC",
                "protocolVersion": "1.0",
            }
        ],
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "extensions": [{"uri": "urn:hermes-yeoman:a2a-profile:v1", "required": False}]
        },
        "skills": [
            {"id": skill, "inputModes": ["application/json"], "outputModes": ["application/json"]}
            for skill in skills
        ],
    }


@pytest.mark.asyncio
async def test_polling_client_accepts_optional_streaming_and_push_capabilities() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        card = _card("search.web")
        capabilities = card["capabilities"]
        assert isinstance(capabilities, dict)
        capabilities["streaming"] = True
        capabilities["pushNotifications"] = True
        return httpx.Response(200, json=card)

    client = A2AClient(
        A2AWorker(name="hermes", url="http://127.0.0.1:9900"),
        transport=httpx.MockTransport(handler),
    )

    card = await client.discover()
    assert card["capabilities"]["streaming"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extension",
    [
        None,
        {"uri": "urn:other-profile:v1", "required": False},
        {"uri": "urn:hermes-yeoman:a2a-profile:v1", "required": "false"},
        "urn:hermes-yeoman:a2a-profile:v1",
    ],
)
async def test_client_rejects_missing_wrong_or_malformed_profile_extension(extension: object) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        card = _card("search.web")
        capabilities = card["capabilities"]
        assert isinstance(capabilities, dict)
        capabilities["extensions"] = [] if extension is None else [extension]
        return httpx.Response(200, json=card)

    client = A2AClient(
        A2AWorker(name="hermes", url="http://127.0.0.1:9900"),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(A2AProtocolError, match="does not carry the Hermes profile"):
        await client.discover()


def _task(*, state: str, output: dict[str, object], task_id: str = "task-1") -> dict[str, object]:
    return {
        "id": task_id,
        "contextId": "ctx-1",
        "status": {"state": state},
        "artifacts": [
            {
                "parts": [
                    {
                        "data": {
                            "skill": "search.web",
                            "status": "completed",
                            "output": output,
                            "correlation": {"task_id": task_id, "context_id": "ctx-1"},
                        },
                        "mediaType": "application/json",
                    }
                ]
            }
        ],
    }


@pytest.mark.asyncio
async def test_invoke_search_web_sends_exact_structured_data_part() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json=_card("search.web"))
        payload = json.loads(request.content)
        assert payload["method"] == "message/send"
        assert payload["params"]["message"]["parts"] == [
            {
                "data": {"skill": "search.web", "input": {"query": "weather", "max_results": 2}},
                "mediaType": "application/json",
            }
        ]
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {"task": _task(state="TASK_STATE_COMPLETED", output={"results": []})},
            },
        )

    client = A2AClient(
        A2AWorker(name="hermes", url="http://127.0.0.1:9900"),
        transport=httpx.MockTransport(handler),
    )

    result = await client.invoke_skill("search.web", {"query": "weather", "max_results": 2}, context_id="ctx-1")

    assert result.task_id == "task-1"
    assert result.output == {"results": []}
    assert [request.method for request in requests] == ["GET", "POST"]


@pytest.mark.asyncio
async def test_research_returns_immediate_working_task_without_polling() -> None:
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        if request.method == "GET":
            return httpx.Response(200, json=_card("research.deep"))
        payload = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {"task": {"id": "research-1", "contextId": "ctx-1", "status": {"state": "TASK_STATE_WORKING"}}},
            },
        )

    client = A2AClient(A2AWorker(name="hermes", url="http://127.0.0.1:9900"), transport=httpx.MockTransport(handler))

    result = await client.invoke_skill(
        "research.deep", {"question": "What changed?", "idempotency_key": "research-1"}, context_id="ctx-1"
    )

    assert result.state == "TASK_STATE_WORKING"
    assert result.task_id == "research-1"
    assert calls == ["GET", "POST"]


@pytest.mark.asyncio
async def test_trading_analyze_sends_the_versioned_structured_invocation() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json=_card("trading.analyze"))
        payload = json.loads(request.content)
        assert payload["params"]["message"]["parts"] == [{
            "data": {
                "skill": "trading.analyze",
                "input": {"question": "Analyse AAPL", "idempotency_key": "trading-1"},
            },
            "mediaType": "application/json",
        }]
        task_id = "trading-1"
        context_id = payload["params"]["message"]["contextId"]
        result = {
            "skill": "trading.analyze",
            "status": "completed",
            "output": {"report": "done", "sources": []},
            "correlation": {"task_id": task_id, "context_id": context_id},
        }
        task = {
            "id": task_id,
            "contextId": context_id,
            "status": {"state": "TASK_STATE_COMPLETED"},
            "artifacts": [{"parts": [{"data": result, "mediaType": "application/json"}]}],
        }
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": payload["id"], "result": {"task": task}},
        )

    client = A2AClient(
        A2AWorker(name="hermes", url="http://127.0.0.1:9900"),
        transport=httpx.MockTransport(handler),
    )

    result = await client.invoke_skill(
        "trading.analyze", {"question": "Analyse AAPL", "idempotency_key": "trading-1"}
    )

    assert result.output == {"report": "done", "sources": []}
    assert [request.method for request in requests] == ["GET", "POST"]


@pytest.mark.asyncio
async def test_get_task_validates_final_artifact_and_correlation() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=_card("search.web"))
        payload = json.loads(request.content)
        assert payload["method"] == "tasks/get"
        assert payload["params"] == {"id": "task-1"}
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": payload["id"], "result": _task(state="TASK_STATE_COMPLETED", output={"results": []})},
        )

    client = A2AClient(A2AWorker(name="hermes", url="http://127.0.0.1:9900"), transport=httpx.MockTransport(handler))

    result = await client.get_task("task-1", skill="search.web", context_id="ctx-1")

    assert result.output == {"results": []}


@pytest.mark.asyncio
async def test_polling_keeps_reference_correlation_until_final_result() -> None:
    polls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal polls
        if request.method == "GET":
            return httpx.Response(200, json=_card("search.web"))
        polls += 1
        task = _task(state="TASK_STATE_COMPLETED", output={"results": []})
        task["artifacts"][0]["parts"][0]["data"]["correlation"]["reference_task_ids"] = ["prior-1"]
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": json.loads(request.content)["id"], "result": task})

    client = A2AClient(A2AWorker(name="hermes", url="http://127.0.0.1:9900"), transport=httpx.MockTransport(handler))
    result = await client.poll_task("task-1", skill="search.web", context_id="ctx-1", reference_task_ids=["prior-1"], deadline_seconds=1, interval_seconds=0.01)

    assert result.reference_task_ids == ("prior-1",)
    assert polls == 1


@pytest.mark.asyncio
async def test_any_live_advertised_skill_with_a_contract_schema_dispatches() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=_card("conversation"))
        payload = json.loads(request.content)
        assert payload["params"]["message"]["parts"][0]["data"] == {"skill": "conversation", "input": {"text": "hello"}}
        task = _task(state="TASK_STATE_COMPLETED", output={"text": "hi"})
        task["artifacts"][0]["parts"][0]["data"]["skill"] = "conversation"
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": {"task": task}})

    client = A2AClient(A2AWorker(name="hermes", url="http://127.0.0.1:9900"), transport=httpx.MockTransport(handler))
    assert (await client.invoke_skill("conversation", {"text": "hello"}, context_id="ctx-1")).output == {"text": "hi"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("card", "skill", "message"),
    [
        (_card("research.deep"), "search.web", "does not advertise skill"),
        ({"supportedInterfaces": []}, "search.web", "interface"),
    ],
)
async def test_client_rejects_unadvertised_or_malformed_card(
    card: dict[str, object], skill: str, message: str
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=card)

    client = A2AClient(A2AWorker(name="hermes", url="http://127.0.0.1:9900"), transport=httpx.MockTransport(handler))

    with pytest.raises(A2AProtocolError, match=message):
        await client.invoke_skill(skill, {"query": "q"})


@pytest.mark.asyncio
async def test_client_rejects_malformed_result_and_jsonrpc_error() -> None:
    count = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal count
        if request.method == "GET":
            return httpx.Response(200, json=_card("search.web"))
        count += 1
        request_id = json.loads(request.content)["id"]
        if count == 1:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": request_id, "result": {"task": {}}})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": request_id, "error": {"code": -1, "message": "denied"}})

    client = A2AClient(A2AWorker(name="hermes", url="http://127.0.0.1:9900"), transport=httpx.MockTransport(handler))
    with pytest.raises(A2AProtocolError, match="task id"):
        await client.invoke_skill("search.web", {"query": "q"})
    with pytest.raises(A2AProtocolError, match="returned an error") as caught:
        await client.invoke_skill("search.web", {"query": "q"})
    assert caught.value.code == -1


@pytest.mark.asyncio
async def test_get_task_rejects_a_different_returned_task_id() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=_card("search.web"))
        payload = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": _task(
                    task_id="task-other",
                    state="TASK_STATE_COMPLETED",
                    output={"results": []},
                ),
            },
        )

    client = A2AClient(
        A2AWorker(name="hermes", url="http://127.0.0.1:9900"),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(A2AProtocolError, match="task id"):
        await client.get_task("task-1", skill="search.web", context_id="ctx-1")


def test_old_text_send_message_api_is_removed() -> None:
    assert not hasattr(A2AClient, "send_message")


@pytest.mark.asyncio
async def test_client_rejects_cross_origin_and_mismatched_rpc_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_TOKEN", "secret")

    async def cross_origin(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={**_card("search.web"), "supportedInterfaces": [{"url": "http://127.0.0.2:9900/a2a", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}]})

    client = A2AClient(A2AWorker(name="hermes", url="http://127.0.0.1:9900", auth_token_env="HERMES_TOKEN"), transport=httpx.MockTransport(cross_origin))
    with pytest.raises(A2AProtocolError, match="trusted origin"):
        await client.invoke_skill("search.web", {"query": "q"})

    async def wrong_id(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=_card("search.web"))
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": "other", "result": {"task": _task(state="TASK_STATE_COMPLETED", output={"results": []})}})

    client = A2AClient(A2AWorker(name="hermes", url="http://127.0.0.1:9900"), transport=httpx.MockTransport(wrong_id))
    with pytest.raises(A2AProtocolError, match="id"):
        await client.invoke_skill("search.web", {"query": "q"}, context_id="ctx-1")


@pytest.mark.asyncio
async def test_client_rejects_boolean_jsonrpc_error_code() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=_card("search.web"))
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": json.loads(request.content)["id"], "error": {"code": True, "message": "no"}})

    client = A2AClient(A2AWorker(name="hermes", url="http://127.0.0.1:9900"), transport=httpx.MockTransport(handler))
    with pytest.raises(A2AProtocolError, match="envelope"):
        await client.invoke_skill("search.web", {"query": "q"})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("skill", "payload"),
    [
        # The 2026-09-14 production failure: unknown fields instead of the required key.
        (
            "research.deep",
            {"question": "Analyse AAPL", "topic": "AAPL stock analysis", "ticker": "AAPL"},
        ),
        # Unknown fields next to the required ones.
        (
            "research.deep",
            {"question": "Analyse AAPL", "idempotency_key": "aapl-1", "symbol": "AAPL"},
        ),
        # Only the required key is missing.
        ("research.deep", {"question": "Analyse AAPL", "output_format": "markdown"}),
        # A value outside the schema bounds.
        ("research.deep", {"question": "Analyse AAPL", "idempotency_key": "aapl-1", "max_sources": 900}),
    ],
)
async def test_local_validation_names_the_offending_field_without_sending(
    skill: str, payload: dict[str, object]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        raise AssertionError(f"nothing may be sent for an invalid invocation: {request.url}")

    client = A2AClient(
        A2AWorker(name="hermes", url="http://127.0.0.1:9900"),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(A2AProtocolError) as caught:
        await client.invoke_skill(skill, payload, context_id="ctx-1")

    message = str(caught.value)
    assert "rejected locally" in message
    assert caught.value.retryable is True
    assert "$" in message
    # The actionable part: which field was wrong.
    assert any(name in message for name in ("idempotency_key", "topic", "ticker", "symbol", "max_sources")), message
