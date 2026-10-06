from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from pathlib import Path

import pytest
from typer.testing import CliRunner
from yeoman_gateway.cli.commands import app
from yeoman_gateway.processing.signals import SignalJournalSink
from yeoman_gateway.processing.store import ProcessingStore
from yeoman_shared.raw_archive.records import archive_files, iter_records
from yeoman_shared.raw_archive.writer import RawArchive, RawEvent
from yeoman_shared.utils.helpers import get_operational_store_path

runner = CliRunner()
NOW = 1_800_000_000_000
CHAT = "chat@g.us"
SECRET_TEXT = "SECRET_PAYLOAD_should_never_print"
SECRET_ID = "SECRET_IDENTIFIER_should_never_print"


def _capture_home(
    home: Path,
    *,
    effects: Iterable[tuple[str, str, str, str]] = (),
    receipts: Iterable[tuple[str, str, str, str, str]] = (),
    raw: Iterable[tuple[str, str, str, str, str | None]] = (),
    start: bool = True,
) -> list[Path]:
    """Create local capture inputs: effect(id, channel, chat, kind), receipt(id, effect, channel, chat, provider)."""
    archive = RawArchive(clock=lambda: NOW)
    paths: list[Path] = []
    for kind, channel, chat_id, correlation, provider_id in raw:
        native = (
            {"type": "send_text", "requestId": correlation, "payload": {"text": SECRET_TEXT}}
            if kind == "outbound_request" and channel == "whatsapp"
            else {"requestId": correlation, "result": {"providerMessageId": provider_id}}
            if kind == "outbound_result" and channel == "whatsapp"
            else {"content": SECRET_TEXT}
            if kind == "outbound_request"
            else {"message_id": provider_id}
        )
        archive.append(RawEvent(
            channel=channel, kind=kind, direction="out", native=native,
            native_id=provider_id or "", chat_id=chat_id, correlation_id=correlation,
        ))
    raw_root = home / "data" / "raw"
    if not start:
        (raw_root / "START").unlink()
    for path in raw_root.rglob("*"):
        if path.is_file():
            paths.append(path)

    db_path = get_operational_store_path("processing", data_dir=home / "data")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as connection:
        connection.executescript("""
            CREATE TABLE effects (
                effect_id TEXT, payload_kind TEXT, state TEXT, target_json TEXT, created_ms INTEGER
            );
            CREATE TABLE transport_receipts (
                receipt_id TEXT, effect_id TEXT, channel TEXT, chat_id TEXT,
                provider_message_id TEXT, confirmed_ms INTEGER
            );
        """)
        for effect_id, channel, chat_id, payload_kind in effects:
            connection.execute(
                "INSERT INTO effects VALUES (?, ?, 'sent', ?, ?)",
                (effect_id, payload_kind, json.dumps({"channel": channel, "chat_id": chat_id}), NOW),
            )
        connection.executemany(
            "INSERT INTO transport_receipts VALUES (?, ?, ?, ?, ?, ?)",
            [(*receipt, NOW) for receipt in receipts],
        )
    return [path for path in home.rglob("*") if path.is_file()]


def _input_bytes(paths: list[Path]) -> dict[Path, bytes]:
    return {path: path.read_bytes() for path in paths}


def _assert_aggregate_output(output: str) -> None:
    assert SECRET_TEXT not in output
    assert SECRET_ID not in output
    assert "secret-chat" not in output


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


def test_zero_match_purge_still_confirms_and_records_disposition(home: Path) -> None:
    result = runner.invoke(
        app,
        ["raw", "purge", "--channel", "whatsapp", "--chat", "never-seen", "--message", "missing"],
        input="y\n",
    )
    assert result.exit_code == 0, result.output
    assert "Permanently apply this purge disposition?" in result.output
    assert "0 archived lines removed" in result.output
    audit_path = home / "data" / "raw" / "AUDIT"
    [audit] = [record for _, record, _ in iter_records(audit_path) if record]
    assert audit["removed_lines"] == 0
    assert audit["disposition"]["chat_id"] == "never-seen"
    assert audit["disposition"]["before_ms"] is None


