"""Daily integrity check: closed months are checksummed; drops need an AUDIT entry."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest
from yeoman_shared.raw_archive import writer
from yeoman_shared.raw_archive.records import append_protected, dumps
from yeoman_shared.raw_archive.verify import (
    AUDIT,
    append_suppression,
    close_months,
    latest_manifest,
    load_suppressions,
    verify_archive,
)
from yeoman_shared.raw_archive.writer import RawArchive, RawEvent

SEPT = 1_790_000_000_000  # 2026-09-21
OCT = 1_791_500_000_000  # 2026-10-08


def _archive(tmp_path: Path, clock: int) -> RawArchive:
    return RawArchive(
        tmp_path / "raw",
        spool=tmp_path / "raw-spool",
        status_path=tmp_path / "run" / "raw-archive.json",
        clock=lambda: clock,
    )


def _append(archive: RawArchive, native_id: str, received_ms: int) -> None:
    archive.append(
        RawEvent(
            channel="whatsapp",
            kind="message",
            direction="in",
            native={"id": native_id},
            native_id=native_id,
            chat_id="c",
            received_ms=received_ms,
        )
    )


def test_close_months_seals_only_finished_months(tmp_path: Path) -> None:
    archive = _archive(tmp_path, OCT)
    _append(archive, "a", SEPT)
    _append(archive, "b", OCT)
    root = tmp_path / "raw"
    assert close_months(root, now_ms=OCT) == ["whatsapp/2026-09.jsonl"]
    assert stat.S_IMODE((root / "whatsapp" / "2026-09.jsonl").stat().st_mode) == 0o444
    assert stat.S_IMODE((root / "whatsapp" / "2026-10.jsonl").stat().st_mode) == 0o600
    entry = latest_manifest(root)["whatsapp/2026-09.jsonl"]
    assert entry["lines"] == 1 and len(entry["sha256"]) == 64
    assert close_months(root, now_ms=OCT) == []


@pytest.mark.parametrize("backlog_kind", ("spool", "memory"))
def test_verify_defers_month_seal_until_backlog_drains(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backlog_kind: str
) -> None:
    archive = _archive(tmp_path, OCT)
    _append(archive, "before-failure", SEPT)
    root = tmp_path / "raw"
    month_file = root / "whatsapp" / "2026-09.jsonl"
    original_append_line = writer.append_line
    original_spool_line = archive._spool_line_locked

    def fail_month_append(
        path: Path,
        line: str,
        *,
        mode: int = writer.OPEN_FILE_MODE,
        **append_options: Any,
    ) -> bool:
        if path == month_file:
            raise OSError("temporary storage failure")
        return original_append_line(path, line, mode=mode, **append_options)

    def fail_spool_append(channel: str, received_ms: int, line: str) -> bool:
        return False

    monkeypatch.setattr(writer, "append_line", fail_month_append)
    if backlog_kind == "memory":
        monkeypatch.setattr(archive, "_spool_line_locked", fail_spool_append)
    _append(archive, "retained-september", SEPT)
    status = archive.status()
    if backlog_kind == "spool":
        assert status.spooled == 1 and status.pending_in_memory == 0
    else:
        assert status.spooled == 0 and status.pending_in_memory == 1

    report = verify_archive(root, run_dir=tmp_path / "run", now_ms=OCT)
    assert report.closed == ()
    assert stat.S_IMODE(month_file.stat().st_mode) == 0o600

    monkeypatch.setattr(writer, "append_line", original_append_line)
    monkeypatch.setattr(archive, "_spool_line_locked", original_spool_line)
    assert archive.drain_spool() == 1
    _append(archive, "october-after-repair", OCT)

    report = verify_archive(root, run_dir=tmp_path / "run", now_ms=OCT + 1)
    assert report.ok, report.problems
    assert report.closed == ("whatsapp/2026-09.jsonl",)
    assert stat.S_IMODE(month_file.stat().st_mode) == 0o444
    assert len(month_file.read_text(encoding="utf-8").splitlines()) == 2


def test_verify_defers_seal_when_status_is_missing_but_spool_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _archive(tmp_path, OCT)
    _append(archive, "before-failure", SEPT)
    root = tmp_path / "raw"
    month_file = root / "whatsapp" / "2026-09.jsonl"
    original_append_line = writer.append_line

    def fail_month_append(
        path: Path,
        line: str,
        *,
        mode: int = writer.OPEN_FILE_MODE,
        **append_options: Any,
    ) -> bool:
        if path == month_file:
            raise OSError("temporary storage failure")
        return original_append_line(path, line, mode=mode, **append_options)

    monkeypatch.setattr(writer, "append_line", fail_month_append)
    _append(archive, "retained-september", SEPT)
    (tmp_path / "run" / writer.STATUS_FILE).unlink()
    assert list((root.parent / "raw-spool").glob("*.json"))

    report = verify_archive(root, run_dir=tmp_path / "run", now_ms=OCT)

    assert report.closed == ()
    assert stat.S_IMODE(month_file.stat().st_mode) == 0o600


def test_verify_defers_seal_when_spool_scan_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _archive(tmp_path, OCT)
    _append(archive, "before-failure", SEPT)
    root = tmp_path / "raw"
    month_file = root / "whatsapp" / "2026-09.jsonl"
    original_append_line = writer.append_line

    def fail_month_append(
        path: Path,
        line: str,
        *,
        mode: int = writer.OPEN_FILE_MODE,
        **append_options: Any,
    ) -> bool:
        if path == month_file:
            raise OSError("temporary storage failure")
        return original_append_line(path, line, mode=mode, **append_options)

    monkeypatch.setattr(writer, "append_line", fail_month_append)
    _append(archive, "retained-september", SEPT)
    spool = root.parent / "raw-spool"
    assert list(spool.glob("*.json"))
    (tmp_path / "run" / writer.STATUS_FILE).unlink()

    original_scandir = os.scandir

    def fail_spool_scan(path: str | os.PathLike[str] | int):
        if isinstance(path, int) or Path(path) != spool:
            return original_scandir(path)
        raise PermissionError("spool is unreadable")

    monkeypatch.setattr(os, "scandir", fail_spool_scan)
    report = verify_archive(root, run_dir=tmp_path / "run", now_ms=OCT)

    assert report.closed == ()
    assert stat.S_IMODE(month_file.stat().st_mode) == 0o600


def test_verify_seals_month_when_writer_status_is_absent(tmp_path: Path) -> None:
    archive = _archive(tmp_path, OCT)
    _append(archive, "september", SEPT)
    root = tmp_path / "raw"
    run_dir = tmp_path / "verify-run"

    report = verify_archive(root, run_dir=run_dir, now_ms=OCT)

    assert not (run_dir / writer.STATUS_FILE).exists()
    assert report.closed == ("whatsapp/2026-09.jsonl",)
    assert stat.S_IMODE((root / "whatsapp" / "2026-09.jsonl").stat().st_mode) == 0o444


def test_verify_is_clean_for_an_untouched_archive(tmp_path: Path) -> None:
    archive = _archive(tmp_path, OCT)
    _append(archive, "a", SEPT)
    _append(archive, "b", OCT)
    report = verify_archive(tmp_path / "raw", run_dir=tmp_path / "run", now_ms=OCT)
    assert report.ok, report.problems
    assert report.closed == ("whatsapp/2026-09.jsonl",)
    assert report.lines_total == 2


def test_verify_flags_a_changed_closed_month(tmp_path: Path) -> None:
    archive = _archive(tmp_path, OCT)
    _append(archive, "a", SEPT)
    root = tmp_path / "raw"
    verify_archive(root, run_dir=tmp_path / "run", now_ms=OCT)
    sealed = root / "whatsapp" / "2026-09.jsonl"
    os.chmod(sealed, 0o600)
    sealed.write_text('{"tampered":true}\n')
    report = verify_archive(root, run_dir=tmp_path / "run", now_ms=OCT)
    assert not report.ok
    assert "checksum_mismatch:whatsapp/2026-09.jsonl" in report.problems


def test_verify_flags_a_line_drop_without_audit(tmp_path: Path) -> None:
    archive = _archive(tmp_path, OCT)
    _append(archive, "a", OCT)
    _append(archive, "b", OCT)
    root = tmp_path / "raw"
    assert verify_archive(root, run_dir=tmp_path / "run", now_ms=OCT).ok
    open_month = root / "whatsapp" / "2026-10.jsonl"
    open_month.write_text(open_month.read_text().splitlines()[0] + "\n")
    report = verify_archive(root, run_dir=tmp_path / "run", now_ms=OCT + 1)
    assert "line_count_dropped:whatsapp/2026-10.jsonl:2->1" in report.problems


def test_verify_accepts_a_drop_covered_by_audit(tmp_path: Path) -> None:
    archive = _archive(tmp_path, OCT)
    _append(archive, "a", OCT)
    _append(archive, "b", OCT)
    root = tmp_path / "raw"
    verify_archive(root, run_dir=tmp_path / "run", now_ms=OCT)
    open_month = root / "whatsapp" / "2026-10.jsonl"
    open_month.write_text(open_month.read_text().splitlines()[0] + "\n")
    append_protected(
        root / AUDIT,
        dumps({"ts_ms": OCT + 1, "files": ["whatsapp/2026-10.jsonl"]}),
    )
    assert verify_archive(root, run_dir=tmp_path / "run", now_ms=OCT + 2).ok


def test_verify_accepts_a_missing_manifest_file_covered_by_audit(tmp_path: Path) -> None:
    archive = _archive(tmp_path, OCT)
    _append(archive, "a", SEPT)
    root = tmp_path / "raw"
    assert verify_archive(root, run_dir=tmp_path / "run", now_ms=OCT).ok
    relative = "whatsapp/2026-09.jsonl"
    (root / relative).unlink()
    append_protected(root / AUDIT, dumps({"ts_ms": OCT + 1, "files": [relative]}))
    assert verify_archive(root, run_dir=tmp_path / "run", now_ms=OCT + 2).ok


def test_verify_reports_a_degraded_writer(tmp_path: Path) -> None:
    _archive(tmp_path, OCT)
    (tmp_path / "run").mkdir(exist_ok=True)
    (tmp_path / "run" / "raw-archive.json").write_text(
        json.dumps({"state": "degraded", "spooled": 3})
    )
    report = verify_archive(tmp_path / "raw", run_dir=tmp_path / "run", now_ms=OCT)
    assert "writer_degraded:spooled=3" in report.problems


def test_verify_reports_a_blocked_writer_without_event_contents(tmp_path: Path) -> None:
    _archive(tmp_path, OCT)
    (tmp_path / "run").mkdir(exist_ok=True)
    (tmp_path / "run" / "raw-archive.json").write_text(
        json.dumps(
            {
                "state": "blocked",
                "spooled": 0,
                "pending_in_memory": 10_000,
                "last_error": "capacity reached",
            }
        )
    )
    report = verify_archive(tmp_path / "raw", run_dir=tmp_path / "run", now_ms=OCT)
    assert "writer_blocked:pending=10000" in report.problems
    assert all("capacity reached" not in problem for problem in report.problems)


def test_suppressions_are_append_only_and_readable(tmp_path: Path) -> None:
    root = tmp_path / "raw"
    root.mkdir()
    append_suppression(
        root, channel="whatsapp", chat_id="c", native_id="m-1", reason="forget", now_ms=OCT
    )
    append_suppression(
        root, channel="whatsapp", chat_id="c", native_id="m-2", reason="forget", now_ms=OCT
    )
    assert load_suppressions(root) == {
        ("whatsapp", "c", "m-1"),
        ("whatsapp", "c", "m-2"),
    }
    assert stat.S_IMODE((root / "SUPPRESSIONS").stat().st_mode) == 0o444


def test_missing_archive_is_a_problem(tmp_path: Path) -> None:
    report = verify_archive(tmp_path / "raw", run_dir=tmp_path / "run", now_ms=OCT)
    assert report.problems == ("archive_missing",)
