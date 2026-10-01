"""S29: gateway and shared deletion routines never touch the raw archive."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from yeoman_gateway.knowledge._snapshot import _unlink_unprotected
from yeoman_gateway.knowledge._upgrade import _discard
from yeoman_gateway.media.storage import MediaStorage
from yeoman_gateway.session.manager import SessionManager
from yeoman_shared.raw_archive.paths import ProtectedPathError
from yeoman_shared.utils.backups import prune_backups


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("YEOMAN_HOME", str(tmp_path))
    return tmp_path


def _old_file(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x\n")
    old = time.time() - 400 * 86400
    os.utime(path, (old, old))
    return path


def test_media_cleanup_refuses_a_root_that_contains_the_archive(home: Path) -> None:
    raw_file = _old_file(home / "data" / "raw" / "media" / "whatsapp" / "2026-01" / "a.jpg")
    sibling = _old_file(home / "data" / "other.txt")
    storage = MediaStorage(incoming_dir=home / "data", outgoing_dir=home / "out")
    assert storage.cleanup_expired("data", retention_days=1) == 0
    assert raw_file.exists()
    assert sibling.exists()


def test_media_cleanup_still_prunes_the_media_directory(home: Path) -> None:
    media = _old_file(home / "var" / "media" / "incoming" / "whatsapp" / "a.jpg")
    storage = MediaStorage(
        incoming_dir=home / "var" / "media" / "incoming", outgoing_dir=home / "out"
    )
    assert storage.cleanup_expired("whatsapp", retention_days=1) == 1
    assert not media.exists()


def test_prune_backups_skips_protected_files(home: Path) -> None:
    raw_file = _old_file(home / "data" / "raw" / "whatsapp" / "2025-01.jsonl")
    removed = prune_backups(raw_file.parent, retention=1)
    assert removed == []
    assert raw_file.exists()


def test_session_delete_refuses_protected_paths(home: Path) -> None:
    raw_file = _old_file(home / "data" / "raw" / "whatsapp" / "2026-02.jsonl")
    manager = SessionManager(workspace=home / "workspace", sessions_dir=home / "sessions")
    manager._get_session_path = lambda key: raw_file  # type: ignore[method-assign]
    assert manager.delete("whatsapp:x") is False
    assert raw_file.exists()


def test_knowledge_cleanup_helpers_refuse_protected_paths(home: Path) -> None:
    raw_file = _old_file(home / "data" / "raw" / "whatsapp" / "2026-03.jsonl")
    _discard(raw_file)
    assert raw_file.exists()
    with pytest.raises(ProtectedPathError):
        _unlink_unprotected(raw_file)
    assert raw_file.exists()
