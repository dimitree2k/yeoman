from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from telegram.ext import TypeHandler
from yeoman_gateway.bus.events import OutboundMessage
from yeoman_gateway.bus.queue import MessageBus
from yeoman_gateway.channels import telegram
from yeoman_gateway.channels.manager import build_raw_archive
from yeoman_gateway.channels.telegram import TelegramChannel, raw_event_for_update
from yeoman_shared.config.loader import convert_keys
from yeoman_shared.config.schema import Config, TelegramConfig
from yeoman_shared.raw_archive import writer as raw_writer
from yeoman_shared.raw_archive.purge import PurgeSelector, purge
from yeoman_shared.raw_archive.records import archive_files, iter_records
from yeoman_shared.raw_archive.writer import RawArchive, RawArchiveCapacityError, RawEvent

NOW = 1_790_000_000_000


class _Update:
    update_id = 42
    message = SimpleNamespace(chat_id=1001)

    def to_dict(self) -> dict:
        return {
            "update_id": 42,
            "message": {"date": datetime(2026, 10, 1, tzinfo=UTC), "text": "hi"},
        }


def _records(root: Path) -> list[dict]:
    return [record for path in archive_files(root) for _, record, _ in iter_records(path) if record]


def _stoppable_app(bot: object) -> tuple[SimpleNamespace, list[str]]:
    stopped: list[str] = []

    async def stop(name: str) -> None:
        stopped.append(name)

    app = SimpleNamespace(
        bot=bot,
        updater=SimpleNamespace(stop=lambda: stop("updater")),
        stop=lambda: stop("application"),
        shutdown=lambda: stop("shutdown"),
    )
    return app, stopped


def test_raw_event_for_update_keeps_the_whole_update() -> None:
    event = raw_event_for_update(_Update())
    assert event.channel == "telegram" and event.kind == "update" and event.direction == "in"
    assert event.native_id == "42" and event.chat_id == "1001"
    assert event.native["message"]["text"] == "hi"


def test_send_is_archived_as_request_and_result(tmp_path: Path) -> None:
    archive = RawArchive(
        tmp_path / "raw",
        spool=tmp_path / "spool",
        status_path=tmp_path / "s.json",
        clock=lambda: NOW,
    )
    channel = TelegramChannel(TelegramConfig(), MessageBus())

    async def send_message(**kwargs):
        return SimpleNamespace(message_id=7)

    channel._app = SimpleNamespace(bot=SimpleNamespace(send_message=send_message))  # type: ignore[assignment]
    channel.set_raw_archive(archive)
    asyncio.run(channel.send(OutboundMessage(channel="telegram", chat_id="1001", content="yo")))
    records = _records(tmp_path / "raw")
    assert [record["kind"] for record in records] == ["outbound_request", "outbound_result"]
    assert records[0]["native"]["content"] == "yo"
    assert records[1]["native_id"] == "7"
    assert records[0]["correlation_id"] == records[1]["correlation_id"]
    assert records[0]["correlation_id"]


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("YEOMAN_HOME", str(tmp_path))
    return tmp_path


def test_build_raw_archive_follows_config(home: Path) -> None:
    archive = build_raw_archive(Config())
    assert isinstance(archive, RawArchive)
    assert archive.root == home / "data" / "raw"
    disabled = Config.model_validate(convert_keys({"raw": {"enabled": False}}))
    assert build_raw_archive(disabled) is None


@pytest.mark.asyncio
async def test_inbound_capacity_stops_telegram_and_skips_queued_handlers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = TelegramChannel(TelegramConfig(), MessageBus())
    handled: list[str] = []
    channel._handle_message = lambda **kwargs: handled.append("handled")  # type: ignore[method-assign]
    app, stopped = _stoppable_app(SimpleNamespace())
    channel._app = app  # type: ignore[assignment]
    channel._running = True
    append_calls = 0

    async def full_archive(archive, event) -> None:
        nonlocal append_calls
        append_calls += 1
        raise RawArchiveCapacityError("full")

    monkeypatch.setattr(telegram, "append_async", full_archive)
    update = SimpleNamespace(
        update_id=1,
        message=SimpleNamespace(),
        effective_user=SimpleNamespace(),
        to_dict=lambda: {"update_id": 1},
    )

    await channel._on_message(update, None)
    await asyncio.sleep(0)
    await channel._on_message(update, None)

    assert append_calls == 1
    assert handled == []
    assert not channel._running
    assert stopped == ["updater", "application", "shutdown"]


