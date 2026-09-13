from __future__ import annotations

import asyncio

import pytest
from yeoman_gateway.a2a.client import A2AWorker, A2AWorkerResult
from yeoman_gateway.a2a.registry import A2AWorkerRegistry
from yeoman_gateway.agent.tools.a2a import A2ADelegateTool


@pytest.mark.asyncio
async def test_registry_and_delegate_use_structured_skill_input() -> None:
    calls: list[tuple[str, dict[str, object], str | None]] = []

    class FakeClient:
        async def invoke_skill(self, skill: str, input: dict[str, object], *, context_id: str | None = None, reference_task_ids=()) -> A2AWorkerResult:
            del reference_task_ids
            calls.append((skill, input, context_id))
            return A2AWorkerResult("hermes", "task-3", "ctx-3", "TASK_STATE_COMPLETED", skill, {"results": []})

    registry = A2AWorkerRegistry([A2AWorker(name="hermes", url="http://127.0.0.1:9900")], client_factory=lambda worker: FakeClient())
    tool = A2ADelegateTool(registry)

    assert set(tool.parameters["required"]) == {"worker", "skill", "input"}
    assert "message" not in tool.parameters["properties"]
    assert await tool.execute(worker="hermes", skill="search.web", input={"query": "weather"}) == '[hermes | search.web | TASK_STATE_COMPLETED | task-3]\n{"results": []}'
    assert calls == [("search.web", {"query": "weather"}, None)]


@pytest.mark.asyncio
async def test_registry_rejects_unknown_worker() -> None:
    with pytest.raises(KeyError, match="unknown A2A worker 'missing'"):
        await A2AWorkerRegistry([]).invoke_skill("missing", "search.web", {"query": "q"})


@pytest.mark.asyncio
async def test_working_research_is_polled_and_delivered() -> None:
    class Client:
        async def invoke_skill(self, skill, input, *, context_id=None, reference_task_ids=()):
            del input
            return A2AWorkerResult("hermes", "research-1", context_id or "ctx-1", "TASK_STATE_WORKING", skill, reference_task_ids=tuple(reference_task_ids))
        async def poll_task(self, task_id, *, skill, context_id, reference_task_ids=()):
            assert (task_id, skill, context_id, tuple(reference_task_ids)) == ("research-1", "research.deep", "ctx-1", ())
            return A2AWorkerResult("hermes", task_id, context_id, "TASK_STATE_COMPLETED", skill, {"report": "done", "sources": []})
    class Delivery:
        def __init__(self): self.sent = []
        async def send(self, **kwargs): self.sent.append(kwargs)

    delivery = Delivery()
    tool = A2ADelegateTool(A2AWorkerRegistry([A2AWorker(name="hermes", url="http://127.0.0.1:9900")], client_factory=lambda _: Client()), delivery=delivery)
    tool.set_context("whatsapp", "chat@g.us")
    assert "TASK_STATE_WORKING" in await tool.execute(worker="hermes", skill="research.deep", input={"question": "q", "idempotency_key": "r1"})
    while tool._background:
        await asyncio.sleep(0)
    assert delivery.sent == [{"source": "a2a", "operation_ref": "a2a-result:", "channel": "whatsapp", "chat_id": "chat@g.us", "content": '{"report": "done", "sources": []}'}]


@pytest.mark.asyncio
async def test_research_timeout_and_protocol_failures_have_distinct_delivery_codes() -> None:
    from yeoman_gateway.a2a.client import (
        A2APollTimeoutError,
        A2AProtocolError,
        A2ATransportError,
    )
    class Delivery:
        def __init__(self): self.sent = []
        async def send(self, **kwargs): self.sent.append(kwargs)
    class Registry:
        def __init__(self, error): self.error = error
        async def poll_task(self, *args, **kwargs): raise self.error
    result = A2AWorkerResult("hermes", "t", "c", "TASK_STATE_WORKING", "research.deep")
    for error, expected in (
        (A2APollTimeoutError("x"), "POLL_TIMEOUT"),
        (A2AProtocolError("x"), "PROTOCOL_FAILURE"),
        (A2ATransportError("x"), "TRANSPORT_FAILURE"),
        (RuntimeError("signed private secret"), "POLL_FAILURE"),
    ):
        delivery = Delivery()
        tool = A2ADelegateTool(Registry(error), delivery=delivery)
        await tool._poll_research("hermes", result, "e", "whatsapp", "chat")
        assert expected in delivery.sent[0]["content"]
        assert len(delivery.sent) == 1
        assert "signed private secret" not in delivery.sent[0]["content"]


def test_workers_are_loopback_only_unless_explicitly_enabled() -> None:
    from yeoman_gateway.a2a.client import A2AWorkerConfigurationError

    with pytest.raises(A2AWorkerConfigurationError, match="loopback"):
        A2AWorker(name="remote", url="https://example.test/a2a")


def test_router_exposes_gateway_store() -> None:
    from types import SimpleNamespace

    from yeoman_gateway.processing.dispatch import IntentEffectRouter
    from yeoman_shared.config.schema import Config

    store = object()
    assert IntentEffectRouter(gateway=SimpleNamespace(store=store), config=Config.model_validate({"processing": {"enabled": True}})).store is store


def test_processing_fence_keeps_a2a_delegate_reachable() -> None:
    from yeoman_gateway.agent.tools.registry import ToolRegistry
    from yeoman_gateway.processing.dispatch import (
        NON_MIGRATED_CAPABILITIES,
        disable_non_migrated_tools,
    )

    class Tool:
        def __init__(self, name: str) -> None:
            self.name = name
        def to_schema(self): return {"type": "function", "function": {"name": self.name}}
        def validate_params(self, params): return []
        async def execute(self, **kwargs): return ""

    registry = ToolRegistry()
    for name in ("message", "a2a_delegate", *NON_MIGRATED_CAPABILITIES):
        registry.register(Tool(name))
    assert "a2a_delegate" not in disable_non_migrated_tools(registry)
