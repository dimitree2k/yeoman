from __future__ import annotations
from pathlib import Path
from unittest.mock import MagicMock
import pytest
from yeoman_overseer.agent.patcher import Patcher, PatchContext


def _make_patch_ctx(worktree: Path, live_repo: Path) -> PatchContext:
    return PatchContext(
        worktree_path=worktree,
        branch="overseer-patch-run1",
        live_repo=live_repo,
    )


def test_no_gate_when_requires_tests_false(tmp_path):
    from yeoman_overseer.agent.loop import _route_write_path
    original = tmp_path / "yeoman" / "src" / "foo.py"
    source_dir = tmp_path / "yeoman"
    result = _route_write_path(original, source_dir, patch_ctx=None, requires_tests=False)
    assert result == original


def test_gate_translates_path_when_requires_tests_true(tmp_path):
    from yeoman_overseer.agent.loop import _route_write_path
    live_repo = tmp_path / "yeoman"
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True)
    ctx = _make_patch_ctx(worktree, live_repo)
    live_repo.mkdir(parents=True)
    (live_repo / "src").mkdir(parents=True)
    original = live_repo / "src" / "foo.py"
    translated = _route_write_path(original, live_repo, patch_ctx=ctx, requires_tests=True)
    assert translated == worktree / "src" / "foo.py"


def test_gate_passes_on_test_success(tmp_path):
    from yeoman_overseer.agent.loop import _finalize_patch
    patcher = MagicMock(spec=Patcher)
    run_tests_result = {"passed": True, "exit_code": 0, "output": "2 passed"}
    run_tests_fn = MagicMock(return_value=run_tests_result)
    ctx = MagicMock()
    worktree = tmp_path / "wt"
    live_repo = tmp_path / "repo"
    patch_ctx = _make_patch_ctx(worktree, live_repo)
    result = _finalize_patch(patch_ctx, patcher, run_tests_fn, ctx)
    patcher.apply.assert_called_once_with(patch_ctx)
    patcher.discard.assert_not_called()
    assert result["patch_applied"] is True


def test_gate_discards_on_test_failure(tmp_path):
    from yeoman_overseer.agent.loop import _finalize_patch
    patcher = MagicMock(spec=Patcher)
    run_tests_result = {"passed": False, "exit_code": 1, "output": "1 failed"}
    run_tests_fn = MagicMock(return_value=run_tests_result)
    ctx = MagicMock()
    worktree = tmp_path / "wt"
    live_repo = tmp_path / "repo"
    patch_ctx = _make_patch_ctx(worktree, live_repo)
    result = _finalize_patch(patch_ctx, patcher, run_tests_fn, ctx)
    patcher.discard.assert_called_once_with(patch_ctx)
    patcher.apply.assert_not_called()
    assert result["patch_applied"] is False
