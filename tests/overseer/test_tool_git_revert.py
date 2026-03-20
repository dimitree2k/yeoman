# tests/overseer/test_tool_git_revert.py
from __future__ import annotations
from pathlib import Path
from unittest.mock import MagicMock
import pytest
from yeoman_overseer.agent.tools.git_revert import git_revert


def _ctx(tmp_path: Path) -> MagicMock:
    ctx = MagicMock()
    ctx.source_dir = tmp_path / "yeoman"
    ctx.data_dir = tmp_path / "data"
    ctx.runbook_name = "test"
    ctx.domain = "ops"
    ctx.git = MagicMock()
    ctx.audit = MagicMock()
    return ctx


def test_revert_calls_internal_git(tmp_path):
    ctx = _ctx(tmp_path)
    ctx.git.revert.return_value = None
    result = git_revert("abc123", ctx=ctx)
    assert result["ok"] is True
    ctx.git.revert.assert_called_once_with("abc123")


def test_revert_audit_logged(tmp_path):
    ctx = _ctx(tmp_path)
    result = git_revert("abc123", ctx=ctx)
    ctx.audit.append.assert_called_once()
    entry = ctx.audit.append.call_args[0][0]
    assert entry.action == "git_revert"
    assert "abc123" in entry.target


def test_revert_propagates_git_error(tmp_path):
    ctx = _ctx(tmp_path)
    ctx.git.revert.side_effect = Exception("nothing to revert")
    result = git_revert("deadbeef", ctx=ctx)
    assert result["ok"] is False
    assert "nothing to revert" in result["error"]


def test_sha_validation_rejects_non_hex(tmp_path):
    ctx = _ctx(tmp_path)
    result = git_revert("not_a_sha!", ctx=ctx)
    assert result["ok"] is False
    assert "invalid sha" in result["error"].lower()