@pytest.mark.asyncio
async def test_outbound_capacity_blocks_send(monkeypatch: pytest.MonkeyPatch) -> None:
    channel = TelegramChannel(TelegramConfig(), MessageBus())
    send_calls = 0

    async def send_message(**kwargs):
        nonlocal send_calls
        send_calls += 1
        return SimpleNamespace(message_id=7)

    app, stopped = _stoppable_app(SimpleNamespace(send_message=send_message))
    channel._app = app  # type: ignore[assignment]
    channel._running = True

    async def full_archive(archive, event) -> None:
        raise RawArchiveCapacityError("full")

    monkeypatch.setattr(telegram, "append_async", full_archive)
    with pytest.raises(RawArchiveCapacityError):
        await channel.send(OutboundMessage(channel="telegram", chat_id="1001", content="yo"))
    await asyncio.sleep(0)

    assert send_calls == 0
    assert stopped == ["updater", "application", "shutdown"]


@pytest.mark.asyncio
async def test_result_capacity_preserves_success_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = TelegramChannel(TelegramConfig(), MessageBus())
    send_calls = 0

    async def send_message(**kwargs):
        nonlocal send_calls
        send_calls += 1
        return SimpleNamespace(message_id=7)

    app, stopped = _stoppable_app(SimpleNamespace(send_message=send_message))
    channel._app = app  # type: ignore[assignment]
    channel._running = True
    append_calls = 0

    async def fail_result(archive, event) -> None:
        nonlocal append_calls
        append_calls += 1
        if event.kind == "outbound_result":
            raise RawArchiveCapacityError("full")

    monkeypatch.setattr(telegram, "append_async", fail_result)
    await channel.send(OutboundMessage(channel="telegram", chat_id="1001", content="yo"))
    await asyncio.sleep(0)

    assert append_calls == 2
    assert send_calls == 1
    assert stopped == ["updater", "application", "shutdown"]


@pytest.mark.asyncio
async def test_inbound_media_is_copied_and_linked_to_its_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    archive = RawArchive(
        tmp_path / "raw",
        spool=tmp_path / "spool",
        status_path=tmp_path / "s.json",
        clock=lambda: NOW,
    )
    channel = TelegramChannel(TelegramConfig(), MessageBus())

    class Download:
        async def download_to_drive(self, path: str) -> None:
            Path(path).write_bytes(b"telegram photo")

    class Bot:
        async def get_file(self, file_id: str) -> Download:
            return Download()

    message = SimpleNamespace(
        chat_id=1001,
        text=None,
        caption=None,
        photo=[SimpleNamespace(file_id="photo-1", mime_type="image/jpeg")],
        voice=None,
        audio=None,
        document=None,
        chat=SimpleNamespace(type="private"),
        entities=[],
        caption_entities=[],
        reply_to_message=None,
        message_id=9,
    )
    user = SimpleNamespace(id=22, username=None, first_name="A")
    update = SimpleNamespace(
        update_id=71,
        message=message,
        effective_user=user,
        to_dict=lambda: {"update_id": 71, "message": {"photo": ["photo-1"]}},
    )
    channel._app = SimpleNamespace(bot=Bot())  # type: ignore[assignment]
    channel.set_raw_archive(archive)
    channel._start_typing = lambda chat_id: None  # type: ignore[method-assign]

    async def handle_message(**kwargs) -> None:
        return None

    channel._handle_message = handle_message  # type: ignore[method-assign]
    context = SimpleNamespace()
    await channel._on_update(update, context)
    await channel._on_message(update, context)

    records = _records(tmp_path / "raw")
    assert [record["kind"] for record in records] == ["update", "media"]
    assert records[1]["native"]["file_id"] == "photo-1"
    media_path = tmp_path / "raw" / records[1]["media"]["path"]
    assert records[1]["media"]["stored"] is True
    assert media_path.read_bytes() == b"telegram photo"


