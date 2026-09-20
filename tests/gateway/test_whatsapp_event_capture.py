"""Task 3: canonical WhatsApp capture is the durable ingest boundary."""

from __future__ import annotations

import asyncio
import json
import time
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


@pytest.mark.parametrize("field", ["eventId", "eventKey", "accountId"])
@pytest.mark.parametrize("value", [" padded", "padded ", "bad\nvalue", "bad\x00value"])
def test_root_identity_rejects_padding_and_control_characters(
    tmp_path: Path, field: str, value: str
) -> None:
    store = ProcessingStore(tmp_path / "processing.db")
    channel = _channel(store)
    frame = json.loads(
        _frame(
            "message",
            {
                "chatJid": CHAT,
                "messageId": "identity-invalid",
                "senderId": "4915@s.whatsapp.net",
                "text": "must stay pending",
            },
            event_id="event-identity",
            event_key="wa:identity",
        )
    )
    frame[field] = value

    asyncio.run(channel._handle_bridge_message(json.dumps(frame)))

    assert store.count_events() == 0
    store.close()


def test_capture_keeps_event_loop_responsive_during_slow_sqlite_write(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "processing.db")
    channel = _channel(store)
    original_append = store.append_event

    def slow_append(*args, **kwargs):
        time.sleep(0.15)
        return original_append(*args, **kwargs)

    store.append_event = slow_append  # type: ignore[method-assign]
    channel._send_command = _ack_immediately  # type: ignore[method-assign]

    async def exercise() -> float:
        started = asyncio.get_running_loop().time()
        handler = asyncio.create_task(
            channel._handle_bridge_message(
                _frame(
                    "message",
                    {
                        "chatJid": CHAT,
                        "messageId": "slow-sqlite",
                        "senderId": "4915@s.whatsapp.net",
                        "text": "slow write",
                    },
                    event_id="event-slow-sqlite",
                    event_key="wa:slow-sqlite",
                )
            )
        )
        await asyncio.sleep(0.02)
        elapsed = asyncio.get_running_loop().time() - started
        await handler
        await channel.stop()
        return elapsed

    elapsed = asyncio.run(exercise())

    assert elapsed < 0.12
    store.close()


async def _ack_immediately(*args, **kwargs):
    del args, kwargs
    return {"acknowledged": True}


def test_concurrent_duplicate_event_id_coalesces_ack_and_projection(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "processing.db")
    channel = _channel(store)
    acknowledgements: list[str] = []
    published: list[str] = []

    async def ack(*args, **kwargs):
        del args, kwargs
        acknowledgements.append("ack")
        await asyncio.sleep(0.02)
        return {"acknowledged": True}

    async def publish(event):
        published.append(event.message_id)

    channel._send_command = ack  # type: ignore[method-assign]
    channel._publish_event = publish  # type: ignore[method-assign]
    frame = _frame(
        "message",
        {
            "chatJid": CHAT,
            "messageId": "concurrent-duplicate",
            "senderId": "4915@s.whatsapp.net",
            "text": "one projection",
        },
        event_id="event-concurrent-duplicate",
        event_key="wa:concurrent-duplicate",
    )

    async def exercise() -> None:
        await asyncio.gather(
            channel._handle_bridge_message(frame),
            channel._handle_bridge_message(frame),
        )
        await channel.stop()

    asyncio.run(exercise())

    assert acknowledgements == ["ack"]
    assert published == ["concurrent-duplicate"]
    store.close()


def test_concurrent_same_id_with_different_kind_is_rejected(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "processing.db")
    channel = _channel(store)
    rejected: list[tuple[str, str]] = []
    channel._reject_replayable_frame = lambda kind, reason: rejected.append((str(kind), reason))  # type: ignore[method-assign]

    async def ack(*args, **kwargs):
        del args, kwargs
        await asyncio.sleep(0.02)
        return {"acknowledged": True}

    channel._send_command = ack  # type: ignore[method-assign]
    payload = {
        "chatJid": CHAT,
        "messageId": "same-root-id",
        "senderId": "4915@s.whatsapp.net",
        "text": "same payload, different kind",
    }
    message = _frame(
        "message", payload, event_id="event-kind-conflict", event_key="wa:kind-conflict"
    )
    delete = _frame(
        "delete", payload, event_id="event-kind-conflict", event_key="wa:kind-conflict"
    )

    async def exercise() -> None:
        await asyncio.gather(
            channel._handle_bridge_message(message),
            channel._handle_bridge_message(delete),
        )
        await channel.stop()

    asyncio.run(exercise())

    assert ("delete", "conflicting_event_id") in rejected
    store.close()


