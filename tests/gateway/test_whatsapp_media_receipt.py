"""An image send reports the bridge's message id, like a text send does.

Without it, an autonomous meme stayed "unknown" after WhatsApp had accepted it, and the
chat's weekly meme budget never counted it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from yeoman_gateway.bus.events import OutboundMessage
from yeoman_gateway.bus.queue import MessageBus
from yeoman_gateway.channels.whatsapp import WhatsAppChannel
from yeoman_gateway.media.storage import MediaStorage
from yeoman_shared.config.schema import WhatsAppConfig


def _channel(tmp_path: Path, replies: list[dict[str, Any]]) -> tuple[WhatsAppChannel, list]:
    storage = MediaStorage(incoming_dir=tmp_path / "in", outgoing_dir=tmp_path / "out")
    channel = WhatsAppChannel(WhatsAppConfig(), MessageBus(), media_storage=storage)
    channel._connected = True  # noqa: SLF001
    calls: list[tuple[str, dict[str, Any]]] = []

    async def no_typing(_chat_id: str) -> None:
        return None

    async def command(command_type: str, payload: dict[str, Any], **_kwargs: Any):
        calls.append((command_type, payload))
        return replies[len(calls) - 1]

    channel._stop_typing = no_typing  # type: ignore[method-assign]  # noqa: SLF001
    channel._send_command_with_retry = command  # type: ignore[method-assign]  # noqa: SLF001
    return channel, calls


def _image(tmp_path: Path, name: str) -> str:
    path = tmp_path / "out" / "memes" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xd8\xff\xe0jpeg")
    return str(path)


@pytest.mark.asyncio
async def test_a_sent_image_reports_its_provider_message_id(tmp_path: Path) -> None:
    channel, calls = _channel(tmp_path, [{"sent": {"messageId": "3EB0MEME", "bytes": 10}}])
    receipt = await channel._send_scoped(  # noqa: SLF001
        OutboundMessage(
            channel="whatsapp",
            chat_id="group@g.us",
            content="",
            media=[_image(tmp_path, "meme-1.jpg")],
        )
    )
    assert [kind for kind, _ in calls] == ["send_media"]
    assert receipt == {"provider_message_id": "3EB0MEME"}


@pytest.mark.asyncio
async def test_several_images_report_the_first_one(tmp_path: Path) -> None:
    channel, calls = _channel(
        tmp_path,
        [{"sent": {"messageId": "first"}}, {"sent": {"messageId": "second"}}],
    )
    receipt = await channel._send_scoped(  # noqa: SLF001
        OutboundMessage(
            channel="whatsapp",
            chat_id="group@g.us",
            content="",
            media=[_image(tmp_path, "a.jpg"), _image(tmp_path, "b.jpg")],
        )
    )
    assert len(calls) == 2
    assert receipt == {"provider_message_id": "first"}
