"""Task 3: canonical WhatsApp capture is the durable ingest boundary."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from yeoman_gateway.bus.queue import MessageBus
from yeoman_gateway.channels.whatsapp import WhatsAppChannel
from yeoman_gateway.processing.signals import SignalJournalSink, WhatsAppSignalMapper
from yeoman_gateway.processing.store import ProcessingStore
from yeoman_shared.config.schema import WhatsAppConfig
from yeoman_shared.whatsapp_protocol import PROTOCOL_VERSION

CHAT = "chat@g.us"
NOW = 1_700_000_000_000


def _frame(kind: str, payload: dict, *, event_id: str, event_key: str) -> str:
    return json.dumps(
        {
            "version": PROTOCOL_VERSION,
            "type": kind,
            "ts": NOW,
            "accountId": "account-a",
            "eventId": event_id,
            "eventKey": event_key,
            "observedAt": NOW,
            "payload": payload,
        }
    )


def _channel(store: ProcessingStore) -> WhatsAppChannel:
    channel = WhatsAppChannel(WhatsAppConfig(), MessageBus())
    channel.set_processing_signals(SignalJournalSink(store, clock=lambda: NOW))
    return channel


def _drain_inbound(channel: WhatsAppChannel) -> None:
    async def drain() -> None:
        tasks = tuple(channel._inbound_tasks)
        if tasks:
            await asyncio.gather(*tasks)

    asyncio.run(drain())


def test_message_is_committed_before_ack_archive_or_routing(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "processing.db")
    channel = _channel(store)
    order: list[str] = []

    async def ack(command_type: str, payload: dict, timeout_seconds: float, **kwargs):
        del timeout_seconds, kwargs
        assert command_type == "ack_event"
        assert store.get_event("event-1") is not None
        order.append("ack")
        return {"acknowledged": True}

    channel._send_command = ack  # type: ignore[method-assign]
    channel._archive_inbound_event = lambda event: order.append("archive")  # type: ignore[method-assign]

    async def publish(event):
        order.append("route")

    channel._publish_event = publish  # type: ignore[method-assign]

    asyncio.run(
        channel._handle_bridge_message(
            _frame(
                "message",
                {
                    "chatJid": CHAT,
                    "messageId": "message-1",
                    "senderId": "4915@s.whatsapp.net",
                    "text": "hello",
                },
                event_id="event-1",
                event_key="wa:account-a:message-1",
            )
        )
    )
    _drain_inbound(channel)

    assert order == ["ack", "archive", "route"]
    assert store.count_events() == 1
    store.close()


def test_conflicting_replay_fails_closed_without_ack_or_downstream(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "processing.db")
    channel = _channel(store)
    acknowledgements: list[str] = []
    published: list[str] = []

    async def ack(*args, **kwargs):
        del args, kwargs
        acknowledgements.append("ack")
        return {}

    async def publish(event):
        published.append(event.message_id)

    channel._send_command = ack  # type: ignore[method-assign]
    channel._publish_event = publish  # type: ignore[method-assign]

    first = _frame(
        "message",
        {
            "chatJid": CHAT,
            "messageId": "message-2",
            "senderId": "4915@s.whatsapp.net",
            "text": "first",
        },
        event_id="event-2",
        event_key="wa:account-a:message-2",
    )
    asyncio.run(channel._handle_bridge_message(first))
    _drain_inbound(channel)

    conflicting = _frame(
        "message",
        {
            "chatJid": CHAT,
            "messageId": "message-2",
            "senderId": "4915@s.whatsapp.net",
            "text": "changed",
        },
        event_id="event-2",
        event_key="wa:account-a:message-2",
    )
    asyncio.run(channel._handle_bridge_message(conflicting))

    assert acknowledgements == ["ack"]
    assert published == ["message-2"]
    assert store.count_events() == 1
    store.close()


def test_same_replay_is_acknowledged_again_without_second_route(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "processing.db")
    channel = _channel(store)
    acknowledgements: list[dict] = []
    published: list[str] = []

    async def ack(command_type: str, payload: dict, timeout_seconds: float, **kwargs):
        del timeout_seconds, kwargs
        assert command_type == "ack_event"
        acknowledgements.append(payload)
        return {"acknowledged": True}

    async def publish(event):
        published.append(event.message_id)

    channel._send_command = ack  # type: ignore[method-assign]
    channel._publish_event = publish  # type: ignore[method-assign]
    frame = _frame(
        "message",
        {
            "chatJid": CHAT,
            "messageId": "message-replay",
            "senderId": "4915@s.whatsapp.net",
            "text": "same payload",
        },
        event_id="event-replay",
        event_key="wa:account-a:message-replay",
    )

    asyncio.run(channel._handle_bridge_message(frame))
    _drain_inbound(channel)
    asyncio.run(channel._handle_bridge_message(frame))
    _drain_inbound(channel)

    assert acknowledgements == [{"eventId": "event-replay"}] * 2
    assert published == ["message-replay"]
    assert store.count_events() == 1
    store.close()


def test_append_failure_emits_no_ack_and_invokes_no_downstream(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "processing.db")
    channel = _channel(store)
    downstream: list[str] = []

    def fail_append(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("append failed")

    async def ack(*args, **kwargs):
        del args, kwargs
        downstream.append("ack")
        return {}

    channel._send_command = ack  # type: ignore[method-assign]
    channel._archive_inbound_event = lambda event: downstream.append("archive")  # type: ignore[method-assign]
    channel._publish_event = lambda event: downstream.append("route")  # type: ignore[method-assign]
    store.append_event = fail_append  # type: ignore[method-assign]

    asyncio.run(
        channel._handle_bridge_message(
            _frame(
                "message",
                {
                    "chatJid": CHAT,
                    "messageId": "append-failure",
                    "senderId": "4915@s.whatsapp.net",
                    "text": "pending",
                },
                event_id="event-append-failure",
                event_key="wa:append-failure",
            )
        )
    )

    assert downstream == []
    store.close()


def test_ack_failure_keeps_canonical_row_but_invokes_no_downstream(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "processing.db")
    channel = _channel(store)
    downstream: list[str] = []

    async def fail_ack(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("ack failed")

    channel._send_command = fail_ack  # type: ignore[method-assign]
    channel._archive_inbound_event = lambda event: downstream.append("archive")  # type: ignore[method-assign]
    channel._publish_event = lambda event: downstream.append("route")  # type: ignore[method-assign]

    asyncio.run(
        channel._handle_bridge_message(
            _frame(
                "message",
                {
                    "chatJid": CHAT,
                    "messageId": "ack-failure",
                    "senderId": "4915@s.whatsapp.net",
                    "text": "committed but pending",
                },
                event_id="event-ack-failure",
                event_key="wa:ack-failure",
            )
        )
    )

    assert store.get_event("event-ack-failure") is not None
    assert downstream == []
    store.close()


@pytest.mark.parametrize(
    "frame",
    [
        json.dumps(
            {
                "version": PROTOCOL_VERSION,
                "type": "message",
                "accountId": "account-a",
                "eventKey": "wa:missing-id",
                "observedAt": NOW,
                "payload": {"chatJid": CHAT, "messageId": "bad-1", "text": "SECRET"},
            }
        ),
        json.dumps(
            {
                "version": PROTOCOL_VERSION,
                "type": "message",
                "eventId": "event-bad-key",
                "accountId": "account-a",
                "eventKey": "",
                "observedAt": NOW,
                "payload": {"chatJid": CHAT, "messageId": "bad-2", "text": "SECRET"},
            }
        ),
        json.dumps(
            {
                "version": PROTOCOL_VERSION,
                "type": "message",
                "eventId": "event-bad-account",
                "eventKey": "wa:bad-account",
                "accountId": "",
                "observedAt": NOW,
                "payload": {"chatJid": CHAT, "messageId": "bad-3", "text": "SECRET"},
            }
        ),
        json.dumps(
            {
                "version": PROTOCOL_VERSION,
                "type": "message",
                "eventId": "event-bad-observed-at",
                "eventKey": "wa:bad-observed-at",
                "accountId": "account-a",
                "observedAt": 0,
                "payload": {"chatJid": CHAT, "messageId": "bad-4", "text": "SECRET"},
            }
        ),
        json.dumps(
            {
                "version": PROTOCOL_VERSION,
                "type": "message",
                "eventId": "event-huge-observed-at",
                "eventKey": "wa:huge-observed-at",
                "accountId": "account-a",
                "observedAt": 10**1000,
                "payload": {"chatJid": CHAT, "messageId": "bad-5", "text": "SECRET"},
            }
        ),
        json.dumps(
            {
                "version": PROTOCOL_VERSION,
                "type": "message",
                "accountId": "account-a",
                "eventId": "event-bad-payload",
                "eventKey": "wa:bad-payload",
                "observedAt": NOW,
                "payload": ["SECRET"],
            }
        ),
    ],
)
def test_malformed_replayable_frame_fails_closed_without_raw_payload_logging(
    tmp_path: Path, frame: str, monkeypatch
) -> None:
    store = ProcessingStore(tmp_path / "processing.db")
    channel = _channel(store)
    calls: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        "yeoman_gateway.channels.whatsapp.logger.warning",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    channel._send_command = lambda *args, **kwargs: calls.append((args, kwargs))  # type: ignore[method-assign]

    asyncio.run(channel._handle_bridge_message(frame))

    assert store.count_events() == 0
    assert all("SECRET" not in repr(call) for call in calls)
    store.close()


def test_media_only_and_reply_are_one_byte_exact_event_with_relation(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "processing.db")
    sink = SignalJournalSink(store, clock=lambda: NOW)
    text = "  exact leading and trailing bytes \n"

    event_id = sink(
        "message",
        {
            "chatJid": CHAT,
            "messageId": "reply-1",
            "senderId": "4915@s.whatsapp.net",
            "text": text,
            "replyToMessageId": "original-1",
            "replyToText": "quoted only",
            "media": {"kind": "document", "mimeType": "application/pdf", "bytes": 5},
        },
    )

    assert event_id is not None
    assert store.count_events() == 1
    event = store.get_event(event_id)
    assert event is not None
    assert event.payload is not None
    assert event.payload["text"] == text
    assert event.payload["reply_to_message_id"] == "original-1"
    assert event.payload["media"]["bytes"] == 5
    assert [(relation, ref) for relation, ref in event.relations()] == [
        ("source", "reply-1"),
        ("target", "original-1"),
        ("reply_to", "original-1"),
    ]
    store.close()


def test_long_mapper_payload_is_byte_identical_and_media_only_is_valid() -> None:
    text = "\u00e4" * 8_001
    signal = WhatsAppSignalMapper().map(
        {
            "chatJid": CHAT,
            "messageId": "long-1",
            "senderId": "4915@s.whatsapp.net",
            "text": text,
            "media": {"kind": "document", "bytes": 1},
        },
        kind="message",
    )
    assert signal is not None
    assert signal.to_event_payload()["text"] == text


@pytest.mark.parametrize("kind", ["edit", "delete", "reaction", "receipt"])
def test_replayable_signal_frames_are_acknowledged_only_after_capture(
    tmp_path: Path, kind: str
) -> None:
    store = ProcessingStore(tmp_path / f"{kind}.db")
    channel = _channel(store)
    seen: list[str] = []

    async def ack(command_type: str, payload: dict, timeout_seconds: float, **kwargs):
        del payload, timeout_seconds, kwargs
        assert command_type == "ack_event"
        assert store.count_events() == 1
        seen.append("ack")
        return {}

    channel._send_command = ack  # type: ignore[method-assign]
    payloads = {
        "edit": {"chatJid": CHAT, "messageId": "edit-1", "text": "new"},
        "delete": {"chatJid": CHAT, "messageId": "delete-1"},
        "reaction": {
            "chatJid": CHAT,
            "targetMessageId": "message-1",
            "senderId": "4915@s.whatsapp.net",
            "emoji": "👍",
        },
        "receipt": {"chatJid": CHAT, "messageId": "message-1", "status": "read"},
    }
    asyncio.run(
        channel._handle_bridge_message(
            _frame(kind, payloads[kind], event_id=f"event-{kind}", event_key=f"wa:{kind}")
        )
    )
    assert seen == ["ack"]
    assert store.count_events() == 1
    store.close()
