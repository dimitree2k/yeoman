"""Telegram topics: each topic is its own conversation, replies land in their topic."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from yeoman_gateway.bus.events import OutboundMessage
from yeoman_gateway.bus.queue import MessageBus
from yeoman_gateway.channels.telegram import TelegramChannel
from yeoman_gateway.processing.tool_context import is_group_chat
from yeoman_shared.config.schema import TelegramConfig
from yeoman_shared.utils.topic_chat import split_topic_chat_id, topic_chat_id


def _channel() -> tuple[TelegramChannel, list[dict]]:
    channel = TelegramChannel(TelegramConfig(), MessageBus())
    sent: list[dict] = []

    async def send_message(**kwargs):
        sent.append(kwargs)
        return SimpleNamespace(message_id=len(sent))

    channel._app = SimpleNamespace(bot=SimpleNamespace(send_message=send_message))  # type: ignore[assignment]
    return channel, sent


def _message(*, thread: int | None, topic: bool, chat_id: int = 453, **extra):
    return SimpleNamespace(
        chat_id=chat_id, message_thread_id=thread, is_topic_message=topic, **extra
    )


def test_topic_chat_id_round_trip() -> None:
    assert topic_chat_id(453, 34029) == "453:34029"
    assert topic_chat_id(453, None) == "453"
    assert split_topic_chat_id("453:34029") == ("453", 34029)
    assert split_topic_chat_id("-1001234:7") == ("-1001234", 7)
    assert split_topic_chat_id("453") == ("453", None)
    assert split_topic_chat_id("34596062240904@lid") == ("34596062240904@lid", None)


def test_each_topic_is_its_own_conversation() -> None:
    a = TelegramChannel._conversation_chat_id(_message(thread=11, topic=True))
    b = TelegramChannel._conversation_chat_id(_message(thread=12, topic=True))
    main = TelegramChannel._conversation_chat_id(_message(thread=None, topic=False))
    assert (a, b, main) == ("453:11", "453:12", "453")
    # A reply quoting a message in the main chat carries a thread id but no topic.
    assert TelegramChannel._conversation_chat_id(_message(thread=99, topic=False)) == "453"


def test_reply_goes_into_the_topic_it_answers() -> None:
    channel, sent = _channel()
    asyncio.run(channel.send(OutboundMessage(channel="telegram", chat_id="453:12", content="b")))
    asyncio.run(channel.send(OutboundMessage(channel="telegram", chat_id="453:11", content="a")))
    asyncio.run(channel.send(OutboundMessage(channel="telegram", chat_id="453", content="m")))
    assert [(m["chat_id"], m.get("message_thread_id")) for m in sent] == [
        (453, 12),
        (453, 11),
        (453, None),
    ]


def test_private_topic_stays_a_dm_and_forum_topic_a_group() -> None:
    assert is_group_chat("telegram", "453:12") is False
    assert is_group_chat("telegram", "-1001234:7") is True


def test_topic_follows_the_chat_policy_override(tmp_path) -> None:
    from yeoman_gateway.policy.engine import PolicyEngine
    from yeoman_gateway.policy.schema import PolicyConfig

    policy = PolicyConfig.model_validate(
        {
            "channels": {
                "telegram": {
                    "default": {"whoCanTalk": {"mode": "everyone"}},
                    "chats": {"453": {"whoCanTalk": {"mode": "allowlist", "senders": ["7"]}}},
                }
            }
        }
    )
    engine = PolicyEngine(policy, workspace=tmp_path)
    assert engine.resolve_policy("telegram", "453:12").who_can_talk_mode == "allowlist"
    assert engine.resolve_policy("telegram", "999:12").who_can_talk_mode == "everyone"


def test_topic_created_without_text_is_logged() -> None:
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
            ),
        )
        asyncio.run(channel._on_update(update, None))  # type: ignore[arg-type]
    finally:
        logger.remove(sink)
    assert any("telegram_topic_created" in line and "has_text=False" in line for line in lines)