@pytest.mark.asyncio
async def test_start_registers_raw_update_capture_before_command_handlers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = TelegramChannel(TelegramConfig(token="fake"), MessageBus())
    registered: list[tuple[int, object]] = []

    class App:
        bot = SimpleNamespace(
            get_me=lambda: _async_result(SimpleNamespace(id=1, username="bot")),
            set_my_commands=lambda commands: _async_result(None),
        )

        def __init__(self) -> None:
            self.updater = SimpleNamespace(start_polling=self.start_polling)

        def add_handler(self, handler, group=0) -> None:
            registered.append((group, handler))

        async def initialize(self) -> None:
            return None

        async def start(self) -> None:
            return None

        async def start_polling(self, **kwargs) -> None:
            channel._running = False

    class Builder:
        def token(self, token: str):
            return self

        def build(self) -> App:
            return App()

    monkeypatch.setattr(telegram.Application, "builder", lambda: Builder())
    await channel.start()

    assert registered[0][0] == -1
    assert isinstance(registered[0][1], TypeHandler)
    assert registered[0][1].callback == channel._on_update
    assert all(group == 0 for group, _ in registered[1:])


async def _async_result(value):
    return value


@pytest.mark.asyncio
async def test_command_update_and_reply_are_archived(monkeypatch: pytest.MonkeyPatch) -> None:
    channel = TelegramChannel(TelegramConfig(), MessageBus())
    events = []
    replies = []

    async def append(archive, event) -> None:
        events.append(event)

    async def reply_text(text: str, **kwargs):
        replies.append((text, kwargs))
        return SimpleNamespace(message_id=99)

    monkeypatch.setattr(telegram, "append_async", append)
    message = SimpleNamespace(chat_id=1001, message_id=42, reply_text=reply_text)
    update = SimpleNamespace(
        update_id=77,
        message=message,
        to_dict=lambda: {"update_id": 77, "message": {"text": "/help"}},
    )

    context = SimpleNamespace()
    await channel._on_update(update, context)
    await channel._on_help(update, context)

    assert [event.kind for event in events] == ["update", "outbound_request", "outbound_result"]
    assert events[0].native["message"]["text"] == "/help"
    assert events[1].native["text"] == replies[0][0]
    assert events[1].native["reply_to_message_id"] == 42
    assert events[2].native_id == "99"
    assert events[1].correlation_id == events[2].correlation_id
    assert events[1].correlation_id
    assert replies[0][1] == {"parse_mode": "HTML"}


@pytest.mark.asyncio
async def test_inbound_capacity_blocks_reset_side_effects_and_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    side_effects: list[str] = []
    replies: list[str] = []

    class Session:
        messages = ["existing"]

        def clear(self) -> None:
            side_effects.append("clear")

    class Sessions:
        def get_or_create(self, key: str) -> Session:
            side_effects.append("get")
            return Session()

        def save(self, session: Session) -> None:
            side_effects.append("save")

    channel = TelegramChannel(TelegramConfig(), MessageBus(), session_manager=Sessions())
    app, stopped = _stoppable_app(SimpleNamespace())
    channel._app = app  # type: ignore[assignment]
    channel._running = True

    async def full_archive(archive, event) -> None:
        raise RawArchiveCapacityError("full")

    monkeypatch.setattr(telegram, "append_async", full_archive)
    message = SimpleNamespace(
        chat_id=1001,
        reply_text=lambda text: replies.append(text),
    )
    update = SimpleNamespace(
        update_id=78,
        message=message,
        effective_user=SimpleNamespace(id=22),
        to_dict=lambda: {"update_id": 78, "message": {"text": "/reset"}},
    )

    context = SimpleNamespace()
    await channel._on_update(update, context)
    await channel._on_reset(update, context)
    await channel._capacity_stop_task

    assert side_effects == []
    assert replies == []
    assert stopped == ["updater", "application", "shutdown"]


