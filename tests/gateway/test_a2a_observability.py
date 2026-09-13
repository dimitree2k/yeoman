from __future__ import annotations

import pytest
from yeoman_gateway.a2a.client import A2AWorker, A2AWorkerResult
from yeoman_gateway.a2a.registry import A2AWorkerRegistry
from yeoman_gateway.adapters.responder_llm import (
    _tool_observability_arguments,
    _tool_observability_output,
)
from yeoman_gateway.agent.tools.a2a import A2ADelegateTool


def test_a2a_content_is_redacted_from_generic_observability() -> None:
    arguments = _tool_observability_arguments("a2a_delegate", {"skill": "search.web", "input": {"query": "private"}})
    assert arguments == "[redacted]"
    assert _tool_observability_output("a2a_delegate", "private result") == "[redacted]"


@pytest.mark.asyncio
async def test_a2a_logs_only_structured_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, tuple[object, ...]]] = []
    monkeypatch.setattr("yeoman_gateway.agent.tools.a2a.logger.info", lambda message, *args: calls.append((message, args)))

    class FakeClient:
        async def invoke_skill(self, skill, input, *, context_id=None, reference_task_ids=()):
            del input, reference_task_ids
            return A2AWorkerResult("hermes", "task-1", context_id or "ctx-1", "TASK_STATE_COMPLETED", skill, {"results": []})

    tool = A2ADelegateTool(A2AWorkerRegistry([A2AWorker(name="hermes", url="http://127.0.0.1:9900")], client_factory=lambda worker: FakeClient()))
    tool.set_context("whatsapp", "owner@s.whatsapp.net")
    await tool.execute(worker="hermes", skill="search.web", input={"query": "secret query"})

    assert "secret query" not in repr(calls)
    assert calls[-1][0].startswith("A2A delegation completed")


@pytest.mark.asyncio
async def test_a2a_failure_log_excludes_structured_input(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, tuple[object, ...]]] = []
    monkeypatch.setattr("yeoman_gateway.agent.tools.a2a.logger.warning", lambda message, *args: calls.append((message, args)))

    class FailingClient:
        async def invoke_skill(self, skill, input, *, context_id=None, reference_task_ids=()):
            del skill, input, context_id, reference_task_ids
            raise RuntimeError("remote secret")

    tool = A2ADelegateTool(A2AWorkerRegistry([A2AWorker(name="hermes", url="http://127.0.0.1:9900")], client_factory=lambda worker: FailingClient()))
    with pytest.raises(RuntimeError, match="remote secret"):
        await tool.execute(worker="hermes", skill="search.web", input={"query": "secret query"})
    assert "secret query" not in repr(calls)
