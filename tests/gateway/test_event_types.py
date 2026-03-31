"""Tests for gateway event types."""

import time

from yeoman_gateway.bus.events import (
    GatewayEvent,
    OverseerCommand,
    SystemEvent,
    WebhookEvent,
)


def test_webhook_event_is_frozen() -> None:
    ev = WebhookEvent(
        source="github",
        event_type="push",
        payload={"ref": "refs/heads/main"},
        signature_verified=True,
        received_at=time.time(),
    )
    assert ev.source == "github"
    assert ev.signature_verified is True
    # Frozen — cannot mutate
    try:
        ev.source = "other"  # type: ignore[misc]
        raise AssertionError("should be frozen")
    except AttributeError:
        pass


def test_overseer_command_fields() -> None:
    cmd = OverseerCommand(
        command="send_message",
        args={"channel": "whatsapp"},
        correlation_id="abc",
    )
    assert cmd.command == "send_message"
    assert cmd.args["channel"] == "whatsapp"


def test_system_event_fields() -> None:
    ev = SystemEvent(
        kind="channel_connected",
        detail={"name": "telegram"},
        timestamp=time.time(),
    )
    assert ev.kind == "channel_connected"


def test_gateway_event_union() -> None:
    ev: GatewayEvent = WebhookEvent(
        source="test",
        event_type="ping",
        payload={},
        signature_verified=False,
        received_at=0.0,
    )
    assert isinstance(ev, WebhookEvent)
