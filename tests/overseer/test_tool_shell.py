# tests/overseer/test_tool_shell.py
from __future__ import annotations
from pathlib import Path
from unittest.mock import MagicMock
import pytest
from yeoman_overseer.agent.tools.shell import shell


def _ctx(timeout: int = 60) -> MagicMock:
    ctx = MagicMock()
    ctx.shell_timeout_s = timeout
    ctx.sandbox = MagicMock()
    ctx.runbook_name = "test"
    ctx.domain = "ops"
    ctx.audit = MagicMock()
    return ctx


def test_shell_returns_structured_result():
    ctx = _ctx()
    ctx.sandbox.run.return_value = {
        "stdout": "hello\n",
        "stderr": "",
        "exit_code": 0,
    }
    result = shell("echo hello", ctx=ctx)
    assert result["stdout"] == "hello\n"
    assert result["exit_code"] == 0


def test_shell_passes_timeout_from_context():
    ctx = _ctx(timeout=30)
    ctx.sandbox.run.return_value = {"stdout": "", "stderr": "", "exit_code": 0}
    shell("true", ctx=ctx)
    call_kwargs = ctx.sandbox.run.call_args[1]
    assert call_kwargs.get("timeout") == 30


def test_shell_audit_logged():
    ctx = _ctx()
    ctx.sandbox.run.return_value = {"stdout": "done", "stderr": "", "exit_code": 0}
    shell("ls /tmp", ctx=ctx)
    ctx.audit.append.assert_called_once()
    entry = ctx.audit.append.call_args[0][0]
    assert entry.action == "shell"


def test_shell_command_split_correctly():
    ctx = _ctx()
    ctx.sandbox.run.return_value = {"stdout": "", "stderr": "", "exit_code": 0}
    shell("echo foo bar", ctx=ctx)
    cmd = ctx.sandbox.run.call_args[0][0]
    assert cmd == ["echo", "foo", "bar"]


def test_shell_propagates_sandbox_exception():
    ctx = _ctx()
    ctx.sandbox.run.side_effect = TimeoutError("timed out")
    result = shell("sleep 999", ctx=ctx)
    assert result["exit_code"] == -1
    assert "timed out" in result["stderr"]
