from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from yeoman_gateway.a2a.client import A2AWorker, A2AWorkerResult
from yeoman_gateway.a2a.registry import A2AWorkerRegistry
from yeoman_gateway.agent.tools.a2a import A2ADelegateTool
from yeoman_gateway.agent.tools.a2a_research import A2AResearchStore, PendingResearch
from yeoman_gateway.processing.dispatch import CURRENT_TURN
from yeoman_gateway.processing.store import ProcessingStore
from yeoman_gateway.processing.tool_context import (
    ToolInvocationContext,
    current_tool_context,
    reset_tool_context,
    set_tool_context,
)


@pytest.mark.asyncio
async def test_registry_and_delegate_use_structured_skill_input() -> None:
    calls: list[tuple[str, dict[str, object], str | None]] = []

    class FakeClient:
        async def invoke_skill(
            self,
            skill: str,
            input: dict[str, object],
            *,
            context_id: str | None = None,
            reference_task_ids=(),
        ) -> A2AWorkerResult:
            del reference_task_ids
            calls.append((skill, input, context_id))
            return A2AWorkerResult(
                "hermes", "task-3", "ctx-3", "TASK_STATE_COMPLETED", skill, {"results": []}
            )

    registry = A2AWorkerRegistry(
        [A2AWorker(name="hermes", url="http://127.0.0.1:9900")],
        client_factory=lambda worker: FakeClient(),
    )
    tool = A2ADelegateTool(registry)

    assert set(tool.parameters["required"]) == {"worker", "skill", "input"}
    assert "message" not in tool.parameters["properties"]
    assert (
        await tool.execute(worker="hermes", skill="search.web", input={"query": "weather"})
        == '[hermes | search.web | TASK_STATE_COMPLETED | task-3]\n{"results": []}'
    )
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
            return A2AWorkerResult(
                "hermes",
                "research-1",
                context_id or "ctx-1",
                "TASK_STATE_WORKING",
                skill,
                reference_task_ids=tuple(reference_task_ids),
            )

        async def poll_task(self, task_id, *, skill, context_id, reference_task_ids=()):
            assert (task_id, skill, context_id, tuple(reference_task_ids)) == (
                "research-1",
                "research.deep",
                "ctx-1",
                (),
            )
            return A2AWorkerResult(
                "hermes",
                task_id,
                context_id,
                "TASK_STATE_COMPLETED",
                skill,
                {"report": "done", "sources": []},
            )

    class Delivery:
        def __init__(self):
            self.sent = []

        async def send(self, **kwargs):
            self.sent.append(kwargs)

    delivery = Delivery()
    tool = A2ADelegateTool(
        A2AWorkerRegistry(
            [A2AWorker(name="hermes", url="http://127.0.0.1:9900")],
            client_factory=lambda _: Client(),
        ),
        delivery=delivery,
    )
    tool.set_context("whatsapp", "chat@g.us")
    assert "TASK_STATE_WORKING" in await tool.execute(
        worker="hermes", skill="research.deep", input={"question": "q", "idempotency_key": "r1"}
    )
    while tool._background:
        await asyncio.sleep(0)
    assert delivery.sent == [
        {
            "source": "a2a",
            "operation_ref": "a2a-result:",
            "channel": "whatsapp",
            "chat_id": "chat@g.us",
            "content": '{"report": "done", "sources": []}',
        }
    ]


@pytest.mark.asyncio
async def test_research_timeout_and_protocol_failures_have_distinct_delivery_codes() -> None:
    from yeoman_gateway.a2a.client import (
        A2APollTimeoutError,
        A2AProtocolError,
        A2ATransportError,
    )

    class Delivery:
        def __init__(self):
            self.sent = []

        async def send(self, **kwargs):
            self.sent.append(kwargs)

    class Registry:
        def __init__(self, error):
            self.error = error

        async def poll_task(self, *args, **kwargs):
            raise self.error

    result = A2AWorkerResult("hermes", "t", "c", "TASK_STATE_WORKING", "research.deep")
    for error, expected in (
        (A2APollTimeoutError("x"), "error=POLL_TIMEOUT retryable=True"),
        (A2AProtocolError("x"), "error=PROTOCOL_FAILURE retryable=False"),
        (A2ATransportError("x"), "error=TRANSPORT_FAILURE retryable=True"),
        (RuntimeError("signed private secret"), "error=POLL_FAILURE retryable=False"),
    ):
        delivery = Delivery()
        tool = A2ADelegateTool(Registry(error), delivery=delivery)
        await tool._poll_research(
            "hermes", result.task_id, result.skill, result.context_id, (), "e", "whatsapp", "chat"
        )
        assert delivery.sent[0]["content"] == expected
        assert len(delivery.sent) == 1
        assert "signed private secret" not in delivery.sent[0]["content"]


