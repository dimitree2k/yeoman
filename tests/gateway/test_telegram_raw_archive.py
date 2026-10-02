from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from yeoman_gateway.bus.events import OutboundMessage
from yeoman_gateway.bus.queue import MessageBus
from yeoman_gateway.channels import telegram
from yeoman_gateway.channels.manager import build_raw_archive
from yeoman_gateway.channels.telegram import TelegramChannel, raw_event_for_update
from yeoman_shared.config.loader import convert_keys
from yeoman_shared.config.schema import Config, TelegramConfig
from yeoman_shared.raw_archive.records import archive_files, iter_records
from yeoman_shared.raw_archive.writer import RawArchive, RawArchiveCapacityError

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
        tmp_path / "raw", spool=tmp_path / "spool", status_path=tmp_path / "s.json", clock=lambda: NOW
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
async def test_result_capacity_preserves_success_without_retry(monkeypatch: pytest.MonkeyPatch) -> None:
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
        tmp_path / "raw", spool=tmp_path / "spool", status_path=tmp_path / "s.json", clock=lambda: NOW
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
    await channel._on_message(update, None)

    records = _records(tmp_path / "raw")
    assert [record["kind"] for record in records] == ["update", "media"]
    assert records[1]["native"]["file_id"] == "photo-1"
    media_path = tmp_path / "raw" / records[1]["media"]["path"]
    assert records[1]["media"]["stored"] is True
    assert media_path.read_bytes() == b"telegram photo"
