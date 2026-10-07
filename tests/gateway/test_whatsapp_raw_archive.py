"""V1 spec §4.0: native WhatsApp frames and outbound commands reach the raw archive."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yeoman_gateway.channels.whatsapp as whatsapp_module
from yeoman_gateway.bus.queue import MessageBus
from yeoman_gateway.channels.whatsapp import InboundEvent, WhatsAppChannel
from yeoman_gateway.media.asr import ASRResult
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

    async def append_durable_inline(archive, event, source=None, *, kind="") -> bool:
        return archive is not None and archive.append_durable(event, source, kind=kind)

    monkeypatch.setattr("yeoman_gateway.channels.whatsapp.append_durable_async", append_durable_inline)


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
    append_raw = archive.append_durable
    append_event = store.append_event

    def record_raw(event, source=None, *, kind=""):
        order.append("raw")
        return append_raw(event, source, kind=kind)

    def record_journal(*args, **kwargs):
        order.append("journal")
        return append_event(*args, **kwargs)

    async def ack(command_type: str, payload: dict, timeout_seconds: float, **kwargs):
        order.append("ack")
        return {"acknowledged": True}

    archive.append_durable = record_raw  # type: ignore[method-assign]
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
    append_raw = archive.append_durable
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

    def record_raw(event, source=None, *, kind=""):
        order.append("raw")
        return append_raw(event, source, kind=kind)

    def record_journal(*args, **kwargs):
        order.append("journal")
        return append_event(*args, **kwargs)

    async def ack(command_type: str, payload: dict, timeout_seconds: float, **kwargs):
        del timeout_seconds, kwargs
        assert command_type == "ack_event"
        assert store.get_event("empty-roster") is not None
        order.append("ack")
        return {"acknowledged": True}

    archive.append_durable = record_raw  # type: ignore[method-assign]
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
    append_raw = archive.append_durable
    append_event = store.append_event

    def record_raw(event, source=None, *, kind=""):
        order.append("raw")
        return append_raw(event, source, kind=kind)

    def record_journal(*args, **kwargs):
        order.append("journal")
        return append_event(*args, **kwargs)

    archive.append_durable = record_raw  # type: ignore[method-assign]
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

    def capacity_error(event, source=None, *, kind=""):
        raise RawArchiveCapacityError("raw archive capacity reached")

    archive.append_durable = capacity_error  # type: ignore[method-assign]
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


def test_send_poll_is_archived_as_request_and_result(tmp_path: Path) -> None:
    provider_id = "poll-provider-1"
    channel, ws = _real_send(
        tmp_path,
        {"ok": True, "result": {"sent": {"to": CHAT, "providerMessageId": provider_id,
          "messageId": provider_id, "options": 2,
          "poll": {"name": "Lunch?", "values": ["Pizza", "Sushi"], "selectableCount": 1}}}},
    )
    payload = {"to": CHAT, "question": "Lunch?", "options": ["Pizza", "Sushi"]}

    asyncio.run(channel._send_command("send_poll", payload, timeout_seconds=2.0, token="secret-t"))

    records = _records(tmp_path / "raw")
    requests = [record for record in records if record["kind"] == "outbound_request"]
    results = [record for record in records if record["kind"] == "outbound_result"]
    assert len(requests) == len(results) == 1
    request, result = requests[0], results[0]
    assert request["native"]["type"] == "send_poll"
    assert request["native"]["payload"] == payload
    assert request["correlation_id"] == ws.sent[0]["requestId"]
    assert result["correlation_id"] == request["correlation_id"]
    assert result["native_id"] == provider_id


def test_all_side_effect_bridge_commands_are_archived() -> None:
    expected_content_commands = {
        "send_text",
        "send_media",
        "send_poll",
        "forward_message",
        "delete_message",
        "react",
    }
    expected_non_content_commands = {
        "presence_update",
        "list_groups",
        "login_start",
        "login_wait",
        "logout",
        "lookup_message",
        "subscribe_events",
        "ack_event",
        "health",
    }
    protocol_path = Path(__file__).parents[2] / "packages/bridge/src/protocol.ts"
    protocol_source = protocol_path.read_text()
    command_type = re.search(
        r"export type BridgeCommandType\s*=\s*(.*?);", protocol_source, re.DOTALL
    )
    assert command_type is not None
    protocol_commands = set(re.findall(r"'([^']+)'", command_type.group(1)))

    assert protocol_commands == expected_content_commands | expected_non_content_commands
    assert whatsapp_module.RAW_ARCHIVED_COMMANDS == expected_content_commands


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

    def capacity_error(event, source=None, *, kind=""):
        raise RawArchiveCapacityError("raw archive capacity reached")

    archive.append_durable = capacity_error  # type: ignore[method-assign]
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
    for command_type in (
        "presence_update",
        "lookup_message",
        "list_groups",
        "login_start",
        "login_wait",
        "logout",
        "subscribe_events",
        "ack_event",
        "health",
    ):
        asyncio.run(
            channel._send_command(command_type, {"to": CHAT}, 2.0, token="t")
        )
    assert _records(tmp_path / "raw") == []


@pytest.mark.parametrize("command,payload,result,provider_id", [
    ("send_poll", {"to": CHAT, "question": "Lunch?", "options": [" One ", "Two"]},
     {"sent": {"to": CHAT, "messageId": "POLL", "providerMessageId": "POLL", "options": 2,
      "poll": {"name": "Lunch?", "values": ["One", "Two"], "selectableCount": 1}}}, "POLL"),
    ("forward_message", {"to": CHAT, "sourceChatJid": CHAT, "sourceMessageId": "SOURCE"},
     {"forwarded": {"to": CHAT, "messageId": "FORWARD", "providerMessageId": "FORWARD", "content": {
        "text": "forwarded text", "caption": None, "media": None, "forwarded": True,
        "sourceChatJid": CHAT, "sourceMessageId": "SOURCE", "provenance": "sent"}}}, "FORWARD"),
    ("delete_message", {"chatJid": CHAT, "messageId": "TARGET"},
     {"deleted": {"chatJid": CHAT, "messageId": "TARGET"}}, ""),
    ("react", {"chatJid": CHAT, "messageId": "TARGET", "emoji": "x"},
     {"reacted": {"chatJid": CHAT, "messageId": "TARGET", "providerMessageId": "REACTION"}}, "REACTION"),
])
def test_outbound_normalized_result_fields_are_archived(tmp_path, command, payload, result, provider_id):
    channel, ws = _real_send(tmp_path, {"ok": True, "result": result})
    returned = asyncio.run(channel._send_command(command, payload, timeout_seconds=2.0, token="synthetic-token"))
    request, response = _records(tmp_path / "raw")
    assert returned == result
    assert request["native"]["payload"] == payload
    assert response["native"]["result"] == result
    assert request["correlation_id"] == response["correlation_id"] == ws.sent[0]["requestId"]
    assert response["native_id"] == provider_id  # delete target is not an outbound provider ID
    assert "synthetic-token" not in json.dumps([request, response])


@pytest.mark.asyncio
async def test_group_metadata_raw_before_ack_no_response(tmp_path):
    channel, archive, store = _setup(tmp_path)
    payload = {"chatJid": CHAT, "value": "", "snapshot": True, "observedAtMs": NOW}
    acknowledged = []
    published = []

    async def ack(command_type, body, timeout_seconds, **kwargs):
        assert command_type == "ack_event"
        records = _records(archive.root)
        assert any(r["native"]["payload"] == payload for r in records)
        acknowledged.append(body["eventId"])
        return {"acknowledged": True}

    async def publish(event):
        published.append(event)

    channel._send_command = ack
    channel._publish_event = publish
    try:
        await channel._handle_bridge_message(_frame(payload, kind="group_description"))
        await channel._drain_bridge_worker()
        assert acknowledged == ["evt-1"]
        assert published == [] and store.count_events() == 1
        await channel._handle_bridge_message(_frame(payload | {"value": None}, event_id="bad", kind="group_description"))
        assert acknowledged == ["evt-1"] and store.count_events() == 1
    finally:
        await channel.stop()
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["message", "group_subject"])
async def test_replay_withholds_ack_until_raw_is_durable(tmp_path, monkeypatch, kind):
    channel, archive, store = _setup(tmp_path)
    payload = (_message() if kind == "message" else
               {"chatJid": CHAT, "value": "change", "actorJid": "222@lid",
                "observedAtMs": NOW, "snapshot": False})
    acknowledged = []
    published = []

    async def ack(command_type, body, timeout_seconds, **kwargs):
        assert any(r["native"]["payload"] == payload for r in _records(archive.root))
        acknowledged.append(body["eventId"])
        return {"acknowledged": True}

    async def publish(event):
        published.append(event)

    channel._send_command = ack
    channel._publish_event = publish
    append_line = archive._append_archive_line
    spool_line = archive._spool_line_locked

    def failed_archive(*args, **kwargs):
        raise OSError("synthetic archive unavailable")

    monkeypatch.setattr(archive, "_append_archive_line", failed_archive)
    monkeypatch.setattr(archive, "_spool_line_locked", lambda *args, **kwargs: False)
    try:
        await channel._handle_bridge_message(_frame(payload, kind=kind))
        await channel._drain_bridge_worker()
        assert acknowledged == [] and published == []
        assert store.count_events() == 0  # journal is writable, but raw is not durable
        assert not archive._pending
        monkeypatch.setattr(archive, "_append_archive_line", append_line)
        monkeypatch.setattr(archive, "_spool_line_locked", spool_line)
        await channel._handle_bridge_message(_frame(payload, kind=kind))
        await _until(lambda: acknowledged == ["evt-1"])
        await channel._drain_bridge_worker()
        await channel._drain_debounce_projections()
        assert acknowledged == ["evt-1"] and store.count_events() == 1
        assert len(published) == (1 if kind == "message" else 0)
        assert not archive._pending
        assert len(_records(archive.root)) == 1
        if kind == "group_subject":
            assert _records(archive.root)[0]["native"]["payload"]["actorJid"] == "222@lid"
    finally:
        await channel.stop()
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["disabled", "legacy", "legacy_media"])
async def test_raw_disabled_and_legacy_archive_keep_journal_ack_order(tmp_path, monkeypatch, mode):
    channel, _, store = _setup(tmp_path)
    order = []
    warnings = []
    append_event = store.append_event

    class LegacyArchive:
        def append(self, event):
            order.append("raw")
            assert event.native_id in {"legacy-0", "legacy-1"}
            return False  # Old API's deferred result must not be treated as durability failure.

        def append_with_media(self, event, source, *, kind):
            assert kind == "document" and Path(source).is_file()
            return self.append(event)

    channel.set_raw_archive(None if mode == "disabled" else LegacyArchive())
    monkeypatch.setattr(whatsapp_module.logger, "warning", lambda message, *args: warnings.append(message))

    def journal(*args, **kwargs):
        order.append("journal")
        return append_event(*args, **kwargs)

    async def ack(command_type, body, timeout_seconds, **kwargs):
        assert command_type == "ack_event"
        assert store.get_event(body["eventId"]) is not None
        order.append("ack")
        return {"acknowledged": True}

    store.append_event = journal
    channel._send_command = ack
    payload = {"chatJid": CHAT, "value": "current", "observedAtMs": NOW, "snapshot": True}
    if mode == "legacy_media":
        source = tmp_path / "incoming" / "synthetic.txt"
        source.parent.mkdir(exist_ok=True)
        source.write_text("synthetic media")
        # Exercise legacy media through a message; metadata has no media field.
        payload = _message(media={"kind": "document", "path": str(source)})
    try:
        for index in range(2):
            current = payload if mode != "legacy_media" else payload | {"messageId": f"media-{index}"}
            await channel._handle_bridge_message(_frame(current, event_id=f"legacy-{index}",
                                                        kind="message" if mode == "legacy_media" else "group_subject"))
            await channel._drain_bridge_worker()
        assert order == (["journal", "ack"] if mode == "disabled" else ["raw", "journal", "ack"]) * 2
        assert store.count_events() == 2
        assert len(warnings) == (0 if mode == "disabled" else 1)
        assert channel._bridge_intake_closed is False
    finally:
        await channel.stop()
        store.close()


async def _until(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.005)


@pytest.mark.asyncio
async def test_raw_retry_recovery_is_single_publication(tmp_path, monkeypatch):
    channel, archive, store = _setup(tmp_path)
    channel._raw_retry_initial_seconds = 0.01
    channel._raw_retry_max_seconds = 0.04
    failed = True
    attempts = []
    original = archive._append_archive_line

    def write(*args):
        attempts.append(len(attempts))
        if failed:
            raise OSError("synthetic unavailable")
        return original(*args)

    monkeypatch.setattr(archive, "_append_archive_line", write)
    monkeypatch.setattr(archive, "_spool_line_locked", lambda *args: False)
    published = []
    acked = []
    async def ack(command, payload, **kwargs):
        acked.append(payload["eventId"])
        return {"acknowledged": True}
    async def project(work):
        published.append(work.event_id)
    channel._send_command = ack
    channel._project_bridge_event = project
    frame = _frame(_message())
    try:
        await channel._handle_bridge_message(frame)
        assert not channel._bridge_intake_closed
        assert not archive._pending
        await _until(lambda: len(attempts) >= 3)
        await channel._handle_bridge_message(frame)  # reuse retry entry
        assert acked == [] and store.count_events() == 0
        failed = False
        await _until(lambda: len(published) == 1)
        assert acked == ["evt-1"] and store.count_events() == 1
        assert len(_records(archive.root)) == 1
        assert not archive._pending
    finally:
        await channel.stop()
        store.close()


@pytest.mark.asyncio
async def test_raw_failure_preserves_another_inflight_ack(tmp_path, monkeypatch):
    channel, archive, store = _setup(tmp_path)
    ack_sent = asyncio.Event()
    ack_response = asyncio.Event()
    published = []
    acknowledged = []
    failed = True
    write = archive._append_archive_line
    def append(*args):
        if args[3]["native_id"] == "B" and failed:
            raise OSError("synthetic B unavailable")
        return write(*args)
    monkeypatch.setattr(archive, "_append_archive_line", append)
    monkeypatch.setattr(archive, "_spool_line_locked", lambda *args: False)
    channel._raw_retry_initial_seconds = 0.01
    channel._raw_retry_max_seconds = 0.04
    class Socket:
        closed = False
        async def close(self):
            self.closed = True
    channel._ws = Socket()
    async def ack(command, payload, **kwargs):
        event_id = payload["eventId"]
        acknowledged.append(event_id)  # Bridge durably deletes before responding.
        if event_id == "A":
            ack_sent.set()
            await ack_response.wait()
        return {"acknowledged": True}
    async def project(work):
        published.append(work.event_id)
    channel._send_command = ack
    channel._project_bridge_event = project
    first = asyncio.create_task(channel._handle_bridge_message(_frame(_message(), event_id="A")))
    try:
        await ack_sent.wait()
        await channel._handle_bridge_message(_frame(_message(messageId="b"), event_id="B"))
        assert channel._ws.closed is False and channel._bridge_intake_closed is False
        assert acknowledged == ["A"] and published == []
        ack_response.set()
        await first
        assert published == ["A"]
        failed = False
        await _until(lambda: published == ["A", "B"])
        assert acknowledged == ["A", "B"] and store.count_events() == 2
        assert len(_records(archive.root)) == 2
    finally:
        ack_response.set()
        await first
        await channel.stop()
        store.close()


@pytest.mark.asyncio
async def test_startup_raw_retry_keeps_subscription_healthy(tmp_path, monkeypatch):
    import websockets
    channel, archive, store = _setup(tmp_path)
    del channel._send_command
    channel._raw_retry_initial_seconds = 0.01
    channel._raw_retry_max_seconds = 0.04
    failures = []
    recovered = False
    write = archive._append_archive_line
    def append(*args):
        if not recovered:
            failures.append(1)
            raise OSError("synthetic unavailable")
        return write(*args)
    monkeypatch.setattr(archive, "_append_archive_line", append)
    monkeypatch.setattr(archive, "_spool_line_locked", lambda *args: False)
    repairs = []
    channel._runtime.ensure_ready = lambda **kwargs: None
    channel._runtime.repair_once = lambda: repairs.append(1)
    published = []
    acked = []
    async def project(work):
        published.append(work.event_id)
    channel._project_bridge_event = project
    async def bridge(socket):
        async for encoded in socket:
            command = json.loads(encoded)
            if command["type"] == "subscribe_events":
                await socket.send(_frame(_message(), event_id="startup"))
                result = {"subscribed": True}
            elif command["type"] == "health":
                result = {"protocolVersion": PROTOCOL_VERSION}
            else:
                assert command["type"] == "ack_event"
                acked.append(command["payload"]["eventId"])
                result = {"acknowledged": True}
            await socket.send(json.dumps({"version": PROTOCOL_VERSION, "type": "response",
                "requestId": command["requestId"], "payload": {"ok": True, "result": result}}))
    async with websockets.serve(bridge, "127.0.0.1", 0) as server:
        channel.config.bridge_host = "127.0.0.1"
        channel.config.bridge_port = server.sockets[0].getsockname()[1]
        channel.config.bridge_token = "synthetic"
        run = asyncio.create_task(channel.start())
        try:
            await _until(lambda: len(failures) >= 3 or run.done())
            assert not run.done() and channel._running and channel._connected
            assert repairs == [] and not channel._bridge_intake_closed
            assert acked == [] and store.count_events() == 0 and not archive._pending
            recovered = True
            await _until(lambda: published == ["startup"])
            assert acked == ["startup"] and store.count_events() == 1
            assert len(_records(archive.root)) == 1 and repairs == []
        finally:
            await channel.stop()
            await run
            store.close()


@pytest.mark.asyncio
async def test_raw_retry_capacity_sheds_and_replay_conflicts(tmp_path, monkeypatch):
    channel, archive, store = _setup(tmp_path)
    channel._raw_retry_maxsize = 1
    channel._raw_retry_initial_seconds = 0.01
    channel._raw_retry_max_seconds = 0.04
    failed = True
    write = archive._append_archive_line
    def append(*args):
        if failed:
            raise OSError("synthetic unavailable")
        return write(*args)
    monkeypatch.setattr(archive, "_append_archive_line", append)
    monkeypatch.setattr(archive, "_spool_line_locked", lambda *args: False)
    first = _frame(_message(), event_id="A")
    second = None
    try:
        await channel._handle_bridge_message(first)
        assert len(channel._bridge_raw_retries) == 1
        await channel._handle_bridge_message(first)
        await channel._handle_bridge_message(_frame(_message("conflict"), event_id="A"))
        assert len(channel._bridge_raw_retries) == 1 and store.count_events() == 0
        second = asyncio.create_task(channel._handle_bridge_message(_frame(_message(messageId="b"), event_id="B")))
        await asyncio.sleep(0.05)
        assert second.done() and len(channel._bridge_raw_retries) == 1
        assert channel._raw_shed_count == 1 and "B" not in channel._bridge_inflight
        assert not channel._bridge_intake_closed
        failed = False
        await second
        await _until(lambda: store.count_events() == 1)
        await channel._handle_bridge_message(_frame(_message(messageId="b"), event_id="B"))
        await _until(lambda: store.count_events() == 2)
        await channel._drain_bridge_worker()
        assert len(_records(archive.root)) == 2
        assert not channel._bridge_raw_retries and not archive._pending
    finally:
        if second is not None and not second.done():
            second.cancel()
            await asyncio.gather(second, return_exceptions=True)
        await channel.stop()
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery", ["live", "replay"])
async def test_saturation_reads_delayed_ack_and_replays_shed_after_recovery(tmp_path, monkeypatch, delivery):
    import websockets
    channel, archive, store = _setup(tmp_path)
    del channel._send_command
    channel._raw_retry_maxsize = 1
    channel._raw_retry_initial_seconds = 0.01
    channel._raw_retry_max_seconds = 0.04
    monkeypatch.setattr(whatsapp_module, "BRIDGE_ACK_TIMEOUT_SECONDS", 0.2)
    channel._runtime.ensure_ready = lambda **kwargs: None
    channel._runtime.repair_once = lambda: pytest.fail("healthy Bridge repaired")
    failed = delivery == "replay"
    write = archive._append_archive_line
    def append(*args):
        if failed and args[3]["native_id"] != "A":
            raise OSError("synthetic storage unavailable")
        return write(*args)
    monkeypatch.setattr(archive, "_append_archive_line", append)
    monkeypatch.setattr(archive, "_spool_line_locked", lambda *args: False)
    pending = {name: _frame(_message(messageId=name), event_id=name) for name in "ABC"}
    projected = []
    subscriptions = []
    a_reply_sent = asyncio.Event()
    async def project(work):
        projected.append(work.event_id)
    channel._project_bridge_event = project
    async def bridge(socket):
        nonlocal failed
        async for encoded in socket:
            command = json.loads(encoded)
            kind = command["type"]
            if kind == "health":
                result = {"protocolVersion": PROTOCOL_VERSION}
            elif kind == "subscribe_events":
                subscriptions.append(1)
                result = {"subscribed": True}
            else:
                assert kind == "ack_event"
                name = command["payload"]["eventId"]
                pending.pop(name)  # deletion is durable before the delayed response
                if name == "A":
                    failed = True
                    if delivery == "live":
                        await socket.send(pending["B"])
                        await socket.send(pending["C"])
                    # The reply reaches the wire after saturation, independently of
                    # the new shedding implementation (the old reader must fail).
                    await _until(lambda: len(channel._bridge_raw_retries) == 1)
                    await asyncio.sleep(0.02)
                result = {"acknowledged": True}
            await socket.send(json.dumps({"version": PROTOCOL_VERSION, "type": "response",
                "requestId": command["requestId"], "payload": {"ok": True, "result": result}}))
            if kind == "ack_event" and name == "A":
                a_reply_sent.set()
            if kind == "subscribe_events":
                if delivery == "live":
                    await _until(lambda: channel._connected and not channel._events_subscription_pending)
                first = ([pending[name] for name in ("A" if delivery == "live" else "BCA")]
                         if len(subscriptions) == 1 else [])
                for frame in (first if len(subscriptions) == 1 else list(pending.values())):
                    await socket.send(frame)
    async with websockets.serve(bridge, "127.0.0.1", 0) as server:
        channel.config.bridge_host = "127.0.0.1"
        channel.config.bridge_port = server.sockets[0].getsockname()[1]
        channel.config.bridge_token = "synthetic"
        run = asyncio.create_task(channel.start())
        try:
            await _until(lambda: (projected == ["A"] and a_reply_sent.is_set()) or run.done())
            assert projected == ["A"] and channel._connected
            await asyncio.sleep(0.3)  # storage outage exceeds A's ACK budget
            assert projected == ["A"] and channel._connected and not channel._bridge_intake_closed
            assert len(channel._bridge_raw_retries) == 1 and channel._raw_shed_count == 1
            assert "C" not in channel._bridge_inflight and set(pending) == {"B", "C"}
            failed = False
            await _until(lambda: len(projected) == 3)
            assert projected == ["A", "B", "C"] and len(subscriptions) == 2
            assert not pending and store.count_events() == 3 and len(_records(archive.root)) == 3
        finally:
            failed = False
            await channel.stop()
            await run
            store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("purge_mode", [None, "chat", "message"])
async def test_stop_settles_raw_thread_and_journals_without_ack(tmp_path, monkeypatch, purge_mode):
    import threading
    channel, archive, store = _setup(tmp_path)
    channel._raw_retry_initial_seconds = 0.01
    channel._raw_retry_max_seconds = 0.04
    entered = threading.Event()
    release = threading.Event()
    mode = "fail"
    write = archive._append_archive_line
    def append(*args):
        if mode == "fail":
            raise OSError("synthetic storage unavailable")
        if mode == "barrier":
            entered.set()
            assert release.wait(5)
        return write(*args)
    monkeypatch.setattr(archive, "_append_archive_line", append)
    monkeypatch.setattr(archive, "_spool_line_locked", lambda *args: False)
    acked = []
    projected = []
    async def ack(command, payload, **kwargs):
        acked.append(payload["eventId"])
        return {"acknowledged": True}
    async def project(work):
        projected.append(work.event_id)
    channel._send_command = ack
    channel._project_bridge_event = project
    stop = None
    try:
        await channel._handle_bridge_message(_frame(_message()))
        mode = "barrier"
        assert await asyncio.to_thread(entered.wait, 5)
        stop = asyncio.create_task(channel.stop())
        await asyncio.sleep(0.05)
        assert not stop.done() and acked == [] and projected == []
        release.set()
        await stop
        assert store.count_events() == 1 and len(_records(archive.root)) == 1
        assert acked == [] and projected == [] and not channel._bridge_raw_writes
        before = _records(archive.root)
        await asyncio.sleep(0.05)
        assert _records(archive.root) == before  # no archive mutation after normal stop
        if purge_mode:
            from yeoman_shared.raw_archive.purge import PurgeSelector, purge
            selector = PurgeSelector(channel="whatsapp", chat_id=CHAT,
                                     native_id="m-1" if purge_mode == "message" else None,
                                     before_ms=NOW + 100 if purge_mode == "message" else None)
            purge(archive.root, selector, operator="synthetic", now_ms=NOW + 100)
            assert _records(archive.root) == [{"purged_version": 1}]
            archive._clock = lambda: NOW + 200
        mode = "success"
        channel._stopping = False
        channel._bridge_intake_closed = False
        await channel._handle_bridge_message(_frame(_message()))
        await channel._drain_bridge_worker()
        assert store.count_events() == 1 and acked == ["evt-1"] and projected == ["evt-1"]
        if purge_mode:
            from yeoman_gateway.history.project import project as project_history
            assert _records(archive.root) == [{"purged_version": 1}]  # durable suppression permits ACK
            # Another restart/replay must still suppress the observation.
            await channel.stop()
            channel._stopping = False
            channel._bridge_intake_closed = False
            await channel._handle_bridge_message(_frame(_message()))
            await channel._drain_bridge_worker()
            assert _records(archive.root) == [{"purged_version": 1}]
            fresh = json.loads(_frame(_message("new content", messageId="new"), event_id="evt-new"))
            fresh["observedAt"] = NOW + 300
            fresh["payload"]["timestamp"] = 1  # provider occurrence is not capture time
            await channel._handle_bridge_message(json.dumps(fresh))
            await channel._drain_bridge_worker()
            rows = _records(archive.root)
            assert rows[0] == {"purged_version": 1} and len(rows) == 2
            assert rows[1]["received_ms"] == NOW + 300
            assert "hello" not in next((archive.root / "whatsapp").glob("*.jsonl")).read_text()
            db = tmp_path / "history.db"
            assert project_history([archive.root], db)["messages"] == 1
            import sqlite3
            with sqlite3.connect(db) as conn:
                assert conn.execute("SELECT native_message_id, text FROM messages").fetchall() == [("new", "new content")]
            assert store.count_events() == 2
            # This mock counts dispatch attempts; canonical journal/history assertions above prove uniqueness.
            assert projected == acked == ["evt-1", "evt-1", "evt-new"]
        else:
            assert len(_records(archive.root)) == 2  # accepted append-only copy across stop/replay
    finally:
        release.set()
        if stop is not None:
            await stop
        await channel.stop()
        store.close()


@pytest.mark.parametrize("quoted", [False, True])
@pytest.mark.parametrize("outcome", ["success", "empty", "whitespace", "failed", "volatile", "capacity", "spool"])
def test_transcript_retained_before_audio_unlink(tmp_path, monkeypatch, quoted, outcome):
    from yeoman_shared.raw_archive import writer as writer_module

    channel, archive, store = _setup(tmp_path)
    path = tmp_path / "incoming" / "voice.ogg"
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(b"audio")
    channel.config.media.enabled = True
    channel.config.media.transcribe_audio = True
    channel.config.media.delete_audio_after_transcription = True
    channel._model_router = type("Router", (), {
        "resolve": lambda self, task, channel: type("Profile", (), {"model": "asr-model"})()
    })()

    async def transcribe(path, profile):
        if outcome == "failed":
            raise RuntimeError("synthetic ASR failure")
        text = {"empty": "", "whitespace": "   "}.get(outcome, " exact transcript ")
        return ASRResult(text=text, model="executed-model") if text else None

    channel._asr_transcriber.transcribe = transcribe
    monkeypatch.setattr(whatsapp_module, "datetime", _FixedDateTime)
    event = InboundEvent(
        message_id="current", chat_jid=CHAT, participant_jid="sender@lid", sender_id="sender",
        sender_phone_jid=None, is_group=True, text="native caption", timestamp=NOW,
        mentioned_jids=[], mentioned_bot=False, reply_to_bot=False,
        reply_to_message_id="source" if quoted else None, reply_to_participant=None,
        reply_to_text=None, media_kind=None if quoted else "audio", media_type="audio/ogg",
        media_file_name="voice.ogg", media_path=None if quoted else str(path), media_bytes=5,
        media_description=None, voice_transcript=None,
        reply_to_media_kind="audio" if quoted else None,
        reply_to_media_path=str(path) if quoted else None,
    )
    target = archive.root / "derived/media-transcripts.jsonl"
    if outcome in {"volatile", "capacity", "spool"}:
        def fail_append(*args, **kwargs):
            raise OSError("synthetic disk failure")
        monkeypatch.setattr(writer_module, "append_line", fail_append)
        if outcome != "spool":
            archive.spool.write_text("not a directory")
        monkeypatch.setattr(writer_module, "MAX_MEMORY_PENDING", 1)
        if outcome == "capacity":
            assert archive.append_media_transcript({
                "kind": "media_transcript", "channel": "whatsapp", "chat_id": CHAT,
                "native_message_id": "older", "generated_ms": NOW, "text": "older transcript",
            }) is False

    real_unlink = Path.unlink
    def unlink(audio, *args, **kwargs):
        if audio == path:
            if outcome == "spool":
                assert list(archive.spool.glob("*.json"))
            else:
                assert target.exists()
                assert list(iter_records(target))[0][1]["text"] == " exact transcript "
        return real_unlink(audio, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", unlink)
    try:
        if outcome in {"volatile", "capacity"}:
            with pytest.raises(RawArchiveCapacityError if outcome == "capacity" else OSError):
                asyncio.run(channel._enrich_media_event(event))
            assert path.exists()
            assert event.text == "native caption"
            return
        result = asyncio.run(channel._enrich_media_event(event))
        if outcome in {"failed", "empty", "whitespace"}:
            assert path.exists() and result == event
            assert not target.exists() and not list(archive.spool.glob("*.json"))
            return
        assert not path.exists()
        if quoted:
            assert result.text == "native caption" and result.voice_transcript is None
            assert " exact transcript " in result.reply_to_text
        else:
            assert result.voice_transcript == " exact transcript "
        if outcome == "spool":
            record = json.loads(next(archive.spool.glob("*.json")).read_text())["line"]
            record = json.loads(record)
        else:
            record = list(iter_records(target))[0][1]
        assert record == {
            "raw_archive_version": 1, "kind": "media_transcript", "provenance": "derived_only",
            "channel": "whatsapp", "chat_id": CHAT,
            "native_message_id": "source" if quoted else "current",
             "generated_ms": NOW, "generator": "executed-model", "text": " exact transcript ",
        }
    finally:
        store.close()
