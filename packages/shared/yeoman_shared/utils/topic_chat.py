"""Topic-scoped chat ids.

A chat with topics (Telegram forum groups, or private chats in a bot's threaded mode)
holds several separate conversations. Each topic gets its own chat id, ``<chat>:<topic>``,
so sessions, history, threads and ``/new`` stay per topic. Policy and transport still
address the underlying chat, so they split the id back apart.
"""

from __future__ import annotations

_SEPARATOR = ":"


def topic_chat_id(chat_id: object, topic_id: int | None) -> str:
    """The conversation id for one topic of a chat (the plain chat id without a topic)."""
    base = str(chat_id)
    return f"{base}{_SEPARATOR}{topic_id}" if topic_id is not None else base


def split_topic_chat_id(chat_id: str) -> tuple[str, int | None]:
    """``("<chat>", topic)`` for a topic chat id, ``(chat_id, None)`` otherwise."""
    base, sep, topic = str(chat_id).rpartition(_SEPARATOR)
    if sep and base and topic.isdigit():
        return base, int(topic)
    return str(chat_id), None


__all__ = ["split_topic_chat_id", "topic_chat_id"]
