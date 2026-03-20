# tests/overseer/test_tool_edit_file.py
from __future__ import annotations
from pathlib import Path
from unittest.mock import MagicMock
import pytest
from yeoman_overseer.agent.tools.edit_file import edit_file


def _ctx(tmp_path: Path) -> MagicMock:
    ctx = MagicMock()
    ctx.yeoman_home = tmp_path / ".yeoman"
    ctx.source_dir = tmp_path / "yeoman"
    ctx.data_dir = tmp_path / ".yeoman" / "data"
    ctx.runbook_name = "test"
    ctx.domain = "ops"
    ctx.git = MagicMock()
    ctx.audit = MagicMock()
    ctx.yeoman_home.mkdir(parents=True)
    ctx.source_dir.mkdir(parents=True)
    return ctx


def test_edit_replaces_old_with_new(tmp_path):
    ctx = _ctx(tmp_path)
    target = ctx.yeoman_home / "notes.txt"
    target.write_text("hello world\nfoo bar\n")
    result = edit_file(str(target), "hello world", "hello everyone", ctx)
    assert result["ok"] is True
    assert target.read_text() == "hello everyone\nfoo bar\n"


def test_edit_fails_if_old_not_found(tmp_path):
    ctx = _ctx(tmp_path)
    target = ctx.yeoman_home / "notes.txt"
    target.write_text("something else\n")
    result = edit_file(str(target), "hello world", "replacement", ctx)
    assert result["ok"] is False
    assert "not found" in result["error"]


def test_edit_fails_on_nonexistent_file(tmp_path):
    ctx = _ctx(tmp_path)
    result = edit_file(str(ctx.yeoman_home / "missing.txt"), "old", "new", ctx)
    assert result["ok"] is False


def test_edit_deny_list_env(tmp_path):
    ctx = _ctx(tmp_path)
    result = edit_file(str(ctx.yeoman_home / ".env"), "old", "new", ctx)
    assert result["ok"] is False
    assert "denied" in result["error"]


def test_audit_logged_on_edit(tmp_path):
    ctx = _ctx(tmp_path)
    target = ctx.yeoman_home / "notes.txt"
    target.write_text("old content")
    edit_file(str(target), "old content", "new content", ctx)
    ctx.audit.append.assert_called_once()
    entry = ctx.audit.append.call_args[0][0]
    assert entry.action == "edit_file"
