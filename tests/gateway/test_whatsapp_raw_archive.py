"""V1 spec §4.0: native WhatsApp frames and outbound commands reach the raw archive."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from yeoman_gateway.bus.queue import MessageBus
from yeoman_gateway.channels.whatsapp import WhatsAppChannel
from yeoman_gateway.media.storage import MediaStorage
from yeoman_gateway.processing.signals import SignalJournalSink
from yeoman_gateway.processing.store import ProcessingStore
from yeoman_shared.config.schema import WhatsAppConfig
from yeoman_shared.raw_archive.records import archive_files, iter_records
from yeoman_shared.raw_archive.writer import RawArchive, RawArchiveCapacityError
from yeoman_shared.whatsapp_protocol import PROTOCOL_VERSION

NOW = 1_790_000_000_000
CHAT = "chat@g.us"


def _frame(payload: dict, *, event_id: str = "evt-1", kind: str = "message") -> str:
    return json.dumps(
        {
            "version": PROTOCOL_VERSION,
            "type": kind,
            "ts": NOW,
            "accountId": "account-a",
            "eventId": event_id,
            "eventKey": f"key-{event_id}",
            "observedAt": NOW,
            "payload": payload,
            "token": "must-not-be-archived",
        }
    )


def _setup(tmp_path: Path) -> tuple[WhatsAppChannel, RawArchive, ProcessingStore]:
    store = ProcessingStore(tmp_path / "processing.db")
    media = MediaStorage(incoming_dir=tmp_path / "incoming", outgoing_dir=tmp_path / "outgoing")
    channel = WhatsAppChannel(WhatsAppConfig(), MessageBus(), media_storage=media)
    channel.set_processing_signals(SignalJournalSink(store, clock=lambda: NOW))
    archive = RawArchive(
        tmp_path / "raw",
        spool=tmp_path / "spool",
        status_path=tmp_path / "run" / "s.json",
        clock=lambda: NOW,
    )
    channel.set_raw_archive(archive)

    async def ack(command_type: str, payload: dict, timeout_seconds: float, **kwargs):
        return {"acknowledged": True}

    channel._send_command = ack  # type: ignore[method-assign]
    channel._archive_inbound_event = lambda event: None  # type: ignore[method-assign]

    async def publish(event):
        return None

    channel._publish_event = publish  # type: ignore[method-assign]
    return channel, archive, store


def _records(root: Path) -> list[dict]:
    return [r for path in archive_files(root) for _, r, _ in iter_records(path) if r]


def _message(text: str = "hello", **extra: object) -> dict:
    payload = {
        "chatJid": CHAT,
        "messageId": "m-1",
        "senderId": "4915@s.whatsapp.net",
        "text": text,
    }
    payload.update(extra)
    return payload


def test_inbound_frame_is_archived_without_token(tmp_path: Path) -> None:
    channel, _, _ = _setup(tmp_path)
    asyncio.run(channel._handle_bridge_message(_frame(_message())))
    [record] = _records(tmp_path / "raw")
    assert record["kind"] == "message" and record["direction"] == "in"
    assert record["native_id"] == "evt-1"
    assert record["chat_id"] == CHAT
    assert record["account"] == "account-a"
    assert record["native"]["payload"]["text"] == "hello"
    assert "token" not in record["native"]


def test_frame_is_archived_even_when_journal_capture_fails(tmp_path: Path) -> None:
    channel, _, _ = _setup(tmp_path)

    class Broken:
        def capture(self, *args, **kwargs):
            raise RuntimeError("journal down")

    channel.set_processing_signals(Broken())
    asyncio.run(channel._handle_bridge_message(_frame(_message())))
    assert len(_records(tmp_path / "raw")) == 1


def test_inbound_capacity_stops_before_capture_and_ack(tmp_path: Path) -> None:
    channel, archive, _ = _setup(tmp_path)
    channel._running = True
    capture_calls: list[str] = []
    ack_calls: list[str] = []

    class Capture:
        def capture(self, *args, **kwargs):
            capture_calls.append("capture")

    async def ack(command_type: str, payload: dict, timeout_seconds: float, **kwargs):
        ack_calls.append(command_type)
        return {"acknowledged": True}

    def capacity_error(event):
        raise RawArchiveCapacityError("raw archive capacity reached")

    archive.append = capacity_error  # type: ignore[method-assign]
    channel.set_processing_signals(Capture())
    channel._send_command = ack  # type: ignore[method-assign]

    asyncio.run(channel._handle_bridge_message(_frame(_message())))

    assert capture_calls == []
    assert ack_calls == []
    assert channel._bridge_intake_closed and not channel._running


def test_inbound_media_inside_incoming_dir_is_copied(tmp_path: Path) -> None:
    channel, _, _ = _setup(tmp_path)
    media_file = tmp_path / "incoming" / "whatsapp" / "photo.jpg"
    media_file.parent.mkdir(parents=True)
    media_file.write_bytes(b"jpeg")
    payload = _message(media={"kind": "image", "mimeType": "image/jpeg", "path": str(media_file)})
    asyncio.run(channel._handle_bridge_message(_frame(payload)))
    [record] = _records(tmp_path / "raw")
    assert record["media"]["stored"] is True
    assert (tmp_path / "raw" / record["media"]["path"]).read_bytes() == b"jpeg"


def test_media_path_outside_incoming_dir_is_not_copied(tmp_path: Path) -> None:
    channel, _, _ = _setup(tmp_path)
    secret = tmp_path / "secret.env"
    secret.write_text("KEY=1")
    payload = _message(
        media={"kind": "document", "path": str(tmp_path / "incoming" / ".." / "secret.env")}
    )
    asyncio.run(channel._handle_bridge_message(_frame(payload)))
    [record] = _records(tmp_path / "raw")
    assert record["media"] == {"stored": False, "path": None, "reason": "path_rejected"}
    assert not (tmp_path / "raw" / "media").exists()


def test_replayed_frame_after_restart_is_appended_again(tmp_path: Path) -> None:
    channel, _, _ = _setup(tmp_path)
    asyncio.run(channel._handle_bridge_message(_frame(_message())))
    restarted, _, _ = _setup(tmp_path)
    asyncio.run(restarted._handle_bridge_message(_frame(_message())))
    assert [r["native_id"] for r in _records(tmp_path / "raw")] == ["evt-1", "evt-1"]


class _FakeWs:
    def __init__(self, channel: WhatsAppChannel, reply: dict) -> None:
        self.channel = channel
        self.reply = reply
        self.sent: list[dict] = []

    async def send(self, encoded: str) -> None:
        envelope = json.loads(encoded)
        self.sent.append(envelope)
        asyncio.get_running_loop().call_soon(
            self.channel._resolve_pending, envelope["requestId"], self.reply
        )


def _real_send(tmp_path: Path, reply: dict) -> tuple[WhatsAppChannel, _FakeWs]:
    channel, _, _ = _setup(tmp_path)
    del channel._send_command
    ws = _FakeWs(channel, reply)
    channel._ws = ws  # type: ignore[assignment]
    return channel, ws


def test_outbound_send_is_archived_as_request_and_result(tmp_path: Path) -> None:
    bridge_result = {
        "sent": {
            "to": CHAT,
            "messageId": "P-1",
            "providerMessageId": "P-1",
            "clientMessageId": "client-1",
        }
    }
    channel, ws = _real_send(tmp_path, {"ok": True, "result": bridge_result})
    payload = {"to": CHAT, "text": "hi there"}
    asyncio.run(channel._send_command("send_text", payload, timeout_seconds=2.0, token="secret-t"))
    request, result = _records(tmp_path / "raw")
    assert request["kind"] == "outbound_request" and request["direction"] == "out"
    assert request["native"] == {
        "type": "send_text",
        "requestId": ws.sent[0]["requestId"],
        "payload": payload,
    }
    assert result["kind"] == "outbound_result"
    assert result["correlation_id"] == request["correlation_id"] == ws.sent[0]["requestId"]
    assert result["native_id"] == "P-1"
    assert result["native"]["result"] == bridge_result
    assert result["chat_id"] == CHAT
    assert "secret-t" not in json.dumps(request) + json.dumps(result)


def test_outbound_request_capacity_prevents_send(tmp_path: Path) -> None:
    channel, ws = _real_send(tmp_path, {"ok": True, "result": {"providerMessageId": "P-1"}})
    channel._running = True
    archive = channel._raw_archive
    assert archive is not None

    def capacity_error(event):
        raise RawArchiveCapacityError("raw archive capacity reached")

    archive.append = capacity_error  # type: ignore[method-assign]
    with pytest.raises(RawArchiveCapacityError):
        asyncio.run(
            channel._send_command("send_text", {"to": CHAT, "text": "hello"}, 2.0, token="t")
        )
    assert ws.sent == []
    assert channel._bridge_intake_closed and not channel._running


def test_successful_send_stays_successful_when_result_cannot_be_archived(tmp_path: Path) -> None:
    channel, ws = _real_send(tmp_path, {"ok": True, "result": {"providerMessageId": "P-1"}})
    channel._running = True
    archive = channel._raw_archive
    assert archive is not None
    append = archive.append

    def fail_result(event):
        if event.kind == "outbound_result":
            raise RawArchiveCapacityError("raw archive capacity reached")
        return append(event)

    archive.append = fail_result  # type: ignore[method-assign]
    result = asyncio.run(
        channel._send_command("send_text", {"to": CHAT, "text": "hello"}, 2.0, token="t")
    )

    assert result == {"providerMessageId": "P-1"}
    assert len(ws.sent) == 1
    assert channel._bridge_intake_closed and not channel._running


def test_startup_replay_capacity_does_not_repair_or_ack(tmp_path: Path, monkeypatch) -> None:
    import websockets

    channel, archive, store = _setup(tmp_path)
    del channel._send_command
    channel._require_token = lambda: "test-token"  # type: ignore[method-assign]

    class Runtime:
        def __init__(self) -> None:
            self.ensure_calls = 0
            self.repair_calls = 0

        def ensure_ready(self, **kwargs) -> None:
            self.ensure_calls += 1

        def repair_once(self) -> None:
            self.repair_calls += 1

    class ReplaySocket:
        def __init__(self) -> None:
            self.subscription_sent = asyncio.Event()
            self.closed = asyncio.Event()
            self.sent_types: list[str] = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            await self.close()

        async def send(self, encoded: str) -> None:
            envelope = json.loads(encoded)
            command_type = envelope["type"]
            self.sent_types.append(command_type)
            if command_type == "subscribe_events":
                self.subscription_sent.set()
                await asyncio.sleep(0)
            result = {
                "health": {"protocolVersion": PROTOCOL_VERSION},
                "subscribe_events": {"subscribed": True},
            }[command_type]
            channel._resolve_pending(envelope["requestId"], {"ok": True, "result": result})

        async def close(self) -> None:
            self.closed.set()

        def __aiter__(self):
            return self._messages()

        async def _messages(self):
            await self.subscription_sent.wait()
            yield _frame(_message(), event_id="evt-replay")
            await self.closed.wait()

    def capacity_error(event):
        raise RawArchiveCapacityError("raw archive capacity reached")

    archive.append = capacity_error  # type: ignore[method-assign]
    runtime = Runtime()
    channel._runtime = runtime  # type: ignore[assignment]
    socket = ReplaySocket()
    connections = []

    def connect(*args, **kwargs):
        connections.append(socket)
        return socket

    monkeypatch.setattr(websockets, "connect", connect)
    try:
        asyncio.run(channel.start())
        assert runtime.repair_calls == 0
        assert runtime.ensure_calls == 1
        assert len(connections) == 1
        assert socket.sent_types == ["health", "subscribe_events"]
        assert store.count_events() == 0
    finally:
        store.close()


def test_failed_outbound_send_is_archived_with_error(tmp_path: Path) -> None:
    channel, _ = _real_send(
        tmp_path, {"ok": False, "error": {"code": "ERR_X", "message": "nope", "retryable": False}}
    )
    with pytest.raises(Exception):
        asyncio.run(channel._send_command("send_text", {"to": CHAT, "text": "x"}, 2.0, token="t"))
    _, result = _records(tmp_path / "raw")
    assert result["kind"] == "outbound_result"
    assert result["native"]["error"].startswith("BridgeProtocolError")


def test_presence_and_ack_commands_are_not_archived(tmp_path: Path) -> None:
    channel, _ = _real_send(tmp_path, {"ok": True, "result": {}})
    asyncio.run(channel._send_command("presence_update", {"to": CHAT}, 2.0, token="t"))
    asyncio.run(channel._send_command("ack_event", {"eventId": "evt-1"}, 2.0, token="t"))
    assert _records(tmp_path / "raw") == []