def test_failed_projection_is_retried_on_the_same_replay(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "processing.db")
    channel = _channel(store)
    sink = channel._processing_signals
    calls: list[str] = []

    def invalidate(kind, payload):
        del payload
        calls.append(kind)
        if len(calls) == 1:
            raise RuntimeError("projection failed")

    sink.invalidate = invalidate

    async def ack(*args, **kwargs):
        del args, kwargs
        return {"acknowledged": True}

    channel._send_command = ack  # type: ignore[method-assign]
    frame = _frame(
        "delete",
        {"chatJid": CHAT, "messageId": "retry-projection", "senderId": "4915"},
        event_id="event-retry-projection",
        event_key="wa:retry-projection",
    )

    async def exercise() -> None:
        await channel._handle_bridge_message(frame)
        await channel._handle_bridge_message(frame)
        await channel.stop()

    asyncio.run(exercise())

    assert calls == ["delete", "delete"]
    store.close()


def test_projection_cache_is_bounded_and_cleared_on_stop(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "processing.db")
    channel = _channel(store)
    channel._max_dedupe_entries = 1

    async def ack(*args, **kwargs):
        del args, kwargs
        return {"acknowledged": True}

    channel._send_command = ack  # type: ignore[method-assign]

    async def exercise() -> None:
        for index in range(2):
            await channel._handle_bridge_message(
                _frame(
                    "delete",
                    {"chatJid": CHAT, "messageId": f"bounded-{index}", "senderId": "4915"},
                    event_id=f"event-bounded-{index}",
                    event_key=f"wa:bounded-{index}",
                )
            )
        assert len(channel._projected_event_ids) == 1
        await channel.stop()

    asyncio.run(exercise())

    assert not channel._projected_event_ids
    store.close()


def test_edit_delete_invalidation_is_after_ack_and_once_per_event(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "processing.db")
    invalidated: list[str] = []
    order: list[str] = []

    def invalidator(kind, payload):
        del payload
        order.append("invalidate")
        invalidated.append(kind)

    channel = WhatsAppChannel(WhatsAppConfig(), MessageBus())
    channel.set_processing_signals(
        SignalJournalSink(store, clock=lambda: NOW, invalidator=invalidator)
    )

    async def ack(*args, **kwargs):
        del args, kwargs
        order.append("ack")
        return {"acknowledged": True}

    channel._send_command = ack  # type: ignore[method-assign]
    frame = _frame(
        "delete",
        {"chatJid": CHAT, "messageId": "delete-once", "senderId": "4915"},
        event_id="event-delete-once",
        event_key="wa:delete-once",
    )

    async def exercise() -> None:
        await channel._handle_bridge_message(frame)
        await channel._handle_bridge_message(frame)
        await channel.stop()

    asyncio.run(exercise())

    assert order == ["ack", "invalidate", "ack"]
    assert invalidated == ["delete"]
    store.close()


