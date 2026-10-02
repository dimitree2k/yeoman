from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner
from yeoman_gateway.cli.commands import app
from yeoman_gateway.processing.signals import SignalJournalSink
from yeoman_gateway.processing.store import ProcessingStore
from yeoman_shared.raw_archive.records import archive_files, iter_records
from yeoman_shared.raw_archive.writer import RawArchive, RawEvent

runner = CliRunner()
NOW = 1_800_000_000_000
CHAT = "chat@g.us"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("YEOMAN_HOME", str(tmp_path))
    archive = RawArchive()
    for message_id in ("m1", "m2"):
        archive.append(
            RawEvent(
                channel="whatsapp",
                kind="message",
                direction="in",
                native={"type": "message", "payload": {"messageId": message_id}},
                native_id=f"evt-{message_id}",
                chat_id="c1",
            )
        )
    return tmp_path


def test_status_prints_state_and_counts(home: Path) -> None:
    result = runner.invoke(app, ["raw", "status", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["files"] == 1 and data["lines"] == 2 and data["started_ms"] > 0


def test_verify_exit_code_reflects_problems(home: Path) -> None:
    assert runner.invoke(app, ["raw", "verify"]).exit_code == 0
    [month_file] = archive_files(home / "data" / "raw")
    month_file.write_text(month_file.read_text().splitlines()[0] + "\n")
    result = runner.invoke(app, ["raw", "verify"])
    assert result.exit_code == 1
    assert "line_count_dropped" in result.output


def test_purge_requires_confirmation(home: Path) -> None:
    result = runner.invoke(
        app, ["raw", "purge", "--channel", "whatsapp", "--message", "m1"], input="n\n"
    )
    assert result.exit_code == 1
    assert "1 line" in result.output
    ids = [
        record["native_id"]
        for path in archive_files(home / "data" / "raw")
        for _, record, _ in iter_records(path)
        if record
    ]
    assert ids == ["evt-m1", "evt-m2"]


def test_purge_with_yes_removes_and_audits(home: Path) -> None:
    result = runner.invoke(
        app,
        ["raw", "purge", "--channel", "whatsapp", "--message", "m1", "--yes"],
    )
    assert result.exit_code == 0, result.output
    assert (home / "data" / "raw" / "AUDIT").is_file()
    ids = [
        record["native_id"]
        for path in archive_files(home / "data" / "raw")
        for _, record, _ in iter_records(path)
        if record
    ]
    assert ids == ["evt-m2"]


def test_purge_without_chat_or_message_is_refused(home: Path) -> None:
    result = runner.invoke(app, ["raw", "purge", "--channel", "whatsapp", "--yes"])
    assert result.exit_code == 2


def test_seed_dry_run_and_import_use_only_home_stores(home: Path) -> None:
    processing_db = home / "data" / "processing" / "processing.db"
    processing_db.parent.mkdir(parents=True)
    with sqlite3.connect(processing_db) as connection:
        connection.execute(
            "CREATE TABLE events (event_id, kind, channel, chat_id, direction, "
            "source_message_id, created_ms, account, payload_json)"
        )
        connection.execute(
            "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "history-event",
                "message",
                "whatsapp",
                CHAT,
                "in",
                "history-message",
                1_000,
                "",
                json.dumps({"text": "synthetic history"}),
            ),
        )

    dry_run = runner.invoke(app, ["raw", "seed", "--dry-run"])
    assert dry_run.exit_code == 0, dry_run.output
    dry_report = json.loads(dry_run.output)
    assert dry_report["per_source"]["journal"]["written"] == 1
    assert not (home / "data" / "raw" / "whatsapp" / "seed-journal.jsonl").exists()

    seeded = runner.invoke(app, ["raw", "seed"])
    assert seeded.exit_code == 0, seeded.output
    seed_file = home / "data" / "raw" / "whatsapp" / "seed-journal.jsonl"
    assert seed_file.is_file()
    assert json.loads(seed_file.read_text().splitlines()[0])["native_id"] == "history-message"


def test_rebuild_drill_compares_with_synthetic_live_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("YEOMAN_HOME", str(home))
    archive = RawArchive(clock=lambda: NOW)
    payload = {
        "chatJid": CHAT,
        "messageId": "m1",
        "senderId": "person@s.whatsapp.net",
        "text": "synthetic message",
    }
    frame = {
        "version": 1,
        "type": "message",
        "ts": NOW,
        "accountId": "account-a",
        "eventId": "e1",
        "eventKey": "key-e1",
        "observedAt": NOW,
        "payload": payload,
    }
    archive.append(
        RawEvent(
            channel="whatsapp",
            kind="message",
            direction="in",
            native=frame,
            native_id="e1",
            chat_id=CHAT,
            received_ms=NOW,
        )
    )
    live_db = home / "data" / "processing" / "processing.db"
    sink = SignalJournalSink(ProcessingStore(live_db), clock=lambda: NOW)
    sink.capture(
        "message",
        payload,
        event_id="e1",
        event_key="key-e1",
        account="account-a",
        observed_at_ms=NOW,
        strict=True,
    )

    target = tmp_path / "rebuilt"
    result = runner.invoke(
        app,
        [
            "raw",
            "rebuild-drill",
            "--channel",
            "whatsapp",
            "--chat",
            CHAT,
            "--target",
            str(target),
            "--compare-live",
        ],
    )
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["replayed"] == 1
    assert report["missing_vs_live"] == [] and report["extra_vs_live"] == []
    rebuilt = ProcessingStore(target / "data" / "processing" / "processing.db")
    assert rebuilt.get_event("e1") is not None