def test_zero_match_unscoped_message_purge_is_refused_without_audit(home: Path) -> None:
    result = runner.invoke(
        app,
        ["raw", "purge", "--channel", "whatsapp", "--message", "never-seen", "--yes"],
    )

    assert result.exit_code == 2
    assert "Refused:" in result.output
    assert not (home / "data" / "raw" / "AUDIT").exists()


def test_purge_without_chat_or_message_is_refused(home: Path) -> None:
    result = runner.invoke(app, ["raw", "purge", "--channel", "whatsapp", "--yes"])
    assert result.exit_code == 2


def test_seed_dry_run_and_import_use_only_home_stores(home: Path) -> None:
    processing_db = get_operational_store_path("processing", data_dir=home / "data")
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
    assert json.loads(seed_file.read_text().splitlines()[0])["native_id"] == "history-event"


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
    live_db = get_operational_store_path("processing", data_dir=home / "data")
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
    rebuilt = ProcessingStore(get_operational_store_path("processing", data_dir=target / "data"))
    assert rebuilt.get_event("e1") is not None


def test_capture_check_passes_complete_whatsapp_pairs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YEOMAN_HOME", str(tmp_path))
    inputs = _capture_home(
        tmp_path,
        effects=[(SECRET_ID, "whatsapp", "secret-chat", "text"),
                 ("forward-effect", "whatsapp", "secret-chat", "forward"),
                 ("delete-effect", "whatsapp", "secret-chat", "delete"),
                 ("external-effect", "whatsapp", "secret-chat", "external_action")],
        receipts=[("r1", SECRET_ID, "whatsapp", "secret-chat", SECRET_ID),
                  ("r-forward", "forward-effect", "whatsapp", "secret-chat", "forward-message")],
        raw=[("outbound_request", "whatsapp", "secret-chat", SECRET_ID, None),
             ("outbound_result", "whatsapp", "secret-chat", SECRET_ID, SECRET_ID),
             ("outbound_request", "whatsapp", "secret-chat", "forward-effect", None),
             ("outbound_result", "whatsapp", "secret-chat", "forward-effect", "forward-message")],
    )
    before = _input_bytes(inputs)

    result = runner.invoke(app, ["raw", "check-capture"])

    assert result.exit_code == 0, result.output
    _assert_aggregate_output(result.output)
    assert 'effect_kinds={"forward": 1, "text": 1}' in result.output
    assert _input_bytes(inputs) == before


def test_capture_check_reports_missing_receipt_result_request_and_provider_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("YEOMAN_HOME", str(tmp_path))
    inputs = _capture_home(
        tmp_path,
        effects=[("e-receipt", "whatsapp", "secret-chat", "text"),
                 ("e-result", "whatsapp", "secret-chat", "text"),
                 ("e-provider", "whatsapp", "secret-chat", "text"),
                 ("e-no-receipt", "whatsapp", "secret-chat", "text")],
        receipts=[("r-receipt", "e-receipt", "whatsapp", "secret-chat", SECRET_ID),
                  ("r-result", "e-result", "whatsapp", "secret-chat", "no-raw-result"),
                  ("r-provider", "e-provider", "whatsapp", "secret-chat", SECRET_ID)],
        raw=[("outbound_result", "whatsapp", "secret-chat", "e-result", "missing-result"),
             ("outbound_request", "whatsapp", "secret-chat", "e-provider", None),
             ("outbound_result", "whatsapp", "secret-chat", "e-provider", None)],
    )
    before = _input_bytes(inputs)

    result = runner.invoke(app, ["raw", "check-capture"])

    assert result.exit_code == 1, result.output
    _assert_aggregate_output(result.output)
    assert "missing_receipts=1" in result.output
    assert "missing_requests=1" in result.output
    assert "missing_results=3" in result.output
    assert "missing_provider_ids=1" in result.output
    assert _input_bytes(inputs) == before


