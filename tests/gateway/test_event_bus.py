# tests/gateway/test_event_bus.py
"""Tests for MessageBus event pub/sub."""

import asyncio
import time

import pytest
from yeoman_gateway.bus.events import OverseerCommand, WebhookEvent
from yeoman_gateway.bus.queue import MessageBus


@pytest.mark.asyncio
async def test_publish_and_dispatch_webhook_event() -> None:
    bus = MessageBus(event_maxsize=10)
    received: list[WebhookEvent] = []

    async def handler(ev: WebhookEvent) -> None:
        received.append(ev)

    bus.subscribe_event("WebhookEvent", handler)

    ev = WebhookEvent(
        source="github",
        event_type="push",
        payload={},
        signature_verified=True,
        received_at=time.time(),
    )
    await bus.publish_event(ev)

    dispatch_task = asyncio.create_task(bus.dispatch_events())
    await asyncio.sleep(0.05)
    bus.stop()
    await dispatch_task

    assert len(received) == 1
    assert received[0].source == "github"


@pytest.mark.asyncio
async def test_ipc_queue_not_affected_by_event_overflow() -> None:
    bus = MessageBus(event_maxsize=2)
    ipc_received: list[OverseerCommand] = []

    async def ipc_handler(ev: OverseerCommand) -> None:
        ipc_received.append(ev)

    bus.subscribe_event("OverseerCommand", ipc_handler)

    # Fill the event queue beyond capacity
    for i in range(5):
        await bus.publish_event(
            WebhookEvent(
                source="flood",
                event_type=f"ev{i}",
                payload={},
                signature_verified=True,
                received_at=0.0,
            )
        )

    # IPC command should still go through
    await bus.publish_event(OverseerCommand(command="ping", args={}, correlation_id="test"))

    dispatch_task = asyncio.create_task(bus.dispatch_events())
    await asyncio.sleep(0.05)
    bus.stop()
    await dispatch_task

    assert len(ipc_received) == 1
    assert ipc_received[0].command == "ping"


@pytest.mark.asyncio
async def test_event_dropped_counter() -> None:
    bus = MessageBus(event_maxsize=1)
    await bus.publish_event(
        WebhookEvent(
            source="a", event_type="t", payload={}, signature_verified=True, received_at=0.0
        )
    )
    await bus.publish_event(
        WebhookEvent(
            source="b", event_type="t", payload={}, signature_verified=True, received_at=0.0
        )
    )
    assert bus.event_dropped >= 1
