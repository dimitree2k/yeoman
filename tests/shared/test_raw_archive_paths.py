"""V1 spec §4.0: nothing but the owner purge may destroy raw archive files."""

from __future__ import annotations

from pathlib import Path

import pytest
from yeoman_shared.raw_archive.paths import (
    ProtectedPathError,
    assert_deletable,
    assert_deletable_tree,
    contains_protected,
    is_protected,
    raw_root,
    spool_root,
)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("YEOMAN_HOME", str(tmp_path))
    return tmp_path


def test_roots_live_under_operational_data(home: Path) -> None:
    assert raw_root() == home / "data" / "raw"
    assert spool_root() == home / "data" / "raw-spool"


def test_files_inside_protected_roots_are_protected(home: Path) -> None:
    assert is_protected(home / "data" / "raw")
    assert is_protected(home / "data" / "raw" / "whatsapp" / "2026-10.jsonl")
    assert is_protected(home / "data" / "raw" / "media" / "whatsapp" / "2026-10" / "a.jpg")
    assert is_protected(home / "data" / "raw-spool" / "1-x.json")


def test_lookalike_siblings_and_parents_are_not_protected_as_single_files(home: Path) -> None:
    assert not is_protected(home / "data" / "raw.bak")
    assert not is_protected(home / "data" / "rawx" / "f.jsonl")
    assert not is_protected(home / "data")
    assert not is_protected(home / "var" / "media" / "incoming" / "a.jpg")


def test_parents_are_protected_for_recursive_operations(home: Path) -> None:
    assert contains_protected(home / "data")
    assert contains_protected(home)
    assert contains_protected(home / "data" / "raw" / "whatsapp")
    assert not contains_protected(home / "var" / "media")


def test_dotdot_and_symlinks_are_resolved(home: Path) -> None:
    assert is_protected(home / "var" / ".." / "data" / "raw" / "f.jsonl")
    target_dir = raw_root()
    target_dir.mkdir(parents=True)
    target = target_dir / "f.jsonl"
    target.write_text("x\n")
    link = home / "link.jsonl"
    link.symlink_to(target)
    assert is_protected(link)


def test_default_runtime_raw_dir_is_always_protected(home: Path) -> None:
    assert is_protected(Path("~/.yeoman/data/raw/whatsapp/2026-10.jsonl").expanduser())


def test_assert_helpers_raise_only_for_protected_paths(home: Path) -> None:
    with pytest.raises(ProtectedPathError):
        assert_deletable(home / "data" / "raw" / "f.jsonl")
    with pytest.raises(ProtectedPathError):
        assert_deletable_tree(home / "data")
    assert_deletable(home / "var" / "media" / "incoming" / "a.jpg")
    assert_deletable_tree(home / "var" / "media")
    assert issubclass(ProtectedPathError, PermissionError)
