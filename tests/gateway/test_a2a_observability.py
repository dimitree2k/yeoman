from __future__ import annotations

import stat

import pytest
from yeoman_gateway.a2a.client import A2AWorker, A2AWorkerResult
from yeoman_gateway.a2a.registry import A2AWorkerRegistry
from yeoman_gateway.adapters.responder_llm import (
    _tool_observability_arguments,
    _tool_observability_output,
)
from yeoman_gateway.agent.tools.a2a import A2ADelegateTool
from yeoman_gateway.bus.events import InboundMessage, OutboundMessage
from yeoman_gateway.bus.queue import MessageBus
from yeoman_gateway.cli.gateway_commands import (
    _prepare_private_log_path,
    _should_setup_daemon_logging,
)
from yeoman_gateway.observability import private_log_identifier, safe_log_token


def test_a2a_content_is_redacted_from_generic_observability() -> None:
    arguments = _tool_observability_arguments("a2a_delegate", {"skill": "search.web", "input": {"query": "private"}})
    assert arguments == "[redacted]"
    assert _tool_observability_output("a2a_delegate", "private result") == "[redacted]"


def test_daemon_logging_controls_are_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INVOCATION_ID", "systemd")
    assert _should_setup_daemon_logging() is True
    monkeypatch.delenv("INVOCATION_ID")
    monkeypatch.delenv("Yeoman_GATEWAY_DAEMON", raising=False)
    assert _should_setup_daemon_logging() is False


@pytest.mark.asyncio
async def test_message_bus_boundary_logs_remain_private(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, tuple[object, ...]]] = []
    monkeypatch.setattr("yeoman_gateway.bus.queue.logger.info", lambda message, *args: calls.append((message, args)))
    from datetime import datetime

    bus = MessageBus()
    await bus.publish_inbound(InboundMessage(channel="whatsapp", sender_id="owner", chat_id="owner@s.whatsapp.net", content="secret", timestamp=datetime.now(), metadata={"message_id": "m1"}))
    await bus.publish_outbound(OutboundMessage(channel="whatsapp", chat_id="owner@s.whatsapp.net", content="secret", metadata={"message_id": "m2"}))
    assert "secret" not in repr(calls)


def test_log_identifiers_remain_control_safe() -> None:
    unsafe = "owner@s.whatsapp.net\nforged=record"
    assert safe_log_token(unsafe) == r"owner@s.whatsapp.net\x0aforged=record"
    assert private_log_identifier(unsafe) != unsafe


def test_gateway_log_path_remains_private(tmp_path) -> None:
    path = tmp_path / "logs" / "gateway.log"
    path.parent.mkdir()
    path.write_text("old")
    path.parent.chmod(0o755)
    path.chmod(0o644)
    _prepare_private_log_path(path)
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


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
