"""Bridge status frames carry the WhatsApp close code; the gateway classifies it."""

from __future__ import annotations

import asyncio
import json

from yeoman_gateway.bus.queue import MessageBus
from yeoman_gateway.channels.whatsapp import WhatsAppChannel
from yeoman_shared.config.schema import WhatsAppConfig
from yeoman_shared.whatsapp_protocol import PROTOCOL_VERSION


def _status(payload: dict) -> str:
    return json.dumps(
        {
            "version": PROTOCOL_VERSION,
            "type": "status",
            "ts": 1,
            "accountId": "default",
            "payload": payload,
        }
    )


def test_disconnect_code_is_classified() -> None:
    channel = WhatsAppChannel(WhatsAppConfig(), MessageBus())
    asyncio.run(
        channel._handle_bridge_message(_status({"status": "disconnected", "statusCode": 401}))
    )
    assert channel.last_disconnect == {"code": 401, "reason": "logged_out", "class": "needs_human"}


def test_reconnecting_uses_last_disconnect_status() -> None:
    channel = WhatsAppChannel(WhatsAppConfig(), MessageBus())
    asyncio.run(
        channel._handle_bridge_message(
            _status({"status": "reconnecting", "reconnectAttempts": 2, "lastDisconnectStatus": 408})
        )
    )
    assert channel.last_disconnect == {"code": 408, "reason": "timed_out", "class": "transient"}


def test_connected_status_leaves_last_disconnect_alone() -> None:
    channel = WhatsAppChannel(WhatsAppConfig(), MessageBus())
    asyncio.run(channel._handle_bridge_message(_status({"status": "connected"})))
    assert channel.last_disconnect is None
