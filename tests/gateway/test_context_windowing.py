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


class TestPreflightHeuristic:
    """Preflight heuristic should detect backward references in messages."""

    def test_detects_earlier_reference(self):
        from yeoman_gateway.adapters.responder_llm import _has_backward_reference

        assert _has_backward_reference("as we discussed earlier, the plan was...")
        assert _has_backward_reference("you mentioned something about auth")
        assert _has_backward_reference("remember when we talked about the API?")
        assert _has_backward_reference("go back to what you said about config")
        assert _has_backward_reference("what about the idea from before?")

    def test_ignores_normal_messages(self):
        from yeoman_gateway.adapters.responder_llm import _has_backward_reference

        assert not _has_backward_reference("hello")
        assert not _has_backward_reference("what's the weather?")
        assert not _has_backward_reference("please write a function that adds two numbers")
        assert not _has_backward_reference("can you help me?")


import pytest
from yeoman_gateway.agent.tools.recall_conversation import RecallConversationTool
from yeoman_gateway.session.manager import SessionManager


class TestRecallConversationTool:
    """recall_conversation tool should search session history."""

    @pytest.fixture
    def session_manager(self, tmp_path):
        return SessionManager(workspace=tmp_path, sessions_dir=tmp_path / "sessions")

    @pytest.fixture
    def tool(self, session_manager):
        t = RecallConversationTool(session_manager=session_manager)
        t.set_context("whatsapp", "owner@s.whatsapp.net")
        return t

    @pytest.mark.asyncio
    async def test_finds_matching_messages(self, tool, session_manager):
        session = session_manager.get_or_create("whatsapp:owner@s.whatsapp.net")
        session.add_message("user", "let's use PostgreSQL for the database")
        session.add_message("assistant", "sure, PostgreSQL is a good choice")
        session.add_message("user", "what about Redis for caching?")
        session_manager.save(session)

        result = await tool.execute(query="PostgreSQL")
        assert "PostgreSQL" in result
        assert "Redis" not in result

    @pytest.mark.asyncio
    async def test_returns_no_matches(self, tool, session_manager):
        session = session_manager.get_or_create("whatsapp:owner@s.whatsapp.net")
        session.add_message("user", "hello world")
        session_manager.save(session)

        result = await tool.execute(query="kubernetes")
        assert "No matching" in result or "no match" in result.lower()

    @pytest.mark.asyncio
    async def test_searches_across_boundaries(self, tool, session_manager):
        session = session_manager.get_or_create("whatsapp:owner@s.whatsapp.net")
        session.add_message("user", "use PostgreSQL")
        session.add_boundary()
        session.add_message("user", "hello")
        session_manager.save(session)

        result = await tool.execute(query="PostgreSQL")
        assert "PostgreSQL" in result

    @pytest.mark.asyncio
    async def test_respects_max_messages(self, tool, session_manager):
        session = session_manager.get_or_create("whatsapp:owner@s.whatsapp.net")
        for i in range(50):
            session.add_message("user", f"message about topic {i}")
        session_manager.save(session)

        result = await tool.execute(query="topic", max_messages=5)
        lines = [l for l in result.strip().split("\n") if l.strip() and not l.startswith("Found")]
        assert len(lines) <= 5
