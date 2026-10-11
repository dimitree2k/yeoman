"""Blocked messages remain durable on the live raw-capture path."""

import asyncio
from contextlib import closing

import pytest
from yeoman_gateway.history.live import HistoryBoundary
from yeoman_gateway.history.project import project
from yeoman_gateway.history.queries import HistoryQueries
from yeoman_gateway.history.reader import HistoryReader

from tests.gateway.test_whatsapp_raw_archive import (
    _frame,
    _handle_with_inline_ack,
    _message,
    _setup,
    _use_inline_bridge_ack,
)


class _DeniedVerdict:
    denied = True
    reason = "blocked_sender"
    decision = None


class _DenyingGate:
    def admit(self, request):
        return _DeniedVerdict()


@pytest.mark.asyncio
async def test_blocked_message_is_still_archived(tmp_path):
    channel, raw, store = _setup(tmp_path)
    channel._processing_gate = _DenyingGate()
    _use_inline_bridge_ack(channel)
    channel._reader_task = asyncio.current_task()
    channel._events_subscribed = True
    try:
        await _handle_with_inline_ack(channel, _frame(_message(messageId="m-blocked-1", text="this sender is blocked")))
        raw.drain_spool()
        path = tmp_path / "history.db"
        project([tmp_path / "raw"], path)
        with closing(HistoryReader(path).open_snapshot(HistoryBoundary(1, ()))) as snapshot:
            row = HistoryQueries(snapshot).native_message(chat_id="chat@g.us", native_id="m-blocked-1")
            assert row is not None
            assert row["current_text"] == "this sender is blocked"
    finally:
        channel._reader_task = None
        store.close()
