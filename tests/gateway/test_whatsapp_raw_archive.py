"""V1 spec §4.0: native WhatsApp frames and outbound commands reach the raw archive."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yeoman_gateway.channels.whatsapp as whatsapp_module
from yeoman_gateway.bus.queue import MessageBus
from yeoman_gateway.channels.whatsapp import InboundEvent, WhatsAppChannel
from yeoman_gateway.media.storage import MediaStorage
from yeoman_gateway.processing.signals import SignalJournalSink
from yeoman_gateway.processing.store import ProcessingStore
from yeoman_shared.config.schema import WhatsAppConfig
from yeoman_shared.raw_archive.records import archive_files, iter_records
from yeoman_shared.raw_archive.writer import RawArchive, RawArchiveCapacityError
from yeoman_shared.whatsapp_protocol import PROTOCOL_VERSION

NOW = 1_790_000_000_000
CHAT = "chat@g.us"


class _FixedDateTime:
    @staticmethod
    def now(tz=None):
        return datetime.fromtimestamp(NOW / 1000, tz or UTC)


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


def _use_inline_bridge_ack(channel: WhatsAppChannel) -> None:
    """Keep the real capture/ACK path while making tests own worker scheduling."""
    def ensure_pipeline() -> None:
        channel._bridge_pipeline_loop = asyncio.get_running_loop()
        channel._bridge_ack_queue = asyncio.Queue(
            maxsize=max(1, int(channel._ack_queue_maxsize))
        )
        channel._bridge_event_lock = asyncio.Lock()
        channel._bridge_inflight.clear()
        channel._bridge_intake_closed = False

    channel._ensure_bridge_pipeline = ensure_pipeline  # type: ignore[method-assign]


async def _handle_with_inline_ack(channel: WhatsAppChannel, raw: str) -> None:
    await channel._handle_bridge_message(raw)
    queue = channel._bridge_ack_queue
    if queue is None:
        return
    while not queue.empty():
        work = queue.get_nowait()
        try:
            await channel._ack_and_project_bridge_work(work)
        finally:
            queue.task_done()


def _inline_raw_append(monkeypatch: pytest.MonkeyPatch) -> None:
    """Avoid a default-executor thread in sync tests; keep RawArchive writes real."""
    async def append_inline(archive, event) -> None:
        if archive is not None:
            archive.append(event)

    monkeypatch.setattr("yeoman_gateway.channels.whatsapp.append_async", append_inline)


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


def test_membership_change_is_raw_archived_before_journaling(tmp_path: Path) -> None:
    channel, archive, store = _setup(tmp_path)
    order: list[str] = []
    append_raw = archive.append
    append_event = store.append_event

    def record_raw(event):
        order.append("raw")
        return append_raw(event)

    def record_journal(*args, **kwargs):
        order.append("journal")
        return append_event(*args, **kwargs)

    async def ack(command_type: str, payload: dict, timeout_seconds: float, **kwargs):
        order.append("ack")
        return {"acknowledged": True}

    archive.append = record_raw  # type: ignore[method-assign]
    store.append_event = record_journal  # type: ignore[method-assign]
    channel._send_command = ack  # type: ignore[method-assign]
    frame = json.loads(
        _frame(
            {
                "chatJid": CHAT,
                "action": "add",
                "changeId": "stub:message-9",
                "sourceCopyId": "stub:message-9",
                "messageId": "message-9",
                "stubType": 27,
                "providerTimestampMs": NOW,
                "participants": [{"lid": "123@lid"}],
            },
            event_id="stub-event",
            kind="membership_change",
        )
    )
    frame["eventKey"] = "whatsapp:account-a:chat@g.us:membership_change:stub%3Amessage-9"
    frame["payload"]["actor"] = {"lid": "456@lid"}

    asyncio.run(channel._handle_bridge_message(json.dumps(frame)))

    assert order == ["raw", "journal", "ack"]
    [record] = _records(tmp_path / "raw")
    assert record["kind"] == "membership_change"
    assert record["native"]["payload"]["sourceCopyId"] == "stub:message-9"
    assert record["native"]["payload"]["messageId"] == "message-9"
    assert record["native"]["payload"]["stubType"] == 27
    assert store.count_events() == 1


def test_empty_complete_membership_snapshot_is_archived_journaled_and_acked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    channel, archive, store = _setup(tmp_path)
    order: list[str] = []
    issued: list[object] = []
    recorded: list[object] = []
    append_raw = archive.append
    append_event = store.append_event

    class Issuer:
        def observe(self, observation: object) -> None:
            issued.append(observation)

    class IdentityService:
        def record_provider_pair(self, observation: object) -> None:
            recorded.append(observation)

    channel.set_processing_signals(
        SignalJournalSink(
            store,
            clock=lambda: NOW,
            identity_observation_issuer=Issuer(),
            statements=IdentityService(),
        )
    )
    _inline_raw_append(monkeypatch)

    def record_raw(event):
        order.append("raw")
        return append_raw(event)

    def record_journal(*args, **kwargs):
        order.append("journal")
        return append_event(*args, **kwargs)

    async def ack(command_type: str, payload: dict, timeout_seconds: float, **kwargs):
        del timeout_seconds, kwargs
        assert command_type == "ack_event"
        assert store.get_event("empty-roster") is not None
        order.append("ack")
        return {"acknowledged": True}

    archive.append = record_raw  # type: ignore[method-assign]
    store.append_event = record_journal  # type: ignore[method-assign]
    channel._send_command = ack  # type: ignore[method-assign]
    _use_inline_bridge_ack(channel)
    payload = {
        "chatJid": CHAT,
        "snapshotAtMs": NOW,
        "complete": True,
        "memberCount": 0,
        "participants": [],
    }
    frame = json.loads(
        _frame(payload, event_id="empty-roster", kind="membership_snapshot")
    )
    frame["eventKey"] = f"whatsapp:account-a:{CHAT}:membership_snapshot:{NOW}"

    async def capture() -> None:
        channel._reader_task = asyncio.current_task()
        channel._events_subscribed = True
        await _handle_with_inline_ack(channel, json.dumps(frame))

    asyncio.run(capture())

    assert order == ["raw", "journal", "ack"]
    [record] = _records(tmp_path / "raw")
    assert record["kind"] == "membership_snapshot"
    event = store.get_event("empty-roster")
    assert event is not None and event.payload is not None
    assert event.payload["complete"] is True
    assert event.payload["member_count"] == 0
    assert event.payload["participants"] == []
    assert issued == recorded == []
    store.close()


@pytest.mark.parametrize("kind", ["membership_change", "membership_snapshot"])
def test_membership_conflict_is_archived_but_not_acked_or_projected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    channel, archive, store = _setup(tmp_path)
    channel._connected = True
    acknowledgements: list[str] = []
    order: list[str] = []
    issued: list[object] = []
    recorded: list[object] = []
    append_raw = archive.append
    append_event = store.append_event

    def record_raw(event):
        order.append("raw")
        return append_raw(event)

    def record_journal(*args, **kwargs):
        order.append("journal")
        return append_event(*args, **kwargs)

    archive.append = record_raw  # type: ignore[method-assign]
    store.append_event = record_journal  # type: ignore[method-assign]
    _use_inline_bridge_ack(channel)
    _inline_raw_append(monkeypatch)

    class Socket:
        closed = False

        async def close(self):
            self.closed = True

    class Issuer:
        def observe(self, observation: object) -> None:
            issued.append(observation)

    class IdentityService:
        def record_provider_pair(self, observation: object) -> None:
            recorded.append(observation)

    socket = Socket()
    channel._ws = socket
    channel.set_processing_signals(
        SignalJournalSink(
            store,
            clock=lambda: NOW,
            identity_observation_issuer=Issuer(),
            statements=IdentityService(),
        )
    )

    async def ack(command_type: str, payload: dict, timeout_seconds: float, **kwargs):
        del timeout_seconds, kwargs
        assert command_type == "ack_event"
        acknowledgements.append(payload["eventId"])
        order.append("ack")
        return {"acknowledged": True}

    channel._send_command = ack  # type: ignore[method-assign]

    def payload(member: str) -> dict:
        if kind == "membership_change":
            return {
                "chatJid": CHAT,
                "action": "add",
                "changeId": "same-change",
                "sourceCopyId": "stub:same-change",
                "participants": [{"lid": member}],
            }
        digits = "123" if member == "123@lid" else "456"
        return {
            "chatJid": CHAT,
            "snapshotAtMs": NOW,
            "complete": True,
            "memberCount": 1,
            "participants": [
                {"lid": member, "phoneJid": f"49{digits}@s.whatsapp.net", "admin": False}
            ],
        }

    def frame(event_id: str, member: str) -> str:
        value = json.loads(_frame(payload(member), event_id=event_id, kind=kind))
        value["eventKey"] = f"whatsapp:account-a:{CHAT}:{kind}:same-provider-identity"
        return json.dumps(value)

    async def capture_both() -> None:
        channel._reader_task = asyncio.current_task()
        channel._events_subscribed = True
        await _handle_with_inline_ack(channel, frame("membership-first", "123@lid"))
        await _handle_with_inline_ack(channel, frame("membership-conflict", "456@lid"))

    asyncio.run(capture_both())

    records = _records(tmp_path / "raw")
    assert [record["kind"] for record in records] == [kind, kind]
    assert acknowledgements == ["membership-first"]
    assert order == ["raw", "journal", "ack", "raw", "journal"]
    assert channel._bridge_intake_closed is True
    assert socket.closed is True
    assert store.count_events() == 1
    original = store.get_event("membership-first")
    assert original is not None and original.payload is not None
    assert original.payload["participants"][0].get("lid") == "123@lid"
    if kind == "membership_snapshot":
        assert len(issued) == len(recorded) == 1
        assert issued[0] is recorded[0]
    else:
        assert issued == recorded == []
    store.close()


def test_membership_copies_make_two_raw_lines_and_one_journal_event(tmp_path: Path) -> None:
    channel, _, store = _setup(tmp_path)
    shared_key = "whatsapp:account-a:chat@g.us:membership_change:stub%3Amessage-10"
    common = {
        "chatJid": CHAT,
        "action": "add",
        "changeId": "stub:message-10",
        "participants": [{"lid": "123@lid", "phoneJid": "49123@s.whatsapp.net"}],
        "actor": {"lid": "456@lid"},
    }
    frames = [
        {
            "version": PROTOCOL_VERSION,
            "type": "membership_change",
            "ts": NOW,
            "accountId": "account-a",
            "eventId": "stub-event-10",
            "eventKey": shared_key,
            "observedAt": NOW,
            "payload": {
                **common,
                "sourceCopyId": "stub:message-10",
                "messageId": "message-10",
                "stubType": 27,
                "providerTimestampMs": NOW,
            },
        },
        {
            "version": PROTOCOL_VERSION,
            "type": "membership_change",
            "ts": NOW + 1,
            "accountId": "account-a",
            "eventId": "update-event-10",
            "eventKey": shared_key,
            "observedAt": NOW + 1,
            "payload": {
                **common,
                "sourceCopyId": "update:copy-10",
                "providerTimestampMs": None,
            },
        },
    ]

    for frame in frames:
        asyncio.run(channel._handle_bridge_message(json.dumps(frame)))

    records = _records(tmp_path / "raw")
    assert [record["kind"] for record in records] == [
        "membership_change",
        "membership_change",
    ]
    assert [record["native"]["eventId"] for record in records] == [
        "stub-event-10",
        "update-event-10",
    ]
    assert [record["native"]["payload"]["sourceCopyId"] for record in records] == [
        "stub:message-10",
        "update:copy-10",
    ]
    assert records[0]["native"]["payload"]["messageId"] == "message-10"
    assert records[0]["native"]["payload"]["stubType"] == 27
    assert store.count_events() == 1
    event = store.get_event("stub-event-10")
    assert event is not None
    assert event.kind == "membership_change"
    assert event.event_key == shared_key


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


@pytest.mark.parametrize(
    ("media_kind", "expected_mode"),
    [("image", "description"), ("video", "description"), ("sticker", "description")],
)
def test_primary_media_description_is_archived(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, media_kind: str, expected_mode: str
) -> None:
    channel, archive, _ = _setup(tmp_path)
    path = tmp_path / "incoming" / "media.bin"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"media")
    channel.config.media.enabled = True
    if media_kind == "image":
        channel.config.media.describe_images = True
    elif media_kind == "video":
        channel.config.media.describe_videos = True
    elif media_kind == "sticker":
        channel.config.media.describe_stickers = True
    channel._model_router = type("Router", (), {
        "resolve": lambda self, task, channel: type("Profile", (), {"model": "vision-model"})()
    })()
    channel._vision_describer = type("Vision", (), {
        "describe": lambda self, path, profile: asyncio.sleep(0, result=" exact output "),
        "describe_video": lambda self, path, profile: asyncio.sleep(0, result=" exact output "),
    })()
    monkeypatch.setattr(whatsapp_module, "datetime", _FixedDateTime)
    event = InboundEvent(
        message_id="native-primary", chat_jid=CHAT, participant_jid="sender@lid", sender_id="sender",
        sender_phone_jid=None, is_group=True, text="caption", timestamp=NOW, mentioned_jids=[],
        mentioned_bot=False, reply_to_bot=False, reply_to_message_id=None, reply_to_participant=None,
        reply_to_text=None, media_kind=media_kind, media_type="image/jpeg", media_file_name="media.bin",
        media_path=str(path), media_bytes=5, media_description=None, voice_transcript=None,
    )

    result = asyncio.run(channel._enrich_primary_media_event(event))

    expected = {
        "derived_version": 1, "kind": "media_description", "channel": "whatsapp", "chat_id": CHAT,
        "native_message_id": "native-primary", "mode": expected_mode, "generator": "vision-model",
        "generated_ms": NOW, "text": " exact output ",
    }
    assert result.media_description == " exact output "
    assert [record for _, record, _ in iter_records(archive.root / "derived" / "media-descriptions.jsonl")] == [expected]


def test_quoted_image_description_uses_quoted_message_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    channel, archive, _ = _setup(tmp_path)
    path = tmp_path / "incoming" / "quoted.jpg"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"image")
    channel.config.media.enabled = True
    channel.config.media.describe_images = True
    channel._model_router = type("Router", (), {
        "resolve": lambda self, task, channel: type("Profile", (), {"model": "vision-model"})()
    })()
    channel._vision_describer = type("Vision", (), {
        "describe": lambda self, path, profile: asyncio.sleep(0, result="quote image")
    })()
    monkeypatch.setattr(whatsapp_module, "datetime", _FixedDateTime)
    event = InboundEvent(
        message_id="reply-native", chat_jid=CHAT, participant_jid="sender@lid", sender_id="sender",
        sender_phone_jid=None, is_group=True, text="reply", timestamp=NOW, mentioned_jids=[],
        mentioned_bot=False, reply_to_bot=False, reply_to_message_id="quoted-native", reply_to_participant=None,
        reply_to_text="[Image]", media_kind=None, media_type=None, media_file_name=None, media_path=None,
        media_bytes=None, media_description=None, voice_transcript=None, reply_to_media_kind="image",
        reply_to_media_type="image/jpeg", reply_to_media_path=str(path),
    )

    asyncio.run(channel._enrich_quoted_image_event(event))

    assert [record for _, record, _ in iter_records(archive.root / "derived" / "media-descriptions.jsonl")] == [{
        "derived_version": 1, "kind": "media_description", "channel": "whatsapp", "chat_id": CHAT,
        "native_message_id": "quoted-native", "mode": "description", "generator": "vision-model",
        "generated_ms": NOW, "text": "quote image",
    }]


def test_quoted_image_without_message_id_is_not_archived(tmp_path: Path) -> None:
    channel, archive, _ = _setup(tmp_path)
    path = tmp_path / "incoming" / "quoted.jpg"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"image")
    channel.config.media.enabled = True
    channel.config.media.describe_images = True

    class Router:
        def resolve(self, task, channel):
            return type("Profile", (), {"model": "vision-model"})()

    class Vision:
        calls = 0

        async def describe(self, path, profile):
            self.calls += 1
            return "quote image"

    channel._model_router = Router()
    vision = Vision()
    channel._vision_describer = vision
    event = InboundEvent(
        message_id="reply-native", chat_jid=CHAT, participant_jid="sender@lid", sender_id="sender",
        sender_phone_jid=None, is_group=True, text="reply", timestamp=NOW, mentioned_jids=[],
        mentioned_bot=False, reply_to_bot=False, reply_to_message_id=None, reply_to_participant=None,
        reply_to_text="[Image]", media_kind=None, media_type=None, media_file_name=None, media_path=None,
        media_bytes=None, media_description=None, voice_transcript=None, reply_to_media_kind="image",
        reply_to_media_type="image/jpeg", reply_to_media_path=str(path),
    )

    result = asyncio.run(channel._enrich_quoted_image_event(event))

    assert result.reply_to_text == "[Image]"
    assert vision.calls == 0
    assert not (archive.root / "derived" / "media-descriptions.jsonl").exists()


@pytest.mark.parametrize("fails", [False, True])
def test_failed_or_empty_description_is_not_archived(tmp_path: Path, fails: bool) -> None:
    channel, archive, _ = _setup(tmp_path)
    path = tmp_path / "incoming" / "media.jpg"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"image")
    channel.config.media.enabled = True
    channel.config.media.describe_images = True
    channel._model_router = type("Router", (), {
        "resolve": lambda self, task, channel: type("Profile", (), {"model": "vision-model"})()
    })()
    class Vision:
        async def describe(self, path, profile):
            if fails:
                raise RuntimeError("generation failed")
            return ""

    channel._vision_describer = Vision()
    event = InboundEvent(
        message_id="native-empty", chat_jid=CHAT, participant_jid="sender@lid", sender_id="sender",
        sender_phone_jid=None, is_group=True, text="", timestamp=NOW, mentioned_jids=[], mentioned_bot=False,
        reply_to_bot=False, reply_to_message_id=None, reply_to_participant=None, reply_to_text=None,
        media_kind="image", media_type="image/jpeg", media_file_name=None, media_path=str(path),
        media_bytes=5, media_description=None, voice_transcript=None,
    )
    asyncio.run(channel._enrich_primary_media_event(event))
    assert not list((archive.root / "derived").glob("*.jsonl"))


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
