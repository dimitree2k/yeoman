"""Telegram threaded mode: answers land in the topic the message came from."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from yeoman_gateway.bus.events import OutboundMessage
from yeoman_gateway.bus.queue import MessageBus
from yeoman_gateway.channels.telegram import TelegramChannel
from yeoman_shared.config.schema import TelegramConfig


def _channel() -> tuple[TelegramChannel, list[dict]]:
    channel = TelegramChannel(TelegramConfig(), MessageBus())
    sent: list[dict] = []

    async def send_message(**kwargs):
        sent.append(kwargs)
        return SimpleNamespace(message_id=len(sent))

    channel._app = SimpleNamespace(bot=SimpleNamespace(send_message=send_message))  # type: ignore[assignment]
    return channel, sent


def _message(*, thread: int | None, topic: bool, **extra):
    return SimpleNamespace(
        chat_id=453, message_thread_id=thread, is_topic_message=topic, **extra
    )


def test_reply_goes_into_the_topic_of_the_last_message() -> None:
    channel, sent = _channel()
    channel._remember_topic(_message(thread=34029, topic=True))
    asyncio.run(channel.send(OutboundMessage(channel="telegram", chat_id="453", content="hi")))
    assert sent[-1]["message_thread_id"] == 34029


def test_message_in_the_main_chat_clears_the_topic() -> None:
    channel, sent = _channel()
    channel._remember_topic(_message(thread=34029, topic=True))
    channel._remember_topic(_message(thread=None, topic=False))
    asyncio.run(channel.send(OutboundMessage(channel="telegram", chat_id="453", content="hi")))
    assert "message_thread_id" not in sent[-1]


def test_topic_created_without_text_is_logged_and_remembered(caplog) -> None:
    from loguru import logger

    channel, _sent = _channel()
    lines: list[str] = []
    sink = logger.add(lines.append, level="INFO", format="{message}")
    try:
        update = SimpleNamespace(
            update_id=279518461,
            to_dict=lambda: {"update_id": 279518461, "message": {}},
            message=_message(
                thread=34029,
                topic=True,
                forum_topic_created=SimpleNamespace(name="I mean the last"),
                text=None,
                caption=None,
            )
        )
        asyncio.run(channel._on_update(update, None))  # type: ignore[arg-type]
    finally:
        logger.remove(sink)
    assert any("telegram_topic_created" in line and "has_text=False" in line for line in lines)
    assert channel._topic_kwargs("453") == {"message_thread_id": 34029}
