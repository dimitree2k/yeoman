"""Tests for smart context windowing."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from yeoman_gateway.core.models import InboundEvent
from yeoman_gateway.pipeline.reply_context import ReplyContextMiddleware


def _make_event(
    chat_id: str = "owner@s.whatsapp.net",
    message_id: str = "msg-1",
    content: str = "hello",
    channel: str = "whatsapp",
    is_group: bool = False,
) -> InboundEvent:
    return InboundEvent(
        channel=channel,
        chat_id=chat_id,
        sender_id="owner@s.whatsapp.net",
        content=content,
        message_id=message_id,
        is_group=is_group,
        raw_metadata={},
    )


class TestAmbientWindowSkipDM:
    """Ambient window should be empty for DM chats, populated for groups."""

    def test_dm_returns_empty_ambient(self):
        archive = MagicMock()
        archive.lookup_messages_before = MagicMock(return_value=[])
        mw = ReplyContextMiddleware(archive=archive, ambient_window_limit=8)

        event = _make_event(chat_id="owner@s.whatsapp.net", is_group=False)
        result = mw._build_ambient_window(event)

        assert result == []
        archive.lookup_messages_before.assert_not_called()

    def test_group_calls_archive(self):
        archive = MagicMock()
        archive.lookup_messages_before = MagicMock(return_value=[])
        mw = ReplyContextMiddleware(archive=archive, ambient_window_limit=8)

        event = _make_event(chat_id="123456@g.us", is_group=True)
        mw._build_ambient_window(event)

        archive.lookup_messages_before.assert_called_once()