def test_capture_check_reports_duplicate_request_correlation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("YEOMAN_HOME", str(tmp_path))
    inputs = _capture_home(
        tmp_path,
        effects=[("e1", "whatsapp", "secret-chat", "text")],
        receipts=[("r1", "e1", "whatsapp", "secret-chat", SECRET_ID)],
        raw=[("outbound_request", "whatsapp", "secret-chat", "e1", None),
             ("outbound_request", "whatsapp", "secret-chat", "e1", None),
             ("outbound_result", "whatsapp", "secret-chat", "e1", SECRET_ID)],
    )
    before = _input_bytes(inputs)

    result = runner.invoke(app, ["raw", "check-capture"])

    assert result.exit_code == 1, result.output
    _assert_aggregate_output(result.output)
    assert "ambiguous_requests=1" in result.output
    assert "missing_requests=0" in result.output
    assert _input_bytes(inputs) == before


def test_capture_check_reports_duplicate_empty_request_correlation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("YEOMAN_HOME", str(tmp_path))
    inputs = _capture_home(
        tmp_path,
        raw=[("outbound_request", "whatsapp", "secret-chat", "", None),
             ("outbound_request", "whatsapp", "secret-chat", "", None)],
    )
    before = _input_bytes(inputs)

    result = runner.invoke(app, ["raw", "check-capture"])

    assert result.exit_code == 1, result.output
    _assert_aggregate_output(result.output)
    assert "ambiguous_requests=1" in result.output
    assert "missing_requests=0" in result.output
    assert _input_bytes(inputs) == before


def test_capture_check_reports_duplicate_provider_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YEOMAN_HOME", str(tmp_path))
    inputs = _capture_home(
        tmp_path,
        effects=[("e1", "whatsapp", "secret-chat", "text")],
        receipts=[("r1", "e1", "whatsapp", "secret-chat", SECRET_ID)],
        raw=[("outbound_request", "whatsapp", "secret-chat", "e1", None),
             ("outbound_result", "whatsapp", "secret-chat", "e1", SECRET_ID),
             ("outbound_result", "whatsapp", "secret-chat", "e-other", SECRET_ID)],
    )
    before = _input_bytes(inputs)
    result = runner.invoke(app, ["raw", "check-capture"])
    assert result.exit_code == 1, result.output
    _assert_aggregate_output(result.output)
    assert "ambiguous_provider_ids=1" in result.output
    assert _input_bytes(inputs) == before


def test_capture_check_reports_duplicate_result_correlation_with_distinct_provider_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("YEOMAN_HOME", str(tmp_path))
    inputs = _capture_home(
        tmp_path,
        effects=[("e1", "whatsapp", "secret-chat", "text")],
        receipts=[("r1", "e1", "whatsapp", "secret-chat", SECRET_ID)],
        raw=[("outbound_request", "whatsapp", "secret-chat", "e1", None),
             ("outbound_result", "whatsapp", "secret-chat", "e1", SECRET_ID),
             ("outbound_result", "whatsapp", "secret-chat", "e1", "other-provider-id")],
    )
    before = _input_bytes(inputs)

    result = runner.invoke(app, ["raw", "check-capture"])

    assert result.exit_code == 1, result.output
    _assert_aggregate_output(result.output)
    assert "ambiguous_results=1" in result.output
    assert _input_bytes(inputs) == before


def test_capture_check_counts_mismatched_receipt_as_one_missing_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("YEOMAN_HOME", str(tmp_path))
    inputs = _capture_home(
        tmp_path,
        effects=[("e1", "whatsapp", "secret-chat", "text")],
        receipts=[("r1", "e1", "telegram", "telegram-chat", SECRET_ID)],
        raw=[],
    )
    before = _input_bytes(inputs)

    result = runner.invoke(app, ["raw", "check-capture"])

    assert result.exit_code == 1, result.output
    _assert_aggregate_output(result.output)
    assert "missing_results=1" in result.output
    assert _input_bytes(inputs) == before


def test_capture_check_matches_telegram_message_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YEOMAN_HOME", str(tmp_path))
    inputs = _capture_home(
        tmp_path,
        effects=[("tg-effect", "telegram", "telegram-chat", "text")],
        receipts=[("tg-receipt", "tg-effect", "telegram", "telegram-chat", SECRET_ID)],
        raw=[("outbound_request", "telegram", "telegram-chat", "tg-correlation", None),
             ("outbound_result", "telegram", "telegram-chat", "tg-correlation", SECRET_ID)],
    )
    before = _input_bytes(inputs)
    result = runner.invoke(app, ["raw", "check-capture"])
    assert result.exit_code == 0, result.output
    _assert_aggregate_output(result.output)
    assert _input_bytes(inputs) == before


