"""Task 3: canonical WhatsApp capture is the durable ingest boundary."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest
from yeoman_gateway.bus.events import ReactionMessage
from yeoman_gateway.bus.queue import MessageBus
from yeoman_gateway.channels.whatsapp import WhatsAppChannel
from yeoman_gateway.processing.signals import SignalJournalSink, WhatsAppSignalMapper
from yeoman_gateway.processing.store import ProcessingStore
from yeoman_shared.config.schema import WhatsAppConfig
from yeoman_shared.whatsapp_protocol import PROTOCOL_VERSION

CHAT = "chat@g.us"
NOW = 1_700_000_000_000


def _frame(
    kind: str,
    payload: dict,
    *,
    event_id: str,
    event_key: str,
    ts: int = NOW,
    observed_at: int = NOW,
) -> str:
    return json.dumps(
        {
            "version": PROTOCOL_VERSION,
            "type": kind,
            "ts": ts,
            "accountId": "account-a",
            "eventId": event_id,
            "eventKey": event_key,
            "observedAt": observed_at,
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


def test_replay_with_new_bridge_observation_is_idempotent_after_ack(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "processing.db")
    commit_times = iter((NOW + 100, NOW + 200))
    channel = WhatsAppChannel(WhatsAppConfig(), MessageBus())
    channel.set_processing_signals(
        SignalJournalSink(store, clock=lambda: next(commit_times))
    )
    acknowledgements: list[dict] = []

    async def ack(command_type: str, payload: dict, timeout_seconds: float, **kwargs):
        del timeout_seconds, kwargs
        assert command_type == "ack_event"
        acknowledgements.append(payload)
        return {"acknowledged": True}

    channel._send_command = ack  # type: ignore[method-assign]
    payload = {
        "chatJid": CHAT,
        "messageId": "message-observation-replay",
        "senderId": "4915@s.whatsapp.net",
        "text": "same provider payload",
        "timestamp": 1_700_000_123,
    }
    first = _frame(
        "message",
        payload,
        event_id="event-observation-replay",
        event_key="wa:account-a:message-observation-replay",
    )
    replay = _frame(
        "message",
        payload,
        event_id="event-observation-replay",
        event_key="wa:account-a:message-observation-replay",
        ts=NOW + 50,
        observed_at=NOW + 50,
    )

    asyncio.run(channel._handle_bridge_message(first))
    _drain_inbound(channel)
    asyncio.run(channel._handle_bridge_message(replay))
    _drain_inbound(channel)

    assert acknowledgements == [{"eventId": "event-observation-replay"}] * 2
    assert store.count_events() == 1
    event = store.get_event("event-observation-replay")
    assert event is not None
    assert event.created_ms == NOW + 100
    assert event.payload is not None
    assert event.payload["observed_at_ms"] == NOW
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


def test_ack_failure_closes_live_intake_for_reconnect(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "processing.db")
    channel = _channel(store)
    channel._running = True
    channel._connected = True

    class Socket:
        closed = False

        async def close(self):
            self.closed = True

    socket = Socket()
    channel._ws = socket

    async def fail_ack(*args, **kwargs):
        del args, kwargs
        raise TimeoutError("ack timed out")

    channel._send_command = fail_ack  # type: ignore[method-assign]

    asyncio.run(
        channel._handle_bridge_message(
            _frame(
                "message",
                {
                    "chatJid": CHAT,
                    "messageId": "ack-timeout",
                    "senderId": "4915",
                    "text": "pending reconnect",
                },
                event_id="event-ack-timeout",
                event_key="wa:ack-timeout",
            )
        )
    )

    assert socket.closed is True
    assert channel._connected is False
    assert channel._bridge_intake_closed is True
    assert channel._running is True
    assert store.get_event("event-ack-timeout") is not None
    store.close()


def test_stop_drains_acknowledged_projection_before_return(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "processing.db")
    channel = _channel(store)
    channel.config.debounce_ms = 0
    projection_started = asyncio.Event()
    release_projection = asyncio.Event()
    projection_finished = asyncio.Event()

    async def ack(*args, **kwargs):
        del args, kwargs
        return {"acknowledged": True}

    async def publish(event):
        del event
        projection_started.set()
        await release_projection.wait()
        projection_finished.set()

    channel._send_command = ack  # type: ignore[method-assign]
    channel._publish_event = publish  # type: ignore[method-assign]

    async def exercise() -> None:
        reader = asyncio.create_task(
            channel._handle_bridge_message(
                _frame(
                    "message",
                    {
                        "chatJid": CHAT,
                        "messageId": "slow-projection",
                        "senderId": "4915",
                        "text": "must finish",
                    },
                    event_id="event-slow-projection",
                    event_key="wa:slow-projection",
                )
            )
        )
        channel._reader_task = reader
        channel._events_subscribed = True
        await asyncio.wait_for(projection_started.wait(), timeout=1)

        stopping = asyncio.create_task(channel.stop())
        await asyncio.sleep(0.02)
        assert not stopping.done()
        assert not projection_finished.is_set()
        release_projection.set()
        await stopping
        await reader
        assert projection_finished.is_set()

    asyncio.run(exercise())
    store.close()


def _parsed_message(channel: WhatsAppChannel, message_id: str, text: str):
    event = channel._parse_inbound_event(
        {
            "chatJid": CHAT,
            "messageId": message_id,
            "senderId": "4915",
            "text": text,
        }
    )
    assert event is not None
    return event


def test_stop_waits_for_timer_owned_debounce_publisher(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "processing.db")
    channel = WhatsAppChannel(WhatsAppConfig(debounce_ms=1), MessageBus())
    channel._chat_registry = None
    old_started = asyncio.Event()
    release_old = asyncio.Event()
    published: list[str] = []

    async def publish(event):
        old_started.set()
        await release_old.wait()
        published.append(event.message_id)

    channel._publish_event = publish  # type: ignore[method-assign]

    async def exercise() -> None:
        await channel._ingest_inbound_event(_parsed_message(channel, "old", "old"))
        await asyncio.wait_for(old_started.wait(), timeout=1)
        stopping = asyncio.create_task(channel.stop())
        await asyncio.sleep(0.02)
        waiting = not stopping.done()
        release_old.set()
        await stopping
        assert waiting
        assert published == ["old"]

    asyncio.run(exercise())
    store.close()


def test_debounce_old_batch_completes_before_new_arrival(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "processing.db")
    channel = WhatsAppChannel(WhatsAppConfig(debounce_ms=1), MessageBus())
    channel._chat_registry = None
    old_started = asyncio.Event()
    release_old = asyncio.Event()
    started: list[str] = []

    async def publish(event):
        started.append(event.message_id)
        if event.message_id == "old":
            old_started.set()
            await release_old.wait()

    channel._publish_event = publish  # type: ignore[method-assign]

    async def exercise() -> None:
        await channel._ingest_inbound_event(_parsed_message(channel, "old", "old"))
        await asyncio.wait_for(old_started.wait(), timeout=1)
        await channel._ingest_inbound_event(_parsed_message(channel, "new", "new"))
        await asyncio.sleep(0.02)
        assert started == ["old"]
        release_old.set()
        await channel.stop()
        assert started == ["old", "new"]

    asyncio.run(exercise())
    store.close()


def test_capture_failure_closes_live_intake_for_reconnect(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "processing.db")
    channel = _channel(store)
    channel._running = True
    channel._connected = True
    channel._events_subscribed = True

    class Socket:
        closed = False

        async def close(self):
            self.closed = True

    socket = Socket()
    channel._ws = socket

    def fail_append(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("sqlite append failed")

    store.append_event = fail_append  # type: ignore[method-assign]

    asyncio.run(
        channel._handle_bridge_message(
            _frame(
                "message",
                {
                    "chatJid": CHAT,
                    "messageId": "capture-timeout",
                    "senderId": "4915",
                    "text": "pending reconnect",
                },
                event_id="event-capture-timeout",
                event_key="wa:capture-timeout",
            )
        )
    )

    assert socket.closed is True
    assert channel._bridge_intake_closed is True
    assert channel._connected is False
    assert channel._running is True
    store.close()


def test_start_aborts_when_replay_ack_fails(tmp_path: Path) -> None:
    import websockets

    store = ProcessingStore(tmp_path / "processing.db")
    replay_failed = asyncio.Event()

    async def exercise() -> None:
        async def bridge_handler(websocket) -> None:
            async for raw in websocket:
                command = json.loads(raw)
                assert command.get("token") == "secret"
                if command["type"] == "health":
                    result = {"protocolVersion": PROTOCOL_VERSION}
                    ok = True
                elif command["type"] == "subscribe_events":
                    await websocket.send(
                        _frame(
                            "message",
                            {
                                "chatJid": CHAT,
                                "messageId": "startup-replay",
                                "senderId": "4915",
                                "text": "startup replay",
                            },
                            event_id="event-startup-replay",
                            event_key="wa:startup-replay",
                        )
                    )
                    result = {"subscribed": True}
                    ok = True
                elif command["type"] == "ack_event":
                    replay_failed.set()
                    result = None
                    ok = False
                else:
                    result = {}
                    ok = True
                payload: dict[str, object] = {"ok": ok}
                if ok:
                    payload["result"] = result or {}
                else:
                    payload["error"] = {
                        "code": "ERR_ACK_REJECTED",
                        "message": "replay ACK rejected",
                        "retryable": True,
                    }
                await websocket.send(
                    json.dumps(
                        {
                            "version": PROTOCOL_VERSION,
                            "type": "response",
                            "requestId": command.get("requestId"),
                            "payload": payload,
                        }
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
            channel = WhatsAppChannel(
                WhatsAppConfig(
                    bridge_host="127.0.0.1",
                    bridge_port=port,
                    bridge_token="secret",
                    bridge_auto_repair=False,
                    bridge_startup_timeout_ms=1_000,
                ),
                MessageBus(),
            )
            channel.set_processing_signals(SignalJournalSink(store, clock=lambda: NOW))
            channel._runtime.ensure_ready = lambda **kwargs: None  # type: ignore[method-assign]
            await channel.start()
            assert replay_failed.is_set()
            assert channel._connected is False
            assert channel._running is False
            assert channel._bridge_intake_closed is True

    asyncio.run(exercise())
    assert store.get_event("event-startup-replay") is not None
    store.close()


def test_debounce_bucket_flushes_before_item_or_byte_ceiling(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "processing.db")
    channel = WhatsAppChannel(
        WhatsAppConfig(debounce_ms=60_000, debounce_media_ms=60_000), MessageBus()
    )
    channel.set_processing_signals(SignalJournalSink(store, clock=lambda: NOW))
    channel._debounce_max_items = 2
    channel._debounce_max_bytes = 10_000
    published: list[tuple[str, tuple[str, ...]]] = []

    async def ack(*args, **kwargs):
        del args, kwargs
        return {"acknowledged": True}

    async def publish(event):
        published.append((event.message_id, event.source_ids))

    channel._send_command = ack  # type: ignore[method-assign]
    channel._publish_event = publish  # type: ignore[method-assign]

    async def exercise() -> None:
        for index, text in enumerate(("one", "two", "three")):
            await channel._handle_bridge_message(
                _frame(
                    "message",
                    {
                        "chatJid": CHAT,
                        "messageId": f"debounce-{index}",
                        "senderId": "4915",
                        "text": text,
                    },
                    event_id=f"event-debounce-{index}",
                    event_key=f"wa:debounce-{index}",
                )
            )
        bucket = channel._debounce_buffers[f"{CHAT}:4915"]
        assert len(bucket) <= 2
        assert channel._debounce_buffer_bytes[f"{CHAT}:4915"] <= 10_000
        await channel.stop()

    asyncio.run(exercise())

    assert published
    assert {source_id for _, source_ids in published for source_id in source_ids} == {
        "debounce-0",
        "debounce-1",
        "debounce-2",
    }
    store.close()


def test_stop_waits_for_boundary_owned_debounce_publisher(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "processing.db")
    channel = WhatsAppChannel(WhatsAppConfig(debounce_ms=60_000), MessageBus())
    channel._chat_registry = None
    channel._debounce_max_items = 1
    old_started = asyncio.Event()
    release_old = asyncio.Event()
    published: list[str] = []

    async def publish(event):
        if event.message_id == "old":
            old_started.set()
            await release_old.wait()
        published.append(event.message_id)

    channel._publish_event = publish  # type: ignore[method-assign]

    async def exercise() -> None:
        await channel._ingest_inbound_event(_parsed_message(channel, "old", "old"))
        boundary = asyncio.create_task(
            channel._ingest_inbound_event(_parsed_message(channel, "new1", "new1"))
        )
        await asyncio.wait_for(old_started.wait(), timeout=1)
        stopping = asyncio.create_task(channel.stop())
        await asyncio.sleep(0.02)
        assert not stopping.done()
        release_old.set()
        await boundary
        await stopping
        assert published == ["old", "new1"]

    asyncio.run(exercise())
    assert not channel._debounce_publish_tasks
    store.close()


def test_boundary_arrivals_keep_triggering_event_before_successor(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "processing.db")
    channel = WhatsAppChannel(WhatsAppConfig(debounce_ms=60_000), MessageBus())
    channel._chat_registry = None
    channel._debounce_max_items = 1
    old_started = asyncio.Event()
    release_old = asyncio.Event()
    published: list[str] = []

    async def publish(event):
        if event.message_id == "old":
            old_started.set()
            await release_old.wait()
        published.extend(event.source_ids)

    channel._publish_event = publish  # type: ignore[method-assign]

    async def exercise() -> None:
        await channel._ingest_inbound_event(_parsed_message(channel, "old", "old"))
        new1 = asyncio.create_task(
            channel._ingest_inbound_event(_parsed_message(channel, "new1", "new1"))
        )
        await asyncio.wait_for(old_started.wait(), timeout=1)
        new2 = asyncio.create_task(
            channel._ingest_inbound_event(_parsed_message(channel, "new2", "new2"))
        )
        await new2
        release_old.set()
        await new1
        await channel.stop()

    asyncio.run(exercise())
    assert published == ["old", "new1", "new2"]
    assert not channel._debounce_publish_tasks
    store.close()


def test_timer_publish_failure_starts_successor_for_buffered_arrival(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "processing.db")
    channel = WhatsAppChannel(WhatsAppConfig(debounce_ms=1), MessageBus())
    channel._chat_registry = None
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    published: list[str] = []

    async def publish(event):
        if event.message_id == "old":
            first_started.set()
            await release_first.wait()
            raise RuntimeError("first debounce publication failed")
        published.append(event.message_id)

    channel._publish_event = publish  # type: ignore[method-assign]

    async def exercise() -> None:
        await channel._ingest_inbound_event(_parsed_message(channel, "old", "old"))
        await asyncio.wait_for(first_started.wait(), timeout=1)
        await channel._ingest_inbound_event(_parsed_message(channel, "new", "new"))
        release_first.set()
        await channel.stop()

    asyncio.run(exercise())
    assert published == ["new"]
    assert not channel._debounce_publish_tasks
    assert not channel._debounce_tasks
    assert not channel._debounce_buffers
    store.close()


def test_concurrent_timer_and_boundary_admission_preserves_order_and_bounds(
    tmp_path: Path,
) -> None:
    store = ProcessingStore(tmp_path / "processing.db")
    channel = WhatsAppChannel(WhatsAppConfig(debounce_ms=1), MessageBus())
    channel._chat_registry = None
    channel._debounce_max_items = 2
    channel._debounce_max_bytes = 10_000
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    published: list[str] = []

    async def publish(event):
        if not published:
            first_started.set()
            await release_first.wait()
        published.extend(event.source_ids)
        await asyncio.sleep(0)

    channel._publish_event = publish  # type: ignore[method-assign]

    async def exercise() -> None:
        await channel._ingest_inbound_event(_parsed_message(channel, "event-0", "0"))
        await asyncio.wait_for(first_started.wait(), timeout=1)
        arrivals = [
            asyncio.create_task(
                channel._ingest_inbound_event(_parsed_message(channel, f"event-{i}", str(i)))
            )
            for i in range(1, 9)
        ]
        await asyncio.sleep(0)
        release_first.set()
        await asyncio.gather(*arrivals)
        await channel.stop()

    asyncio.run(exercise())
    assert published == [f"event-{i}" for i in range(9)]
    assert not channel._debounce_publish_tasks
    assert not channel._debounce_tasks
    assert not channel._debounce_buffers
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


def test_strict_edit_delete_projection_is_deferred_to_task4(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "processing.db")
    channel = _channel(store)
    sink = channel._processing_signals
    calls: list[str] = []

    def invalidate(kind, payload):
        del payload
        calls.append(kind)

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

    # Strict canonical capture is the Task 3B ACK boundary.  Legacy invalidation
    # needs a durable event-id projection owned by Task 4, so it is deliberately
    # not invoked from this path (including after a reconnect/replay).
    assert calls == []
    store.close()


def test_strict_replay_does_not_depend_on_resettable_projection_cache(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "processing.db")
    channel = _channel(store)
    invalidated: list[str] = []
    channel.set_processing_signals(
        SignalJournalSink(store, clock=lambda: NOW, invalidator=lambda kind, payload: invalidated.append(kind))
    )

    async def ack(*args, **kwargs):
        del args, kwargs
        return {"acknowledged": True}

    channel._send_command = ack  # type: ignore[method-assign]

    async def exercise() -> None:
        frame = _frame(
            "delete",
            {"chatJid": CHAT, "messageId": "bounded", "senderId": "4915"},
            event_id="event-bounded",
            event_key="wa:bounded",
        )
        await channel._handle_bridge_message(frame)
        # Simulate the per-connection worker teardown/recreation that a
        # reconnect performs, then replay the same canonical event.
        await channel._stop_bridge_worker()
        channel._bridge_intake_closed = False
        channel._stopping = False
        await channel._handle_bridge_message(frame)
        await channel.stop()

    asyncio.run(exercise())

    assert invalidated == []
    store.close()


def test_edit_delete_invalidation_is_deferred_after_ack_to_task4(tmp_path: Path) -> None:
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

    assert order == ["ack", "ack"]
    assert invalidated == []
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
        delivered_event_ids: set[str] = set()
        ack_rejected = asyncio.Event()
        reaction_sent = asyncio.Event()
        connected_during_projection: list[bool] = []

        async def bridge_handler(websocket) -> None:
            async for raw in websocket:
                command = json.loads(raw)
                commands.append(command)
                assert command.get("token") == "secret"
                result = {"protocolVersion": PROTOCOL_VERSION}
                response_ok = True
                response_error: dict[str, object] | None = None
                if command["type"] == "subscribe_events":
                    delivered_event_ids.add("live-event")
                    # The Bridge may replay before it resolves the subscribe
                    # command.  The reader must buffer this frame while it
                    # still consumes the response.
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
                    result = {"subscribed": True}
                elif command["type"] == "ack_event":
                    if command.get("payload", {}).get("eventId") not in delivered_event_ids:
                        ack_rejected.set()
                        response_ok = False
                        response_error = {
                            "code": "ERR_NOT_DELIVERED",
                            "message": "event was not delivered",
                            "retryable": True,
                        }
                    else:
                        acked.set()
                        result = {"acknowledged": True}
                elif command["type"] == "react":
                    reaction_sent.set()
                    result = {"reacted": True}
                response_payload: dict[str, object] = {"ok": response_ok}
                if response_ok:
                    response_payload["result"] = result
                else:
                    response_payload["error"] = response_error or {}
                await websocket.send(
                    json.dumps(
                        {
                            "version": PROTOCOL_VERSION,
                            "type": "response",
                            "requestId": command.get("requestId"),
                            "payload": response_payload,
                        }
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

                async def publish(event):
                    connected_during_projection.append(channel._connected)
                    await channel.send_reaction(
                        ReactionMessage(
                            channel="whatsapp",
                            chat_id=event.chat_jid,
                            message_id=event.message_id,
                            emoji="👀",
                        )
                    )
                    routed.append(event.message_id)
                    routed_event.set()

                channel._publish_event = publish  # type: ignore[method-assign]
                reader = asyncio.create_task(channel._read_loop())
                channel._reader_task = reader
                await channel._verify_bridge_health("secret", timeout_seconds=1)
                await channel._subscribe_bridge_events("secret", timeout_seconds=1)
                await asyncio.wait_for(acked.wait(), timeout=1)
                await asyncio.wait_for(reaction_sent.wait(), timeout=1)
                await asyncio.wait_for(routed_event.wait(), timeout=1)
                assert not ack_rejected.is_set()
                await channel.stop()

        assert [command["type"] for command in commands[:2]] == ["health", "subscribe_events"]
        assert all(command["token"] == "secret" for command in commands)
        assert delivered_event_ids == {"live-event"}
        assert connected_during_projection == [True]
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
