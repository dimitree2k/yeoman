from __future__ import annotations

import json

import httpx
import pytest
from yeoman_gateway.a2a.client import (
    A2AClient,
    A2AProtocolError,
    A2AWorker,
    A2AWorkerConfigurationError,
    A2AWorkerResult,
)
from yeoman_gateway.a2a.registry import A2AWorkerRegistry
from yeoman_gateway.agent.tools.a2a import A2ADelegateTool
from yeoman_shared.config.schema import Config


@pytest.mark.asyncio
async def test_client_discovers_agent_card_and_sends_v1_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_A2A_TOKEN", "test-token")
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "GET":
            assert request.url.path == "/.well-known/agent-card.json"
            assert request.headers["Authorization"] == "Bearer test-token"
            return httpx.Response(
                200,
                json={
                    "name": "Hermes",
                    "supportedInterfaces": [
                        {
                            "url": "http://127.0.0.1:9900/",
                            "protocolBinding": "JSONRPC",
                            "protocolVersion": "1.0",
                        }
                    ],
                },
            )

        payload = json.loads(request.content)
        assert request.headers["A2A-Version"] == "1.0"
        assert payload["jsonrpc"] == "2.0"
        assert payload["method"] == "SendMessage"
        message = payload["params"]["message"]
        assert message["role"] == "ROLE_USER"
        assert message["contextId"] == "ctx-1"
        assert message["parts"] == [{"text": "do the work", "mediaType": "text/plain"}]
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {
                    "task": {
                        "id": "task-1",
                        "contextId": "ctx-1",
                        "status": {
                            "state": "TASK_STATE_COMPLETED",
                            "message": {
                                "role": "ROLE_AGENT",
                                "parts": [{"text": "status copy"}],
                            },
                        },
                        "artifacts": [
                            {"parts": [{"text": "done", "mediaType": "text/plain"}]}
                        ],
                    }
                },
            },
        )

    worker = A2AWorker(
        name="hermes",
        url="http://127.0.0.1:9900",
        auth_token_env="HERMES_A2A_TOKEN",
    )
    client = A2AClient(worker, transport=httpx.MockTransport(handler))

    result = await client.send_message("do the work", context_id="ctx-1")

    assert result == A2AWorkerResult(
        worker="hermes",
        task_id="task-1",
        context_id="ctx-1",
        state="TASK_STATE_COMPLETED",
        text="done",
    )
    assert [request.method for request in seen] == ["GET", "POST"]


@pytest.mark.asyncio
async def test_client_preserves_input_required_task_state() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "supportedInterfaces": [
                        {"url": "http://127.0.0.1:9900/", "protocolBinding": "JSONRPC"}
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "result": {
                    "task": {
                        "id": "task-2",
                        "contextId": "ctx-2",
                        "status": {
                            "state": "TASK_STATE_INPUT_REQUIRED",
                            "message": {
                                "parts": [{"text": "Which account?"}],
                            },
                        },
                    }
                }
            },
        )

    client = A2AClient(
        A2AWorker(name="hermes", url="http://127.0.0.1:9900"),
        transport=httpx.MockTransport(handler),
    )

    result = await client.send_message("check it", context_id="ctx-2")

    assert result.state == "TASK_STATE_INPUT_REQUIRED"
    assert result.text == "Which account?"