@pytest.mark.asyncio
async def test_command_reply_capacity_blocks_send_and_preserves_sent_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = TelegramChannel(TelegramConfig(), MessageBus())
    app, stopped = _stoppable_app(SimpleNamespace())
    channel._app = app  # type: ignore[assignment]
    channel._running = True
    events = []
    replies = []

    async def archive_request(archive, event) -> None:
        events.append(event.kind)
        if event.kind == "outbound_request":
            raise RawArchiveCapacityError("full")

    async def blocked_reply_text(text: str, **kwargs):
        replies.append(text)
        return SimpleNamespace(message_id=1)

    monkeypatch.setattr(telegram, "append_async", archive_request)
    message = SimpleNamespace(chat_id=1001, message_id=42, reply_text=blocked_reply_text)
    update = SimpleNamespace(
        update_id=79,
        message=message,
        to_dict=lambda: {"update_id": 79, "message": {"text": "/help"}},
    )
    context = SimpleNamespace()
    await channel._on_update(update, context)
    await channel._on_help(update, context)
    await channel._capacity_stop_task

    assert events == ["update", "outbound_request"]
    assert replies == []
    assert stopped == ["updater", "application", "shutdown"]

    channel = TelegramChannel(TelegramConfig(), MessageBus())
    app, stopped = _stoppable_app(SimpleNamespace())
    channel._app = app  # type: ignore[assignment]
    channel._running = True
    events = []
    replies = []

    async def archive_result(archive, event) -> None:
        events.append(event.kind)
        if event.kind == "outbound_result":
            raise RawArchiveCapacityError("full")

    async def successful_reply_text(text: str, **kwargs):
        replies.append(text)
        return SimpleNamespace(message_id=2)

    monkeypatch.setattr(telegram, "append_async", archive_result)
    message = SimpleNamespace(chat_id=1001, message_id=42, reply_text=successful_reply_text)
    update = SimpleNamespace(
        update_id=80,
        message=message,
        to_dict=lambda: {"update_id": 80, "message": {"text": "/help"}},
    )
    context = SimpleNamespace()
    await channel._on_update(update, context)
    await channel._on_help(update, context)
    await channel._capacity_stop_task

    assert events == ["update", "outbound_request", "outbound_result"]
    assert len(replies) == 1
    assert stopped == ["updater", "application", "shutdown"]


