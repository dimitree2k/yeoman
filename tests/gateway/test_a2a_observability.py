from __future__ import annotations

import stat
from datetime import datetime

import pytest
from yeoman_gateway.a2a.client import A2AClient, A2AWorker, A2AWorkerResult
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


def test_systemd_gateway_uses_file_logging(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("Yeoman_GATEWAY_DAEMON", raising=False)
    monkeypatch.setenv("INVOCATION_ID", "systemd-invocation")

    assert _should_setup_daemon_logging() is True


def test_daemon_gateway_uses_file_logging(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("Yeoman_GATEWAY_DAEMON", "1")
    monkeypatch.delenv("INVOCATION_ID", raising=False)

    assert _should_setup_daemon_logging() is True


def test_unmanaged_foreground_gateway_keeps_console_logging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("Yeoman_GATEWAY_DAEMON", raising=False)
    monkeypatch.delenv("INVOCATION_ID", raising=False)

    assert _should_setup_daemon_logging() is False


@pytest.mark.asyncio
async def test_message_bus_logs_inbound_and_outbound_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, tuple[object, ...]]] = []

    def capture(message: str, *args: object, **kwargs: object) -> None:
        del kwargs
        calls.append((message, args))

    monkeypatch.setattr("yeoman_gateway.bus.queue.logger.info", capture)
    bus = MessageBus()
    timestamp = datetime.now()

    await bus.publish_inbound(
        InboundMessage(
            channel="whatsapp",
            sender_id="owner",
            chat_id="owner@s.whatsapp.net",
            content="test inbound",
            timestamp=timestamp,
            metadata={"message_id": "wamid-in"},
        )
    )
    await bus.publish_outbound(
        OutboundMessage(
            channel="whatsapp",
            chat_id="owner@s.whatsapp.net",
            content="test outbound",
            metadata={"message_id": "wamid-out"},
        )
    )

    assert [message for message, _ in calls] == [
        "MessageBus inbound channel={} chat={} message_id={} chars={}",
        "MessageBus outbound channel={} chat={} message_id={} chars={}",
    ]
    assert calls[0][1] == (
        "whatsapp",
        private_log_identifier("owner@s.whatsapp.net"),
        private_log_identifier("wamid-in"),
        12,
    )
    assert calls[1][1] == (
        "whatsapp",
        private_log_identifier("owner@s.whatsapp.net"),
        private_log_identifier("wamid-out"),
        13,
    )


@pytest.mark.asyncio
async def test_a2a_delegate_logs_completed_task_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, tuple[object, ...]]] = []

    def capture(message: str, *args: object, **kwargs: object) -> None:
        del kwargs
        calls.append((message, args))

    monkeypatch.setattr("yeoman_gateway.agent.tools.a2a.logger.info", capture)

    class FakeClient:
        async def send_message(
            self,
            message: str,
            *,
            context_id: str | None = None,
        ) -> A2AWorkerResult:
            assert message == "inspect this"
            return A2AWorkerResult(
                worker="hermes",
                task_id="task-observe-1",
                context_id=context_id or "ctx-observe-1",
                state="TASK_STATE_COMPLETED",
                text="done",
            )

    registry = A2AWorkerRegistry(
        [A2AWorker(name="hermes", url="http://127.0.0.1:9900")],
        client_factory=lambda worker: FakeClient(),
    )

    tool = A2ADelegateTool(registry)
    tool.set_context("whatsapp", "owner@s.whatsapp.net")

    result = await tool.execute(
        worker="hermes",
        message="inspect this",
        context_id="ctx-observe-1",
    )

    assert result == "[hermes | TASK_STATE_COMPLETED | task-observe-1]\ndone"
    assert calls[-1] == (
        "A2A delegation completed channel={} chat={} worker={} task_id={} context_id={} state={} result_chars={}",
        (
            "whatsapp",
            private_log_identifier("owner@s.whatsapp.net"),
            "hermes",
            "task-observe-1",
            "ctx-observe-1",
            "TASK_STATE_COMPLETED",
            4,
        ),
    )


def test_a2a_content_is_redacted_from_generic_observability() -> None:
    secret_task = "private task content that must not enter telemetry"
    secret_result = "private worker result that must not enter telemetry"

    arguments = _tool_observability_arguments(
        "a2a_delegate",
        {"worker": "hermes", "message": secret_task},
    )
    output = _tool_observability_output("a2a_delegate", secret_result)

    assert arguments == "[redacted]"
    assert output == "[redacted]"
    assert secret_task not in arguments
    assert secret_result not in output


def test_log_identifiers_are_private_and_control_safe() -> None:
    unsafe = "owner@s.whatsapp.net\nforged=record"

    assert safe_log_token(unsafe) == r"owner@s.whatsapp.net\x0aforged=record"
    assert private_log_identifier(unsafe) != unsafe
    assert "\n" not in safe_log_token(unsafe)


def test_gateway_log_path_is_private(tmp_path) -> None:
    log_path = tmp_path / "logs" / "gateway.log"
    log_path.parent.mkdir()
    log_path.write_text("old", encoding="utf-8")
    log_path.parent.chmod(0o755)
    log_path.chmod(0o644)

    _prepare_private_log_path(log_path)

    assert stat.S_IMODE(log_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(log_path.stat().st_mode) == 0o600


@pytest.mark.asyncio
async def test_a2a_failure_log_excludes_remote_error(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, tuple[object, ...]]] = []

    def capture(message: str, *args: object, **kwargs: object) -> None:
        del kwargs
        calls.append((message, args))

    monkeypatch.setattr("yeoman_gateway.agent.tools.a2a.logger.warning", capture)

    class FailingClient(A2AClient):
        def __init__(self) -> None:
            pass

        async def send_message(
            self,
            message: str,
            *,
            context_id: str | None = None,
        ) -> A2AWorkerResult:
            del message, context_id
            raise RuntimeError("remote secret\nforged log line")

    registry = A2AWorkerRegistry(
        [A2AWorker(name="hermes", url="http://127.0.0.1:9900")],
        client_factory=lambda worker: FailingClient(),
    )
    tool = A2ADelegateTool(registry)

    with pytest.raises(RuntimeError, match="remote secret"):
        await tool.execute(worker="hermes", message="inspect this")

    assert calls[-1] == (
        "A2A delegation failed channel={} chat={} worker={} error_type={}",
        ("", "", "hermes", "RuntimeError"),
    )
    assert "remote secret" not in repr(calls)
    assert "forged log line" not in repr(calls)
