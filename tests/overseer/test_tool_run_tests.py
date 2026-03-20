# tests/overseer/test_tool_run_tests.py
from __future__ import annotations
from pathlib import Path
from unittest.mock import MagicMock
import pytest
from yeoman_overseer.agent.tools.run_tests import run_tests


def _ctx(source_dir: Path) -> MagicMock:
    ctx = MagicMock()
    ctx.source_dir = source_dir
    ctx.sandbox = MagicMock()
    ctx.runbook_name = "test"
    ctx.domain = "ops"
    ctx.audit = MagicMock()
    return ctx


def test_passes_with_zero_failures(tmp_path):
    ctx = _ctx(tmp_path)
    ctx.sandbox.run.return_value = {
        "stdout": "5 passed in 0.12s",
        "stderr": "",
        "exit_code": 0,
    }
    result = run_tests(ctx=ctx)
    assert result["passed"] is True
    assert result["exit_code"] == 0


def test_fails_with_nonzero_exit(tmp_path):
    ctx = _ctx(tmp_path)
    ctx.sandbox.run.return_value = {
        "stdout": "2 failed, 3 passed",
        "stderr": "",
        "exit_code": 1,
    }
    result = run_tests(ctx=ctx)
    assert result["passed"] is False


def test_source_root_override(tmp_path):
    ctx = _ctx(tmp_path)
    ctx.sandbox.run.return_value = {"stdout": "1 passed", "stderr": "", "exit_code": 0}
    worktree = tmp_path / "worktree"
    run_tests(source_root=worktree, ctx=ctx)
    call_kwargs = ctx.sandbox.run.call_args
    assert call_kwargs[1].get("source_root") == worktree


def test_pytest_env_vars_set(tmp_path):
    ctx = _ctx(tmp_path)
    ctx.sandbox.run.return_value = {"stdout": "1 passed", "stderr": "", "exit_code": 0}
    run_tests(ctx=ctx)
    call_kwargs = ctx.sandbox.run.call_args
    env = call_kwargs[1].get("env", {})
    assert "PYTEST_CACHE_DIR" in env


def test_output_included_in_result(tmp_path):
    ctx = _ctx(tmp_path)
    ctx.sandbox.run.return_value = {
        "stdout": "collected 3 items\n3 passed",
        "stderr": "some warning",
        "exit_code": 0,
    }
    result = run_tests(ctx=ctx)
    assert "3 passed" in result["output"]
