from __future__ import annotations

import asyncio
from collections import deque
from datetime import UTC, datetime
from typing import Any

import pytest
from yeoman_gateway.app.bootstrap import OrchestratorService
from yeoman_gateway.bus.events import InboundMessage, OutboundMessage
from yeoman_gateway.core.intents import SendOutboundIntent
from yeoman_gateway.core.models import OutboundEvent
from yeoman_shared.telemetry import InMemoryTelemetry


class _Bus:
    def __init__(self, messages: list[InboundMessage], *, fail_on_outbound: int | None = None) -> None:
        self._inbound = deque(messages)
        self._fail_on_outbound = fail_on_outbound
        self._outbound_calls = 0
        self.outbound: list[OutboundMessage] = []
        self.reactions: list[Any] = []

    async def consume_inbound(self) -> InboundMessage:
        if not self._inbound:
            # Review F03: ingest no longer waits for a running generation, so the loop may
            # ask again before the scripted orchestrator has stopped the service. A real
            # bus simply has nothing to say; the service's own timeout ends the loop.
            await asyncio.sleep(30)
            raise AssertionError("unreachable: the service times out first")  # pragma: no cover
        return self._inbound.popleft()

    async def publish_outbound(self, message: OutboundMessage) -> None:
        self._outbound_calls += 1
        if self._outbound_calls == self._fail_on_outbound:
            raise RuntimeError("dispatch failed after one side effect")
        self.outbound.append(message)

    async def publish_reaction(self, message: Any) -> None:
        self.reactions.append(message)


class _Memory:
    pass


class _RaiseThenReply:
    def __init__(self) -> None:
        self.service: OrchestratorService | None = None
        self.calls: list[str] = []

    async def handle(self, event: Any) -> list[SendOutboundIntent]:
        self.calls.append(event.content)
        if len(self.calls) == 1:
            raise ValueError("secret=synthetic foreign-chat@g.us")
        assert self.service is not None
        self.service.stop()
        return [
            SendOutboundIntent(
                event=OutboundEvent(
                    channel=event.channel,
                    chat_id=event.chat_id,
                    content="normal follow-up",
                )
            )
        ]


class _PartialDispatch:
    def __init__(self) -> None:
        self.service: OrchestratorService | None = None

    async def handle(self, event: Any) -> list[SendOutboundIntent]:
        assert self.service is not None
        self.service.stop()
        return [
            SendOutboundIntent(
                event=OutboundEvent(
                    channel=event.channel,
                    chat_id=event.chat_id,
                    content="first intent",
                )
            ),
            SendOutboundIntent(
                event=OutboundEvent(
                    channel=event.channel,
                    chat_id=event.chat_id,
                    content="second intent",
                )
            ),
        ]


def _message(content: str) -> InboundMessage:
    return InboundMessage(
        channel="whatsapp",
        chat_id="chat@g.us",
        sender_id="sender@s.whatsapp.net",
        content=content,
        timestamp=datetime.now(UTC),
        metadata={"message_id": content},
    )


@pytest.mark.asyncio
async def test_orchestrator_error_is_silent_and_next_message_is_processed() -> None:
    bus = _Bus([_message("broken"), _message("works")])
    orchestrator = _RaiseThenReply()
    service = OrchestratorService(
        bus=bus,
        orchestrator=orchestrator,
        typing_adapter=lambda channel, chat_id, enabled: None,
        telemetry=InMemoryTelemetry(),
        memory=_Memory(),
    )
    orchestrator.service = service

    await service.run()

    assert orchestrator.calls == ["broken", "works"]
    assert [message.content for message in bus.outbound] == ["normal follow-up"]
    assert not bus.reactions


@pytest.mark.asyncio
async def test_partial_dispatch_does_not_replay_or_publish_error_text() -> None:
    bus = _Bus([_message("partial")], fail_on_outbound=2)
    orchestrator = _PartialDispatch()
    service = OrchestratorService(
        bus=bus,
        orchestrator=orchestrator,
        typing_adapter=lambda channel, chat_id, enabled: None,
        telemetry=InMemoryTelemetry(),
        memory=_Memory(),
    )
    orchestrator.service = service

    await service.run()

    assert [message.content for message in bus.outbound] == ["first intent"]
    assert all(not message.content.startswith("Sorry, I encountered an error:") for message in bus.outbound)


@pytest.mark.asyncio
async def test_f03_a_second_message_is_ingested_during_a_running_generation() -> None:
    """Review F03: the loop used to wait for pipeline *and* dispatch before reading on.

    A follow-up could therefore never reach the running thread's postbox - the branch that
    handles it was unreachable in the real bus path.
    """
    first_running = asyncio.Event()
    second_ingested = asyncio.Event()
    release = asyncio.Event()
    handled: list[str] = []

    class _Orchestrator:
        service: OrchestratorService | None = None

        async def handle(self, event: Any) -> list[Any]:
            handled.append(event.content)
            if event.content == "first":
                first_running.set()
                await release.wait()  # a generation that takes its time
                self.service.stop()  # type: ignore[union-attr]
            else:
                second_ingested.set()
                self.service.stop()  # type: ignore[union-attr]
            return []

    bus = _Bus([_message("first"), _message("second")])
    service = OrchestratorService(
        bus=bus,
        orchestrator=_Orchestrator(),
        typing_adapter=lambda channel, chat_id, enabled: None,
        telemetry=InMemoryTelemetry(),
        memory=_Memory(),
    )
    orchestrator = service._orchestrator
    orchestrator.service = service  # type: ignore[attr-defined]

    task = asyncio.create_task(service.run())
    await asyncio.wait_for(first_running.wait(), timeout=2)

    # The proof: the second message is already being handled while the first is blocked.
    await asyncio.wait_for(second_ingested.wait(), timeout=2)
    assert handled == ["first", "second"]

    release.set()
    await asyncio.wait_for(task, timeout=10)
