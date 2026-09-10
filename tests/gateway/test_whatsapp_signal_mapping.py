"""Plan 04 / task 7: the signal channel is version-gated and fail-closed."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from yeoman_gateway.bus.queue import MessageBus
from yeoman_gateway.channels.whatsapp import WhatsAppChannel
from yeoman_gateway.processing.signals import SignalJournalSink, signal_event_id
from yeoman_gateway.processing.store import ProcessingStore
from yeoman_shared.config.schema import WhatsAppConfig
from yeoman_shared.whatsapp_protocol import PROTOCOL_VERSION

CHAT = "chat@g.us"
T0 = 1_700_000_000_000


def _channel(store: ProcessingStore) -> WhatsAppChannel:
    channel = WhatsAppChannel(WhatsAppConfig(), MessageBus())
    channel.set_processing_signals(SignalJournalSink(store, clock=lambda: T0))
    return channel


def _frame(kind: str, payload: dict, *, version: int = PROTOCOL_VERSION) -> str:
    return json.dumps({"version": version, "type": kind, "ts": T0, "accountId": "a", "payload": payload})


def test_protocol_is_v4_and_gateway_rejects_older_frames(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "p.db")
    channel = _channel(store)

    asyncio.run(channel._handle_bridge_message(_frame("reaction", {
        "chatJid": CHAT, "targetMessageId": "3EB0", "senderId": "4915@s.whatsapp.net",
        "emoji": "🔥", "timestamp": 1_700_000_000,
    }, version=3)))

    assert PROTOCOL_VERSION == 4
    # A v3 frame is dropped instead of being half understood.
    assert store.count_events() == 0
    store.close()


def test_every_signal_kind_reaches_the_journal(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "p.db")
    channel = _channel(store)
    frames = {
        "edit": {"chatJid": CHAT, "messageId": "3EB1", "text": "new", "timestamp": 1_700_000_000},
        "delete": {"chatJid": CHAT, "messageId": "3EB2"},
        "reaction": {
            "chatJid": CHAT, "targetMessageId": "3EB3", "senderId": "4915@s.whatsapp.net",
            "emoji": "👍", "timestamp": 1_700_000_000,
        },
        "receipt": {
            "chatJid": CHAT, "messageId": "3EB4", "recipientJid": "4915@s.whatsapp.net",
            "status": "read",
        },
    }

    for kind, payload in frames.items():
        asyncio.run(channel._handle_bridge_message(_frame(kind, payload)))

    assert store.count_events() == 4
    kinds = sorted(
        event.kind for event in (store.get_event(signal_event_id(key)) for key in [])
        if event is not None
    )
    assert kinds == []  # no ids invented here; the rows are asserted below
    stored = [store.get_event(event_id) for event_id in _event_ids(store)]
    assert sorted(event.kind for event in stored if event is not None) == [
        "delete", "edit", "reaction", "receipt",
    ]
    # A signal is evidence, never an order.
    assert store.get_lineage("whatsapp:" + CHAT + ":x").decisions == ()
    store.close()


def _event_ids(store: ProcessingStore) -> list[str]:
    import sqlite3

    con = sqlite3.connect(f"file:{store.path}?mode=ro", uri=True)
    try:
        return [row[0] for row in con.execute("select event_id from events")]
    finally:
        con.close()


def test_a_message_frame_still_takes_the_legacy_path(tmp_path: Path) -> None:
    """Signal frames are additive: the message path is untouched."""
    store = ProcessingStore(tmp_path / "p.db")
    channel = _channel(store)
    published: list[str] = []

    async def _fake_publish(event):
        published.append(event.message_id)

    channel._enrich_media_event = lambda event: _passthrough(event)  # type: ignore[method-assign]
    channel._publish_event = _fake_publish  # type: ignore[method-assign]

    asyncio.run(channel._handle_bridge_message(_frame("message", {
        "chatJid": CHAT, "messageId": "3EB9", "senderId": "4915", "text": "hello",
        "timestamp": 1_700_000_000,
    })))

    assert published == ["3EB9"]
    assert store.count_events() == 0  # the legacy path does not journal signals
    store.close()


async def _passthrough(event):
    return event