@pytest.mark.asyncio
async def test_research_poll_timeout_extends_instead_of_failing_long_research() -> None:
    """A poll window expiring must not fail research that is still progressing."""
    from yeoman_gateway.a2a.client import A2APollTimeoutError

    class Delivery:
        def __init__(self):
            self.sent = []

        async def send(self, **kwargs):
            self.sent.append(kwargs)

    class Registry:
        def __init__(self):
            self.calls = 0

        async def poll_task(self, worker, task_id, *, skill, context_id, reference_task_ids=()):
            del worker, task_id, skill, context_id, reference_task_ids
            self.calls += 1
            if self.calls <= 2:
                raise A2APollTimeoutError("window expired")
            return A2AWorkerResult(
                "hermes",
                "task-long",
                "ctx-long",
                "TASK_STATE_COMPLETED",
                "research.deep",
                {"report": "long research finished", "sources": ["https://example.test"]},
            )

    registry = Registry()
    delivery = Delivery()
    tool = A2ADelegateTool(registry, delivery=delivery)

    await tool._poll_research(
        "hermes", "task-long", "research.deep", "ctx-long", (), "effect-1", "whatsapp", "chat"
    )

    assert registry.calls == 3, "the expired windows must be retried, not surfaced"
    assert len(delivery.sent) == 1
    assert "long research finished" in delivery.sent[0]["content"]
    assert "POLL_TIMEOUT" not in delivery.sent[0]["content"]


@pytest.mark.asyncio
async def test_research_poll_timeout_is_reported_after_bounded_extensions() -> None:
    """The extension budget is finite: a task that never reports still ends in timeout."""
    from yeoman_gateway.a2a.client import A2APollTimeoutError

    class Delivery:
        def __init__(self):
            self.sent = []

        async def send(self, **kwargs):
            self.sent.append(kwargs)

    class Registry:
        def __init__(self):
            self.calls = 0

        async def poll_task(self, *args, **kwargs):
            del args, kwargs
            self.calls += 1
            raise A2APollTimeoutError("window expired")

    registry = Registry()
    delivery = Delivery()
    tool = A2ADelegateTool(registry, delivery=delivery, research_poll_extensions=2)

    await tool._poll_research(
        "hermes", "task-stuck", "research.deep", "ctx-stuck", (), "effect-2", "whatsapp", "chat"
    )

    assert registry.calls == 3, "one initial window plus exactly two extensions"
    assert len(delivery.sent) == 1
    assert delivery.sent[0]["content"] == "error=POLL_TIMEOUT retryable=True"


@pytest.mark.asyncio
async def test_research_poll_extensions_can_be_disabled() -> None:
    from yeoman_gateway.a2a.client import A2APollTimeoutError

    class Delivery:
        async def send(self, **kwargs):
            del kwargs

    class Registry:
        def __init__(self):
            self.calls = 0

        async def poll_task(self, *args, **kwargs):
            del args, kwargs
            self.calls += 1
            raise A2APollTimeoutError("window expired")

    registry = Registry()
    tool = A2ADelegateTool(registry, delivery=Delivery(), research_poll_extensions=0)

    await tool._poll_research(
        "hermes", "task-x", "research.deep", "ctx-x", (), "effect-3", "whatsapp", "chat"
    )

    assert registry.calls == 1


@pytest.mark.asyncio
async def test_research_poll_is_detached_from_closed_turn_and_deleted_after_delivery(
    tmp_path: Path,
) -> None:
    seen_contexts: list[tuple[object, object]] = []

    class Client:
        async def invoke_skill(self, skill, input, *, context_id=None, reference_task_ids=()):
            del input, reference_task_ids
            return A2AWorkerResult(
                "hermes", "research-detached", context_id or "ctx", "TASK_STATE_WORKING", skill
            )

        async def poll_task(self, task_id, *, skill, context_id, reference_task_ids=()):
            del task_id, skill, context_id, reference_task_ids
            return A2AWorkerResult(
                "hermes",
                "research-detached",
                "ctx",
                "TASK_STATE_COMPLETED",
                "research.deep",
                {"report": "done", "sources": []},
            )

    class Delivery:
        async def send(self, **kwargs):
            del kwargs
            seen_contexts.append((current_tool_context(), CURRENT_TURN.get()))

    processing = ProcessingStore(tmp_path / "processing.db")
    store = A2AResearchStore(tmp_path / "research.db")
    tool = A2ADelegateTool(
        A2AWorkerRegistry(
            [A2AWorker(name="hermes", url="http://127.0.0.1:9900")],
            client_factory=lambda _: Client(),
        ),
        store=processing,
        delivery=Delivery(),
        pending_store=store,
    )
    tool.set_context("whatsapp", "chat@g.us")
    token = set_tool_context(ToolInvocationContext(channel="whatsapp", chat_id="chat@g.us"))
    turn_token = CURRENT_TURN.set(object())
    try:
        response = await tool.execute(
            worker="hermes", skill="research.deep", input={"question": "q", "idempotency_key": "r1"}
        )
    finally:
        CURRENT_TURN.reset(turn_token)
        reset_tool_context(token)
    assert "TASK_STATE_WORKING" in response
    while tool._background:
        await asyncio.sleep(0)
    assert seen_contexts == [(None, None)]
    assert store.pending() == ()


