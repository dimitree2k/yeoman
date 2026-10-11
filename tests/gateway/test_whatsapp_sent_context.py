from contextlib import closing
from unittest.mock import AsyncMock

import pytest
from yeoman_gateway.adapters.reply_archive_history import HistoryReplyArchiveAdapter
from yeoman_gateway.bus.events import OutboundMessage
from yeoman_gateway.history.live import HistoryBoundary
from yeoman_gateway.history.project import project
from yeoman_gateway.history.queries import HistoryQueries
from yeoman_gateway.history.reader import HistoryReader

from tests.gateway.test_whatsapp_raw_archive import _FakeWs, _setup


@pytest.mark.parametrize("confirmed", [True, False])
async def test_only_confirmed_sent_text_becomes_reply_context(tmp_path, confirmed):
    channel, raw, store = _setup(tmp_path)
    del channel._send_command
    channel.config.bridge_token = "synthetic-token"
    channel._ws = _FakeWs(channel, {"ok": True, "result": {"sent": {"to": "group@g.us", "messageId": "bot-1"}} if confirmed else {}})
    channel._connected = True
    channel._stop_typing = AsyncMock()
    try:
        await channel.send(OutboundMessage(channel="whatsapp", chat_id="group@g.us", content="On 300k that is 4%.", reply_to="human-1"))
        path = tmp_path / "history.db"
        project([tmp_path / "raw"], path)
        with closing(HistoryReader(path).open_snapshot(HistoryBoundary(1, ()))) as snapshot:
            row = HistoryReplyArchiveAdapter(HistoryQueries(snapshot)).lookup_message("whatsapp", "group@g.us", "bot-1")
            if confirmed:
                assert row is not None
                assert row.text == "On 300k that is 4%."
                assert row.reply_to_message_id == "human-1"
                assert HistoryQueries(snapshot).native_message(chat_id="group@g.us", native_id="bot-1")["direction"] == "out"
            else:
                assert row is None
    finally:
        store.close()