@pytest.mark.asyncio
async def test_explicit_restart_drains_retained_event_and_resumes_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = RawArchive(
        tmp_path / "raw",
        spool=tmp_path / "spool",
        status_path=tmp_path / "s.json",
        clock=lambda: NOW,
    )
    channel = TelegramChannel(TelegramConfig(token="fake"), MessageBus())
    channel.set_raw_archive(archive)
    old_app, stopped = _stoppable_app(SimpleNamespace())
    channel._app = old_app  # type: ignore[assignment]
    channel._running = True

    def unavailable(*args, **kwargs):
        raise OSError("storage unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(raw_writer, "MAX_MEMORY_PENDING", 1)
        patch.setattr(raw_writer, "append_line", unavailable)
        patch.setattr(archive, "_spool_line_locked", lambda *args: False)
        await telegram.append_async(
            archive,
            raw_writer.RawEvent(
                channel="telegram", kind="retained", direction="in", native={"n": 1}
            ),
        )
        assert archive.status().pending_in_memory == 1

        update = SimpleNamespace(
            update_id=81,
            message=SimpleNamespace(chat_id=1001),
            to_dict=lambda: {"update_id": 81, "message": {"text": "/help"}},
        )
        await channel._on_update(update, None)
        assert channel._stopping_due_to_raw_archive_capacity
        assert archive.status().state == "blocked"

    await channel._capacity_stop_task
    assert channel._app is None
    assert stopped == ["updater", "application", "shutdown"]

    replied = []

    class App:
        def __init__(self) -> None:
            self.handlers = []
            self.bot = SimpleNamespace(
                get_me=lambda: _async_result(SimpleNamespace(id=1, username="bot")),
                set_my_commands=lambda commands: _async_result(None),
            )
            self.updater = SimpleNamespace(start_polling=self.start_polling)

        def add_handler(self, handler, group=0) -> None:
            self.handlers.append((group, handler))

        async def initialize(self) -> None:
            return None

        async def start(self) -> None:
            return None

        async def start_polling(self, **kwargs) -> None:
            channel._running = False

    new_app = App()

    class Builder:
        def token(self, token: str):
            return self

        def build(self) -> App:
            return new_app

    monkeypatch.setattr(telegram.Application, "builder", lambda: Builder())
    await channel.start()

    assert not channel._stopping_due_to_raw_archive_capacity
    assert new_app.handlers[0][0] == -1
    await channel._on_update(update, None)
    reply_update = SimpleNamespace(
        update_id=82,
        message=SimpleNamespace(
            chat_id=1001,
            message_id=81,
            reply_text=lambda text, **kwargs: _async_reply(replied, text),
        ),
        to_dict=lambda: {"update_id": 82, "message": {"text": "/help"}},
    )
    reply_context = SimpleNamespace()
    await channel._on_update(reply_update, reply_context)
    await channel._on_help(reply_update, reply_context)

    records = _records(tmp_path / "raw")
    assert [record["kind"] for record in records] == [
        "retained",
        "update",
        "update",
        "outbound_request",
        "outbound_result",
    ]
    assert archive.status().pending_in_memory == 0
    assert replied


async def _async_reply(replies: list[str], text: str):
    replies.append(text)
    return SimpleNamespace(message_id=10)


def test_telegram_purge_uses_effective_message_scope_and_removes_associated_media(
    tmp_path: Path,
) -> None:
    archive = RawArchive(
        tmp_path / "raw",
        spool=tmp_path / "spool",
        status_path=tmp_path / "status.json",
        clock=lambda: NOW,
    )

    def update(
        update_id: int, field: str, message_id: int, chat_id: int, *, reply_id: int | None = None
    ):
        native_message = {"message_id": message_id, "chat": {"id": chat_id}, "text": "synthetic"}
        if reply_id is not None:
            native_message["reply_to_message"] = {"message_id": reply_id}
        message = SimpleNamespace(
            message_id=message_id,
            chat_id=chat_id,
            chat=SimpleNamespace(id=chat_id),
        )
        return SimpleNamespace(
            update_id=update_id,
            **{field: message},
            to_dict=lambda: {"update_id": update_id, field: native_message},
        )

    normal = update(1001, "message", 7, 101)
    edited = update(1002, "edited_message", 7, 101)
    same_number_elsewhere = update(1003, "message", 7, 202)
    reply_to_target = update(1004, "message", 8, 101, reply_id=7)
    for item in (normal, edited, same_number_elsewhere, reply_to_target):
        archive.append(raw_event_for_update(item))
    archive.append(
        RawEvent(
            channel="telegram",
            kind="media",
            direction="in",
            native={"update_id": 1001, "file_id": "photo"},
            native_id="1001",
            chat_id="101",
            correlation_id="7",
            received_ms=NOW,
        )
    )

    result = purge(
        archive.root,
        PurgeSelector(channel="telegram", chat_id="101", native_id="7"),
        operator="dm",
        now_ms=NOW + 1,
    )
    assert result.removed_lines == 3
    records = [
        record
        for path in archive_files(archive.root)
        for _, record, _ in iter_records(path)
        if record
    ]
    # Purged lines stay in place as content-free tombstones so later refs keep their positions.
    assert [record for record in records if "purged_version" in record] == [{"purged_version": 1}] * 3
    remaining = [record for record in records if "purged_version" not in record]
    assert [(record["native_id"], record["chat_id"], record["kind"]) for record in remaining] == [
        ("1003", "202", "update"),
        ("1004", "101", "update"),
    ]


@pytest.mark.asyncio
async def test_reply_text_archives_correlated_request_and_result(tmp_path: Path) -> None:
    archive = RawArchive(
        tmp_path / "raw",
        spool=tmp_path / "spool",
        status_path=tmp_path / "s.json",
        clock=lambda: NOW,
    )
    channel = TelegramChannel(TelegramConfig(), MessageBus())

    async def reply_text(text: str, **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(message_id=88)

    message = SimpleNamespace(chat_id=1001, message_id=13, reply_text=reply_text)
    channel.set_raw_archive(archive)
    await channel._reply_text(SimpleNamespace(message=message), "synthetic reply")

    records = _records(archive.root)
    assert [record["kind"] for record in records] == ["outbound_request", "outbound_result"]
    assert records[0]["correlation_id"]
    assert records[0]["correlation_id"] == records[1]["correlation_id"]