def test_capture_check_requires_start_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YEOMAN_HOME", str(tmp_path))
    inputs = _capture_home(tmp_path, start=False)
    before = _input_bytes(inputs)
    result = runner.invoke(app, ["raw", "check-capture"])
    assert result.exit_code == 1
    _assert_aggregate_output(result.output)
    assert "start_marker=missing" in result.output
    assert _input_bytes(inputs) == before


def test_capture_check_is_read_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YEOMAN_HOME", str(tmp_path))
    inputs = _capture_home(
        tmp_path,
        effects=[("e1", "whatsapp", "secret-chat", "text")],
        receipts=[("r1", "e1", "whatsapp", "secret-chat", SECRET_ID)],
        raw=[("outbound_request", "whatsapp", "secret-chat", "e1", None),
             ("outbound_result", "whatsapp", "secret-chat", "e1", SECRET_ID)],
    )
    before = _input_bytes(inputs)
    result = runner.invoke(app, ["raw", "check-capture"])
    assert result.exit_code == 0, result.output
    _assert_aggregate_output(result.output)
    assert _input_bytes(inputs) == before
    assert {path for path in tmp_path.rglob("*") if path.is_file()} == set(inputs)


def test_capture_check_ignores_pre_start_effects_and_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("YEOMAN_HOME", str(tmp_path))
    inputs = _capture_home(
        tmp_path,
        effects=[("old-effect", "whatsapp", "old-chat", "text"),
                 ("current-effect", "whatsapp", "current-chat", "text")],
        receipts=[("old-receipt", "old-effect", "whatsapp", "old-chat", "old-id"),
                  ("current-receipt", "current-effect", "whatsapp", "current-chat", "current-id")],
        raw=[("outbound_request", "whatsapp", "current-chat", "current-effect", None),
             ("outbound_result", "whatsapp", "current-chat", "current-effect", "current-id")],
    )
    db_path = get_operational_store_path("processing", data_dir=tmp_path / "data")
    with sqlite3.connect(db_path) as connection:
        connection.execute("UPDATE effects SET created_ms = 1 WHERE effect_id = 'old-effect'")
        connection.execute("UPDATE transport_receipts SET confirmed_ms = 1 WHERE effect_id = 'old-effect'")
    before = _input_bytes(inputs)

    result = runner.invoke(app, ["raw", "check-capture"])

    assert result.exit_code == 0, result.output
    _assert_aggregate_output(result.output)
    assert "effects=1 receipts=1 outbound_results=1" in result.output
    assert 'channels={"whatsapp": 1}' in result.output
    assert _input_bytes(inputs) == before


def test_capture_check_ignores_unsupported_channel_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("YEOMAN_HOME", str(tmp_path))
    inputs = _capture_home(
        tmp_path,
        effects=[("current-effect", "whatsapp", "current-chat", "text"),
                 ("unsupported-effect", "email", "unsupported-chat", "text")],
        receipts=[("current-receipt", "current-effect", "whatsapp", "current-chat", "current-id"),
                  ("wrong-channel-receipt", "current-effect", "email", "unsupported-chat", "unsupported-id"),
                  ("unsupported-receipt", "unsupported-effect", "email", "unsupported-chat", "unsupported-id")],
        raw=[("outbound_request", "whatsapp", "current-chat", "current-effect", None),
             ("outbound_result", "whatsapp", "current-chat", "current-effect", "current-id"),
             ("outbound_request", "email", "unsupported-chat", "unsupported-effect", None),
             ("outbound_result", "email", "unsupported-chat", "unsupported-effect", "unsupported-id")],
    )
    before = _input_bytes(inputs)

    result = runner.invoke(app, ["raw", "check-capture"])

    assert result.exit_code == 0, result.output
    _assert_aggregate_output(result.output)
    assert "effects=1 receipts=1 outbound_results=1" in result.output
    assert 'channels={"whatsapp": 1}' in result.output
    assert _input_bytes(inputs) == before
