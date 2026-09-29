from unittest.mock import AsyncMock

import pytest
from yeoman_gateway.bus.events import OutboundMessage
from yeoman_gateway.bus.queue import MessageBus
from yeoman_gateway.channels.whatsapp import WhatsAppChannel
from yeoman_gateway.storage.inbound_archive import InboundArchive
from yeoman_shared.config.schema import WhatsAppConfig


@pytest.mark.parametrize("confirmed", [True, False])
async def test_only_confirmed_sent_text_becomes_reply_context(tmp_path, confirmed):
    archive = InboundArchive(tmp_path / "archive.db")
    try:
        channel = WhatsAppChannel(WhatsAppConfig(), MessageBus(), inbound_archive=archive)
        channel._connected = True
        channel._stop_typing = AsyncMock()
        channel._send_command_with_retry = AsyncMock(
            return_value={"sent": {"messageId": "bot-1"}} if confirmed else {}
        )
        await channel.send(OutboundMessage(
            channel="whatsapp", chat_id="group@g.us", content="On 300k that is 4%.",
            reply_to="human-1",
        ))
        row = archive.lookup_message("whatsapp", "group@g.us", "bot-1")
        if confirmed:
            assert row is not None
            assert row["text"] == "On 300k that is 4%."
            assert row["sender_id"] == "assistant"
            assert row["reply_to_message_id"] == "human-1"
        else:
            assert row is None
    finally:
        archive.close()
