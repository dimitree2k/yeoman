# tests/overseer/test_agent_patcher.py
from __future__ import annotations
import subprocess
from pathlib import Path
import pytest
from yeoman_overseer.agent.patcher import Patcher, PatchContext


def _init_git_repo(path: Path) -> None:
    """Create a real git repo with a root commit so worktrees work."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True, capture_output=True)
    (path / "README.md").write_text("init")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True)


def test_create_worktree(tmp_path):
    live_repo = tmp_path / "repo"
    _init_git_repo(live_repo)
    patcher = Patcher()
    ctx = patcher.create_worktree(live_repo, run_id="abc123")
    try:
        assert ctx.worktree_path.is_dir()
        assert ctx.branch == "overseer-patch-abc123"
        assert ctx.live_repo == live_repo
        result = subprocess.run(
            ["git", "worktree", "list"],
            cwd=live_repo, capture_output=True, text=True
        )
        assert "abc123" in result.stdout
    finally:
        patcher.discard(ctx)


def test_translate_path(tmp_path):
    live_repo = tmp_path / "repo"
    _init_git_repo(live_repo)
    patcher = Patcher()
    ctx = patcher.create_worktree(live_repo, run_id="xyz")
    try:
        original = live_repo / "src" / "foo.py"
        translated = patcher.translate_path(ctx, original)
        assert translated == ctx.worktree_path / "src" / "foo.py"
    finally:
        patcher.discard(ctx)


def test_apply_merges_into_live_repo(tmp_path):
    live_repo = tmp_path / "repo"
    _init_git_repo(live_repo)
    patcher = Patcher()
    ctx = patcher.create_worktree(live_repo, run_id="test-apply")
    try:
        new_file = ctx.worktree_path / "hello.txt"
        new_file.write_text("world")
        patcher.apply(ctx)
        assert (live_repo / "hello.txt").exists()
        assert not ctx.worktree_path.exists()
    except Exception:
        patcher.discard(ctx)
        raise


def test_discard_removes_worktree(tmp_path):
    live_repo = tmp_path / "repo"
    _init_git_repo(live_repo)
    patcher = Patcher()
    ctx = patcher.create_worktree(live_repo, run_id="test-discard")
    patcher.discard(ctx)
    assert not ctx.worktree_path.exists()
    result = subprocess.run(
        ["git", "branch"],
        cwd=live_repo, capture_output=True, text=True
    )
    assert "overseer-patch-test-discard" not in result.stdout


def test_apply_handles_no_changes(tmp_path):
    live_repo = tmp_path / "repo"
    _init_git_repo(live_repo)
    patcher = Patcher()
    ctx = patcher.create_worktree(live_repo, run_id="empty")
    try:
        patcher.apply(ctx)
        assert not ctx.worktree_path.exists()
    except Exception:
        patcher.discard(ctx)
        raise