@pytest.mark.asyncio
async def test_research_poll_resumes_from_store_after_tool_restart(tmp_path: Path) -> None:
    path = tmp_path / "research.db"
    pending = A2AResearchStore(path)
    pending.put(
        PendingResearch(
            task_id="research-restart",
            worker="hermes",
            skill="research.deep",
            context_id="ctx-restart",
            reference_task_ids=("ref-1",),
            channel="whatsapp",
            chat_id="chat@g.us",
            effect_id="effect-restart",
        )
    )
    calls: list[tuple[str, str, str, tuple[str, ...]]] = []

    class Registry:
        async def poll_task(self, worker, task_id, *, skill, context_id, reference_task_ids=()):
            calls.append((worker, task_id, context_id, tuple(reference_task_ids)))
            return A2AWorkerResult(
                worker,
                task_id,
                context_id,
                "TASK_STATE_COMPLETED",
                skill,
                {"report": "done", "sources": []},
                tuple(reference_task_ids),
            )

    class Delivery:
        def __init__(self):
            self.sent = []

        async def send(self, **kwargs):
            self.sent.append(kwargs)

    delivery = Delivery()
    tool = A2ADelegateTool(Registry(), delivery=delivery, pending_store=A2AResearchStore(path))
    while tool._background:
        await asyncio.sleep(0)
    assert calls == [("hermes", "research-restart", "ctx-restart", ("ref-1",))]
    assert delivery.sent[0]["operation_ref"] == "a2a-result:effect-restart"
    assert A2AResearchStore(path).pending() == ()


@pytest.mark.asyncio
async def test_research_pending_state_survives_an_unknown_delivery_outcome(
    tmp_path: Path,
) -> None:
    from types import SimpleNamespace

    path = tmp_path / "research.db"
    store = A2AResearchStore(path)
    pending = PendingResearch(
        task_id="research-unknown",
        worker="hermes",
        skill="research.deep",
        context_id="ctx-unknown",
        reference_task_ids=(),
        channel="whatsapp",
        chat_id="chat@g.us",
        effect_id="effect-unknown",
    )
    store.put(pending)

    class Registry:
        async def poll_task(self, worker, task_id, **kwargs):
            return A2AWorkerResult(
                worker,
                task_id,
                kwargs["context_id"],
                "TASK_STATE_COMPLETED",
                kwargs["skill"],
                {"report": "done", "sources": []},
            )

    class Delivery:
        async def send(self, **kwargs):
            return SimpleNamespace(state="unknown")

    tool = A2ADelegateTool(Registry(), delivery=Delivery(), pending_store=store)
    while tool._background:
        await asyncio.sleep(0)

    assert store.pending() == (pending,)


def test_workers_are_loopback_only_unless_explicitly_enabled() -> None:
    from yeoman_gateway.a2a.client import A2AWorkerConfigurationError

    with pytest.raises(A2AWorkerConfigurationError, match="loopback"):
        A2AWorker(name="remote", url="https://example.test/a2a")


def test_gateway_runtime_startup_resumes_durable_research() -> None:
    from types import SimpleNamespace

    from yeoman_gateway.app.bootstrap import GatewayRuntime

    calls: list[str] = []
    tool = SimpleNamespace(resume_pending_research=lambda: calls.append("resume"))
    runtime = object.__new__(GatewayRuntime)
    runtime.responder = SimpleNamespace(
        tools=SimpleNamespace(get=lambda name: tool if name == "a2a_delegate" else None)
    )

    runtime._resume_a2a_research()

    assert calls == ["resume"]


def test_router_exposes_gateway_store() -> None:
    from types import SimpleNamespace

    from yeoman_gateway.processing.dispatch import IntentEffectRouter
    from yeoman_shared.config.schema import Config

    store = object()
    assert (
        IntentEffectRouter(
            gateway=SimpleNamespace(store=store),
            config=Config.model_validate({"processing": {"enabled": True}}),
        ).store
        is store
    )


def test_processing_fence_keeps_a2a_delegate_reachable() -> None:
    from yeoman_gateway.agent.tools.registry import ToolRegistry
    from yeoman_gateway.processing.dispatch import (
        NON_MIGRATED_CAPABILITIES,
        disable_non_migrated_tools,
    )

    class Tool:
        def __init__(self, name: str) -> None:
            self.name = name

        def to_schema(self):
            return {"type": "function", "function": {"name": self.name}}

        def validate_params(self, params):
            return []

        async def execute(self, **kwargs):
            return ""

    registry = ToolRegistry()
    for name in ("message", "a2a_delegate", *NON_MIGRATED_CAPABILITIES):
        registry.register(Tool(name))
    assert "a2a_delegate" not in disable_non_migrated_tools(registry)
