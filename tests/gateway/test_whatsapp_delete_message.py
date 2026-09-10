from __future__ import annotations

import pytest
from yeoman_gateway.bus.events import OutboundMessage
from yeoman_gateway.channels.whatsapp import WhatsAppChannel


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
