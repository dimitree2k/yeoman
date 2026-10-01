"""S29: overseer file actions never touch the raw archive."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from yeoman_overseer.comms.cascading import CascadingComms
from yeoman_overseer.executor.deterministic import DeterministicExecutor


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("YEOMAN_HOME", str(tmp_path))
    return tmp_path


def _old_file(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x\n")
    old = time.time() - 30 * 86400
    os.utime(path, (old, old))
    return path


def _executor() -> DeterministicExecutor:
    return DeterministicExecutor(comms=CascadingComms(channels=[], local_log=True))


async def test_prune_files_refuses_raw_archive_files(home: Path) -> None:
    raw_file = _old_file(home / "data" / "raw" / "whatsapp" / "2026-01.jsonl")
    result = await _executor().execute(
        "prune_files", target=str(raw_file.parent), max_age_days="1"
    )
    assert raw_file.exists()
    assert result.success is False
    assert "refused 1" in result.detail


async def test_prune_files_refuses_spool_files(home: Path) -> None:
    spooled = _old_file(home / "data" / "raw-spool" / "0001-a.json")
    result = await _executor().execute("prune_files", target=str(spooled.parent), max_age_days="1")
    assert spooled.exists()
    assert result.success is False


async def test_prune_files_still_prunes_ordinary_directories(home: Path) -> None:
    media = _old_file(home / "var" / "media" / "incoming" / "a.jpg")
    result = await _executor().execute("prune_files", target=str(media.parent), max_age_days="1")
    assert not media.exists()
    assert result.success is True


async def test_rotate_logs_refuses_raw_archive_files(home: Path) -> None:
    audit = _old_file(home / "data" / "raw" / "AUDIT")
    result = await _executor().execute("rotate_logs", target=str(audit))
    assert audit.exists()
    assert result.success is False
    assert "refusing" in result.detail
