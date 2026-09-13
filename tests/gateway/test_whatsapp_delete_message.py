from __future__ import annotations

import asyncio
import json

import pytest
from loguru import logger
from yeoman_gateway.bus.events import OutboundMessage
from yeoman_gateway.channels.whatsapp import WhatsAppChannel
from yeoman_shared.config.schema import WhatsAppConfig


@pytest.mark.asyncio
async def test_whatsapp_channel_routes_delete_without_sending_text() -> None:
    commands: list[tuple[str, dict[str, object]]] = []

    async def stop_typing(chat_id: str) -> None:
        assert chat_id == "12345@g.us"

    async def send_command(
        command_type: str,
        payload: dict[str, object],
        *,
        timeout_seconds: float,
        max_attempts: int,
    ) -> dict[str, object]:
        assert timeout_seconds == 20.0
        assert max_attempts == 3
        commands.append((command_type, payload))
        return {"ok": True}

    channel = object.__new__(WhatsAppChannel)
    channel._connected = True
    channel._stop_typing = stop_typing
    channel._send_command_with_retry = send_command

    await channel.send(
        OutboundMessage(
            channel="whatsapp",
            chat_id="12345@g.us",
            content="must not be sent",
            metadata={"delete_message": {"message_id": "BAE5DELETE"}},
        )
    )

    assert commands == [
        (
            "delete_message",
            {"chatJid": "12345@g.us", "messageId": "BAE5DELETE"},
        )
    ]


@pytest.mark.asyncio
async def test_whatsapp_channel_rejects_malformed_delete_request() -> None:
    channel = object.__new__(WhatsAppChannel)
    channel._connected = True

    async def stop_typing(chat_id: str) -> None:
        del chat_id

    async def fail_if_called(*args: object, **kwargs: object) -> None:
        raise AssertionError("bridge must not be called")

    channel._stop_typing = stop_typing
    channel._send_command_with_retry = fail_if_called

    with pytest.raises(RuntimeError, match="message_id"):
        await channel.send(
            OutboundMessage(
                channel="whatsapp",
                chat_id="12345@s.whatsapp.net",
                content="must not be sent",
                metadata={"delete_message": {}},
            )
        )


@pytest.mark.asyncio
async def test_whatsapp_debug_log_redacts_target_but_keeps_command_summary() -> None:
    sentinel = "120363400000000999@g.us"
    sent_frames: list[dict[str, object]] = []
    channel = object.__new__(WhatsAppChannel)
    channel.config = WhatsAppConfig(bridge_token="test-token")
    channel._pending = {}
    channel._send_lock = asyncio.Lock()

    class _CompletingWebSocket:
        async def send(self, encoded: str) -> None:
            frame = json.loads(encoded)
            sent_frames.append(frame)
            channel._pending[frame["requestId"]].set_result({"ok": True})

    channel._ws = _CompletingWebSocket()
    records: list[str] = []
    sink = logger.add(lambda message: records.append(message.record["message"]), level="DEBUG")
    try:
        result = await channel._send_command(
            "send_text",
            {"to": sentinel, "text": "private text"},
            timeout_seconds=1.0,
        )
    finally:
        logger.remove(sink)

    assert result == {"ok": True}
    assert sent_frames[0]["payload"] == {"to": sentinel, "text": "private text"}
    logged = "\n".join(records)
    assert sentinel not in logged
    assert "type=send_text" in logged
    assert "text_len" in logged
