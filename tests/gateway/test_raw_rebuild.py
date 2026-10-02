"""S25/S27: the raw archive alone recreates a chat's journal; deletes and suppressions hold."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from yeoman_gateway.bus.queue import MessageBus
from yeoman_gateway.channels.whatsapp import WhatsAppChannel
from yeoman_gateway.processing.signals import SignalJournalSink
from yeoman_gateway.processing.store import ProcessingStore
from yeoman_gateway.storage.raw_rebuild import rebuild_chat
from yeoman_shared.config.schema import WhatsAppConfig
from yeoman_shared.raw_archive.verify import append_suppression
from yeoman_shared.raw_archive.writer import RawArchive
from yeoman_shared.whatsapp_protocol import PROTOCOL_VERSION

NOW = 1_790_000_000_000
CHAT = "chat@g.us"


def _frame(kind: str, payload: dict, event_id: str) -> str:
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
        }
    )


def _live(tmp_path: Path) -> tuple[WhatsAppChannel, ProcessingStore]:
    store = ProcessingStore(tmp_path / "live" / "processing.db")
    channel = WhatsAppChannel(WhatsAppConfig(), MessageBus())
    channel.set_processing_signals(SignalJournalSink(store, clock=lambda: NOW))
    channel.set_raw_archive(
        RawArchive(
            tmp_path / "raw",
            spool=tmp_path / "spool",
            status_path=tmp_path / "run" / "s.json",
            clock=lambda: NOW,
        )
    )

    async def ack(command_type: str, payload: dict, timeout_seconds: float, **kwargs):
        return {"acknowledged": True}

    channel._send_command = ack  # type: ignore[method-assign]
    channel._archive_inbound_event = lambda event: None  # type: ignore[method-assign]

    async def publish(event):
        return None

    channel._publish_event = publish  # type: ignore[method-assign]
    return channel, store


def _feed(channel: WhatsAppChannel, frames: list[str]) -> None:
    async def run() -> None:
        for frame in frames:
            await channel._handle_bridge_message(frame)

    asyncio.run(run())


def _msg(message_id: str, text: str, chat: str = CHAT) -> dict:
    return {
        "chatJid": chat,
        "messageId": message_id,
        "senderId": "4915@s.whatsapp.net",
        "text": text,
    }


def test_rebuild_recreates_the_journal_events_of_one_chat(tmp_path: Path) -> None:
    channel, _ = _live(tmp_path)
    _feed(
        channel,
        [
            _frame("message", _msg("m1", "one"), "e1"),
            _frame("message", _msg("m2", "two"), "e2"),
            _frame("message", _msg("x1", "other chat", chat="other@g.us"), "e3"),
        ],
    )
    report = rebuild_chat(
        tmp_path / "raw",
        channel="whatsapp",
        chat_id=CHAT,
        target_home=tmp_path / "rebuilt",
        live_processing_db=tmp_path / "live" / "processing.db",
    )
    assert report.replayed == 2
    assert report.missing_vs_live == () and report.extra_vs_live == ()
    rebuilt = ProcessingStore(tmp_path / "rebuilt" / "data" / "processing" / "processing.db")
    assert rebuilt.get_event("e1") is not None and rebuilt.get_event("e2") is not None
    assert rebuilt.get_event("e3") is None


def test_replayed_duplicate_frames_rebuild_once(tmp_path: Path) -> None:
    channel, _ = _live(tmp_path)
    frame = _frame("message", _msg("m1", "one"), "e1")
    _feed(channel, [frame])
    restarted, _ = _live(tmp_path)
    _feed(restarted, [frame])
    report = rebuild_chat(
        tmp_path / "raw", channel="whatsapp", chat_id=CHAT, target_home=tmp_path / "rebuilt"
    )
    assert report.lines_read == 2 and report.replayed == 1 and report.duplicates == 1


def test_delete_for_everyone_keeps_the_original_and_rebuilds_the_delete(tmp_path: Path) -> None:
    channel, _ = _live(tmp_path)
    _feed(
        channel,
        [
            _frame("message", _msg("m1", "regret"), "e1"),
            _frame(
                "delete",
                {"chatJid": CHAT, "messageId": "m1", "senderId": "4915@s.whatsapp.net"},
                "e2",
            ),
        ],
    )
    raw_text = "".join(p.read_text() for p in (tmp_path / "raw" / "whatsapp").glob("*.jsonl"))
    assert "regret" in raw_text
    report = rebuild_chat(
        tmp_path / "raw", channel="whatsapp", chat_id=CHAT, target_home=tmp_path / "rebuilt"
    )
    rebuilt = ProcessingStore(tmp_path / "rebuilt" / "data" / "processing" / "processing.db")
    assert report.replayed == 2
    assert rebuilt.get_event("e2") is not None


def test_suppressed_lines_are_not_rebuilt(tmp_path: Path) -> None:
    channel, _ = _live(tmp_path)
    _feed(
        channel,
        [
            _frame("message", _msg("m1", "forget me"), "e1"),
            _frame("message", _msg("m2", "keep"), "e2"),
        ],
    )
    append_suppression(
        tmp_path / "raw",
        channel="whatsapp",
        chat_id=CHAT,
        native_id="e1",
        reason="forget",
        now_ms=NOW,
    )
    report = rebuild_chat(
        tmp_path / "raw", channel="whatsapp", chat_id=CHAT, target_home=tmp_path / "rebuilt"
    )
    assert report.suppressed == 1 and report.replayed == 1


def test_rebuild_refuses_the_live_home_and_an_existing_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("YEOMAN_HOME", str(tmp_path / "home"))
    with pytest.raises(RuntimeError, match="live"):
        rebuild_chat(
            tmp_path / "raw", channel="whatsapp", chat_id=CHAT, target_home=tmp_path / "home"
        )
    with pytest.raises(RuntimeError, match="live"):
        rebuild_chat(
            tmp_path / "raw",
            channel="whatsapp",
            chat_id=CHAT,
            target_home=tmp_path / "home" / "rebuild",
        )
    existing = tmp_path / "other" / "data" / "processing"
    existing.mkdir(parents=True)
    (existing / "processing.db").write_text("")
    with pytest.raises(RuntimeError, match="empty"):
        rebuild_chat(
            tmp_path / "raw", channel="whatsapp", chat_id=CHAT, target_home=tmp_path / "other"
        )


def test_rebuild_refuses_journal_symlink_into_live_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live_home = tmp_path / "home"
    live_home.mkdir()
    monkeypatch.setenv("YEOMAN_HOME", str(live_home))
    target_home = tmp_path / "target"
    target_home.mkdir()
    live_redirect = live_home / "rebuild-data"
    live_redirect.mkdir()
    (target_home / "data").symlink_to(live_redirect, target_is_directory=True)
    live_journal = live_redirect / "processing" / "processing.db"

    with pytest.raises(RuntimeError, match="live"):
        rebuild_chat(tmp_path / "raw", channel="whatsapp", chat_id=CHAT, target_home=target_home)

    assert not live_journal.exists()


def test_rebuild_refuses_journal_symlink_outside_target_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("YEOMAN_HOME", str(tmp_path / "home"))
    outside = tmp_path / "outside"
    outside.mkdir()
    target_home = tmp_path / "target"
    target_home.mkdir()
    (target_home / "data").symlink_to(outside, target_is_directory=True)
    redirected_journal = outside / "processing" / "processing.db"

    with pytest.raises(RuntimeError, match="target_home"):
        rebuild_chat(tmp_path / "raw", channel="whatsapp", chat_id=CHAT, target_home=target_home)

    assert not redirected_journal.exists()


def test_live_comparison_detects_missing_events_when_archive_has_no_records(tmp_path: Path) -> None:
    channel, _ = _live(tmp_path)
    channel.set_raw_archive(None)
    _feed(channel, [_frame("message", _msg("m1", "not archived"), "e1")])

    report = rebuild_chat(
        tmp_path / "raw",
        channel="whatsapp",
        chat_id=CHAT,
        target_home=tmp_path / "rebuilt",
        live_processing_db=tmp_path / "live" / "processing.db",
    )

    assert report.lines_read == 0
    assert report.missing_vs_live == ("e1",)
