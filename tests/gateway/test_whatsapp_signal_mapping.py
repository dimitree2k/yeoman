"""Plan 04 / task 7: the signal channel is version-gated and fail-closed."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from yeoman_gateway.bus.queue import MessageBus
from yeoman_gateway.channels.whatsapp import WhatsAppChannel
from yeoman_gateway.processing.signals import (
    SignalJournalSink,
    WhatsAppSignalMapper,
    signal_event_id,
)
from yeoman_gateway.processing.store import ProcessingStore
from yeoman_shared.config.schema import WhatsAppConfig
from yeoman_shared.whatsapp_protocol import (
    MAX_BRIDGE_FRAME_BYTES,
    MEDIA_METADATA_FIELDS,
    PROTOCOL_VERSION,
    REPLAYABLE_EVENT_TYPES,
)

CHAT = "chat@g.us"
T0 = 1_700_000_000_000


def _channel(store: ProcessingStore) -> WhatsAppChannel:
    channel = WhatsAppChannel(WhatsAppConfig(), MessageBus())
    channel.set_processing_signals(SignalJournalSink(store, clock=lambda: T0))
    return channel


def _frame(kind: str, payload: dict, *, version: int = PROTOCOL_VERSION) -> str:
    return json.dumps({"version": version, "type": kind, "ts": T0, "accountId": "a", "payload": payload})


def test_protocol_is_v5_and_gateway_rejects_older_frames(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "p.db")
    channel = _channel(store)

    asyncio.run(channel._handle_bridge_message(_frame("reaction", {
        "chatJid": CHAT, "targetMessageId": "3EB0", "senderId": "4915@s.whatsapp.net",
        "emoji": "🔥", "timestamp": 1_700_000_000,
    }, version=3)))

    assert PROTOCOL_VERSION == 5
    # A v3 frame is dropped instead of being half understood.
    assert store.count_events() == 0
    store.close()


def test_v5_payload_constants_match_supported_event_and_media_shapes() -> None:
    assert REPLAYABLE_EVENT_TYPES == frozenset({"message", "edit", "delete", "reaction", "receipt"})
    assert MAX_BRIDGE_FRAME_BYTES == 262_144
    assert MEDIA_METADATA_FIELDS == frozenset(
        {"kind", "mimeType", "fileName", "bytes", "path", "ref", "sha256", "hash"}
    )


def test_long_message_and_media_metadata_are_preserved_without_binary_payload() -> None:
    text = "x" * 8_001
    signal = WhatsAppSignalMapper().map(
        {
            "chatJid": CHAT,
            "messageId": "long-media-1",
            "senderId": "4915@s.whatsapp.net",
            "text": text,
            "media": {
                "kind": "document",
                "mimeType": "application/pdf",
                "fileName": "report.pdf",
                "bytes": 128,
                "path": "/safe/media/report.pdf",
                "sha256": "a" * 64,
                "data": b"must-not-cross-wire",
            },
        },
        kind="message",
    )

    assert signal is not None
    body = signal.to_event_payload()
    assert body["text"] == text
    assert body["media"] == {
        "kind": "document",
        "mimeType": "application/pdf",
        "fileName": "report.pdf",
        "bytes": 128,
        "path": "/safe/media/report.pdf",
        "sha256": "a" * 64,
    }
    assert "data" not in body["media"]


def test_media_hash_mapping_keeps_only_valid_sha256_and_uses_first_valid_candidate() -> None:
    signal = WhatsAppSignalMapper().map(
        {
            "chatJid": CHAT,
            "messageId": "hash-1",
            "text": "[Document]",
            "media": {
                "kind": "document",
                "sha256": "invalid",
                "hash": "AB" * 32,
                "fileSha256": b"raw-provider-bytes",
            },
        },
        kind="message",
    )

    assert signal is not None
    assert signal.to_event_payload()["media"]["sha256"] == "ab" * 32


def test_edit_preserves_replacement_text_and_revision() -> None:
    signal = WhatsAppSignalMapper().map(
        {
            "chatJid": CHAT,
            "messageId": "edited-1",
            "text": "replacement text",
            "revision": 3,
            "timestamp": 1_700_000_123,
        },
        kind="edit",
    )

    assert signal is not None
    body = signal.to_event_payload()
    assert body["text"] == "replacement text"
    assert body["revision"] == 3


def test_delete_reaction_and_receipt_keep_provider_references() -> None:
    mapper = WhatsAppSignalMapper()
    delete = mapper.map({"chatJid": CHAT, "messageId": "deleted-1"}, kind="delete")
    reaction = mapper.map(
        {
            "chatJid": CHAT,
            "targetMessageId": "reacted-1",
            "senderId": "4915@s.whatsapp.net",
            "emoji": "👍",
        },
        kind="reaction",
    )
    receipt = mapper.map(
        {
            "chatJid": CHAT,
            "messageId": "received-1",
            "recipientJid": "4915@s.whatsapp.net",
            "status": "read",
        },
        kind="receipt",
    )

    assert delete is not None and delete.source_message_id == "deleted-1"
    assert reaction is not None and reaction.target_message_id == "reacted-1"
    assert receipt is not None and receipt.source_message_id == "received-1"
    assert delete.to_event_payload()["source_message_id"] == "deleted-1"
    assert reaction.to_event_payload()["target_message_id"] == "reacted-1"
    assert receipt.to_event_payload()["source_message_id"] == "received-1"


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
