# tests/overseer/test_tool_write_file.py
from __future__ import annotations
from pathlib import Path
from unittest.mock import MagicMock
import pytest
from yeoman_overseer.agent.tools.write_file import write_file, _is_allowed


def _ctx(tmp_path: Path) -> MagicMock:
    ctx = MagicMock()
    ctx.yeoman_home = tmp_path / ".yeoman"
    ctx.source_dir = tmp_path / "yeoman"
    ctx.data_dir = tmp_path / ".yeoman" / "data"
    ctx.runbook_name = "test-runbook"
    ctx.domain = "ops"
    ctx.git = MagicMock()
    ctx.audit = MagicMock()
    ctx.yeoman_home.mkdir(parents=True)
    ctx.source_dir.mkdir(parents=True)
    return ctx


def test_write_to_allowed_path(tmp_path):
    ctx = _ctx(tmp_path)
    target = str(ctx.yeoman_home / "config.json")
    result = write_file(target, '{"key": "value"}', ctx)
    assert result["ok"] is True
    assert Path(target).read_text() == '{"key": "value"}'


def test_audit_logged_on_write(tmp_path):
    ctx = _ctx(tmp_path)
    target = str(ctx.yeoman_home / "notes.txt")
    write_file(target, "hello", ctx)
    ctx.audit.append.assert_called_once()
    entry = ctx.audit.append.call_args[0][0]
    assert entry.action == "write_file"
    assert entry.target == target


def test_deny_list_dot_env(tmp_path):
    ctx = _ctx(tmp_path)
    result = write_file(str(ctx.yeoman_home / ".env"), "SECRET=x", ctx)
    assert result["ok"] is False
    assert "denied" in result["error"]


def test_deny_list_secrets_dir(tmp_path):
    ctx = _ctx(tmp_path)
    result = write_file(str(ctx.yeoman_home / "secrets" / "key.pem"), "data", ctx)
    assert result["ok"] is False


def test_deny_list_dot_git(tmp_path):
    ctx = _ctx(tmp_path)
    result = write_file(str(ctx.source_dir / ".git" / "hooks" / "pre-commit"), "evil", ctx)
    assert result["ok"] is False


def test_deny_list_runbooks(tmp_path):
    ctx = _ctx(tmp_path)
    result = write_file(str(ctx.yeoman_home / "runbooks" / "new.md"), "---\nname: x", ctx)
    assert result["ok"] is False


def test_deny_list_systemd(tmp_path):
    ctx = _ctx(tmp_path)
    result = write_file(str(ctx.yeoman_home / "systemd" / "unit.service"), "[Unit]", ctx)
    assert result["ok"] is False


def test_path_outside_roots_denied(tmp_path):
    ctx = _ctx(tmp_path)
    result = write_file("/etc/passwd", "root:x:0:0", ctx)
    assert result["ok"] is False


def test_symlink_traversal_blocked(tmp_path):
    ctx = _ctx(tmp_path)
    link = ctx.yeoman_home / "escape"
    link.symlink_to("/etc")
    result = write_file(str(link / "passwd"), "evil", ctx)
    assert result["ok"] is False


def test_is_allowed_helper(tmp_path):
    ctx = _ctx(tmp_path)
    assert _is_allowed(ctx.yeoman_home / "config.json", ctx) is True
    assert _is_allowed(ctx.yeoman_home / ".env", ctx) is False
    assert _is_allowed(Path("/tmp/outside"), ctx) is False
