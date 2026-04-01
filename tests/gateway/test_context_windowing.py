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


from yeoman_gateway.session.manager import Session


class TestSessionBoundary:
    """Session.get_history() should stop at the most recent session_boundary."""

    def test_no_boundary_returns_all(self):
        s = Session(key="test")
        s.add_message("user", "msg1")
        s.add_message("assistant", "reply1")
        s.add_message("user", "msg2")
        s.add_message("assistant", "reply2")
        history = s.get_history(max_messages=50)
        assert len(history) == 4

    def test_boundary_limits_history(self):
        s = Session(key="test")
        s.add_message("user", "old message")
        s.add_message("assistant", "old reply")
        s.add_boundary()
        s.add_message("user", "new message")
        s.add_message("assistant", "new reply")
        history = s.get_history(max_messages=50)
        assert len(history) == 2
        assert history[0]["content"] == "new message"
        assert history[1]["content"] == "new reply"

    def test_multiple_boundaries_uses_latest(self):
        s = Session(key="test")
        s.add_message("user", "ancient")
        s.add_boundary()
        s.add_message("user", "old")
        s.add_boundary()
        s.add_message("user", "recent")
        history = s.get_history(max_messages=50)
        assert len(history) == 1
        assert history[0]["content"] == "recent"

    def test_boundary_with_max_messages_uses_smaller(self):
        s = Session(key="test")
        s.add_boundary()
        for i in range(20):
            s.add_message("user", f"msg {i}")
        history = s.get_history(max_messages=5)
        assert len(history) == 5

    def test_boundary_at_end_returns_empty(self):
        s = Session(key="test")
        s.add_message("user", "hello")
        s.add_boundary()
        history = s.get_history(max_messages=50)
        assert len(history) == 0
