"""Integration test: webhook -> event bus -> handler."""

import asyncio
import time

import pytest
from yeoman_gateway.bus.events import WebhookEvent
from yeoman_gateway.bus.queue import MessageBus


@pytest.mark.asyncio
async def test_webhook_event_through_bus() -> None:
    """Simulate: webhook publishes event -> bus dispatches -> handler receives."""
    bus = MessageBus(event_maxsize=10)
    received: list[WebhookEvent] = []

    async def webhook_handler(ev: WebhookEvent) -> None:
        received.append(ev)

    bus.subscribe_event("WebhookEvent", webhook_handler)

    # Simulate webhook publishing an event
    event = WebhookEvent(
        source="github",
        event_type="push",
        payload={
            "repository": {"full_name": "user/repo"},
            "ref": "refs/heads/main",
            "commits": [{}],
        },
        signature_verified=True,
        received_at=time.time(),
    )
    await bus.publish_event(event)

    # Start dispatch loop briefly
    dispatch = asyncio.create_task(bus.dispatch_events())
    await asyncio.sleep(0.1)
    bus.stop()
    await dispatch

    assert len(received) == 1
    assert received[0].source == "github"
    assert received[0].event_type == "push"