def test_ack_failure_does_not_invalidate_edit_or_delete(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "processing.db")
    invalidated: list[str] = []
    channel = WhatsAppChannel(WhatsAppConfig(), MessageBus())
    channel.set_processing_signals(
        SignalJournalSink(store, clock=lambda: NOW, invalidator=lambda kind, payload: invalidated.append(kind))
    )

    async def fail_ack(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("ack failed")

    channel._send_command = fail_ack  # type: ignore[method-assign]
    asyncio.run(
        channel._handle_bridge_message(
            _frame(
                "edit",
                {"chatJid": CHAT, "messageId": "edit-no-invalidate", "text": "new"},
                event_id="event-edit-no-invalidate",
                event_key="wa:edit-no-invalidate",
            )
        )
    )

    assert invalidated == []
    store.close()


def test_stop_drains_inbound_debounce_and_ack_workers(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "processing.db")
    channel = _channel(store)
    finished: list[str] = []

    async def work(name: str) -> None:
        try:
            await asyncio.sleep(0.01)
        finally:
            finished.append(name)

    async def exercise() -> None:
        inbound = asyncio.create_task(work("inbound"))
        debounce = asyncio.create_task(work("debounce"))
        channel._inbound_tasks.add(inbound)
        channel._debounce_tasks[CHAT] = debounce
        await asyncio.sleep(0)
        await channel.stop()
        assert inbound.done()
        assert debounce.done()
        assert not channel._inbound_tasks
        assert not channel._debounce_tasks

    asyncio.run(exercise())

    assert sorted(finished) == ["debounce", "inbound"]
    store.close()


def test_reader_authenticates_subscribes_and_resolves_live_ack_response(tmp_path: Path) -> None:
    import websockets

    store = ProcessingStore(tmp_path / "processing.db")
    channel = WhatsAppChannel(WhatsAppConfig(bridge_token="secret"), MessageBus())
    channel.set_processing_signals(SignalJournalSink(store, clock=lambda: NOW))
    routed: list[str] = []
    commands: list[dict] = []

    async def exercise() -> None:
        routed_event = asyncio.Event()
        acked = asyncio.Event()

        async def bridge_handler(websocket) -> None:
            async for raw in websocket:
                command = json.loads(raw)
                commands.append(command)
                result = {"protocolVersion": PROTOCOL_VERSION}
                if command["type"] == "subscribe_events":
                    result = {"subscribed": True}
                elif command["type"] == "ack_event":
                    acked.set()
                    result = {"acknowledged": True}
                await websocket.send(
                    json.dumps(
                        {
                            "version": PROTOCOL_VERSION,
                            "type": "response",
                            "requestId": command.get("requestId"),
                            "payload": {"ok": True, "result": result},
                        }
                    )
                )
                if command["type"] == "subscribe_events":
                    await websocket.send(
                        _frame(
                            "message",
                            {
                                "chatJid": CHAT,
                                "messageId": "live-message",
                                "senderId": "4915@s.whatsapp.net",
                                "text": "live",
                            },
                            event_id="live-event",
                            event_key="wa:live-message",
                        )
                    )

        try:
            server = await websockets.serve(bridge_handler, "127.0.0.1", 0)
        except OSError as exc:
            if "bind" in str(exc).lower():
                pytest.skip(f"loopback bind unavailable in this test runner: {exc}")
            raise
        async with server:
            port = server.sockets[0].getsockname()[1]
            async with websockets.connect(f"ws://127.0.0.1:{port}") as websocket:
                channel._ws = websocket
                channel._running = True
                channel._connected = True

                async def publish(event):
                    routed.append(event.message_id)
                    routed_event.set()

                channel._publish_event = publish  # type: ignore[method-assign]
                reader = asyncio.create_task(channel._read_loop())
                channel._reader_task = reader
                await channel._verify_bridge_health("secret", timeout_seconds=1)
                await channel._subscribe_bridge_events("secret", timeout_seconds=1)
                await asyncio.wait_for(acked.wait(), timeout=1)
                await asyncio.wait_for(routed_event.wait(), timeout=1)
                await channel.stop()

        assert [command["type"] for command in commands[:2]] == ["health", "subscribe_events"]
        assert commands[1]["token"] == "secret"
        assert routed == ["live-message"]

    asyncio.run(exercise())
    assert store.get_event("live-event") is not None
    store.close()


def test_ack_queue_full_closes_intake_and_leaves_pending_rows(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "processing.db")
    channel = _channel(store)
    channel._ack_queue_maxsize = 1
    channel._running = True
    channel._connected = True
    gate = asyncio.Event()
    acknowledgements: list[str] = []

    class Socket:
        closed = False

        async def close(self):
            self.closed = True

    socket = Socket()
    channel._ws = socket

    async def ack(command_type: str, payload: dict, timeout_seconds: float, **kwargs):
        del timeout_seconds, kwargs
        assert command_type == "ack_event"
        acknowledgements.append(payload["eventId"])
        await gate.wait()
        return {"acknowledged": True}

    channel._send_command = ack  # type: ignore[method-assign]

    async def exercise() -> None:
        tasks = [
            asyncio.create_task(
                channel._handle_bridge_message(
                    _frame(
                        "message",
                        {
                            "chatJid": CHAT,
                            "messageId": f"queue-{index}",
                            "senderId": "4915@s.whatsapp.net",
                            "text": "queue",
                        },
                        event_id=f"queue-event-{index}",
                        event_key=f"wa:queue-{index}",
                    )
                )
            )
            for index in range(3)
        ]
        await asyncio.sleep(0.05)
        assert socket.closed is True
        assert channel._connected is False
        gate.set()
        await channel.stop()
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(exercise())

    assert store.count_events() == 3
    assert len(acknowledgements) <= 2
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
