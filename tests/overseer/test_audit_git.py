"""Tests for internal git operations."""
from __future__ import annotations
from pathlib import Path
from yeoman_overseer.audit.git import InternalGit

def test_init_creates_repo(tmp_path: Path) -> None:
    git = InternalGit(tmp_path / "overseer")
    git.init()
    assert (tmp_path / "overseer" / ".git").is_dir()

def test_init_idempotent(tmp_path: Path) -> None:
    git = InternalGit(tmp_path / "overseer")
    git.init()
    git.init()
    assert (tmp_path / "overseer" / ".git").is_dir()

def test_commit_file(tmp_path: Path) -> None:
    repo = tmp_path / "overseer"
    git = InternalGit(repo)
    git.init()
    (repo / "runbooks").mkdir()
    (repo / "runbooks" / "test.md").write_text("# Test runbook")
    sha = git.commit(files=["runbooks/test.md"], message="[test] add test runbook")
    assert sha is not None
    assert len(sha) == 40

def test_commit_no_changes(tmp_path: Path) -> None:
    repo = tmp_path / "overseer"
    git = InternalGit(repo)
    git.init()
    sha = git.commit(files=[], message="empty commit")
    assert sha is None

def test_revert(tmp_path: Path) -> None:
    repo = tmp_path / "overseer"
    git = InternalGit(repo)
    git.init()
    f = repo / "config.txt"
    f.write_text("original")
    sha1 = git.commit(files=["config.txt"], message="original")
    f.write_text("modified")
    sha2 = git.commit(files=["config.txt"], message="modified")
    git.revert(sha2)
    assert f.read_text() == "original"

def test_log(tmp_path: Path) -> None:
    repo = tmp_path / "overseer"
    git = InternalGit(repo)
    git.init()
    (repo / "a.txt").write_text("a")
    git.commit(files=["a.txt"], message="first")
    (repo / "b.txt").write_text("b")
    git.commit(files=["b.txt"], message="second")
    log = git.log(limit=5)
    assert len(log) == 2
    assert "second" in log[0]["message"]
    assert "first" in log[1]["message"]