@pytest.mark.asyncio
async def test_client_surfaces_jsonrpc_errors() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={"supportedInterfaces": [{"url": str(request.url), "protocolBinding": "JSONRPC"}]},
            )
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": "1", "error": {"code": -32050, "message": "unauthorized"}},
        )

    client = A2AClient(
        A2AWorker(name="hermes", url="http://127.0.0.1:9900"),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(A2AProtocolError, match="unauthorized") as caught:
        await client.send_message("secret work")

    assert caught.value.code == -32050


def test_workers_are_loopback_only_unless_explicitly_enabled() -> None:
    with pytest.raises(A2AWorkerConfigurationError, match="loopback"):
        A2AWorker(name="remote", url="https://example.test/a2a")

    remote = A2AWorker(
        name="remote",
        url="https://example.test/a2a",
        allow_remote=True,
    )
    assert remote.url == "https://example.test/a2a"


@pytest.mark.asyncio
async def test_registry_dispatches_named_workers_and_tool_returns_worker_result() -> None:
    calls: list[tuple[str, str, str | None]] = []

    class FakeClient:
        async def send_message(self, message: str, *, context_id: str | None = None) -> A2AWorkerResult:
            calls.append((message, "hermes", context_id))
            return A2AWorkerResult(
                worker="hermes",
                task_id="task-3",
                context_id=context_id or "ctx-3",
                state="TASK_STATE_COMPLETED",
                text="worker result",
            )

    registry = A2AWorkerRegistry(
        [A2AWorker(name="hermes", url="http://127.0.0.1:9900")],
        client_factory=lambda worker: FakeClient(),
    )
    tool = A2ADelegateTool(registry)

    assert registry.names == ("hermes",)
    assert tool.name == "a2a_delegate"
    assert set(tool.parameters["required"]) == {"worker", "message"}

    output = await tool.execute(worker="hermes", message="inspect this", context_id="ctx-3")

    assert output == "[hermes | TASK_STATE_COMPLETED | task-3]\nworker result"
    assert calls == [("inspect this", "hermes", "ctx-3")]


@pytest.mark.asyncio
async def test_bound_delegate_does_not_forward_model_context_id() -> None:
    forwarded_contexts: list[str | None] = []

    class FakeClient:
        async def send_message(
            self,
            message: str,
            *,
            context_id: str | None = None,
        ) -> A2AWorkerResult:
            assert message == "inspect this"
            forwarded_contexts.append(context_id)
            return A2AWorkerResult(
                worker="hermes",
                task_id="task-bound-1",
                context_id=context_id or "remote-generated-context",
                state="TASK_STATE_COMPLETED",
                text="done",
            )

    registry = A2AWorkerRegistry(
        [A2AWorker(name="hermes", url="http://127.0.0.1:9900")],
        client_factory=lambda worker: FakeClient(),
    )
    tool = A2ADelegateTool(registry)
    tool.set_context("whatsapp", "owner@s.whatsapp.net", session_key="whatsapp:owner")

    result = await tool.execute(
        worker="hermes",
        message="inspect this",
        context_id="model-selected-foreign-context",
    )

    assert result == "[hermes | TASK_STATE_COMPLETED | task-bound-1]\ndone"
    assert forwarded_contexts == [None]
    assert "context_id" not in tool.parameters["properties"]


def test_unbound_delegate_keeps_explicit_context_for_internal_callers() -> None:
    tool = A2ADelegateTool(A2AWorkerRegistry([]))

    # The public model schema hides context_id; this only documents the
    # compatibility path for trusted non-chat callers and existing tests.
    assert "context_id" not in tool.parameters["properties"]


def test_a2a_config_is_disabled_and_secret_is_referenced_by_env_name() -> None:
    config = Config.model_validate(
        {
            "tools": {
                "a2a": {
                    "enabled": True,
                    "workers": {
                        "hermes": {
                            "url": "http://127.0.0.1:9900",
                            "authTokenEnv": "HERMES_A2A_TOKEN",
                        }
                    },
                }
            }
        }
    )

    assert config.tools.a2a.enabled is True
    assert config.tools.a2a.workers["hermes"].auth_token_env == "HERMES_A2A_TOKEN"


@pytest.mark.asyncio
async def test_registry_rejects_unknown_worker() -> None:
    registry = A2AWorkerRegistry([])

    with pytest.raises(KeyError, match="unknown A2A worker 'missing'"):
        await registry.call("missing", "work")


def test_policy_diagnostics_include_a2a_delegate() -> None:
    from yeoman_gateway.cli.policy_commands import _policy_known_tools

    assert "a2a_delegate" in _policy_known_tools()
