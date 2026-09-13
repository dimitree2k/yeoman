from __future__ import annotations

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


def test_workers_are_loopback_only_unless_explicitly_enabled() -> None:
    from yeoman_gateway.a2a.client import A2AWorkerConfigurationError

    with pytest.raises(A2AWorkerConfigurationError, match="loopback"):
        A2AWorker(name="remote", url="https://example.test/a2a")
