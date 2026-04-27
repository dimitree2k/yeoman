import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from yeoman_overseer.agent.tools.check_health import execute as check_health_execute
from yeoman_overseer.agent.tools.git_log import execute as git_log_execute
from yeoman_overseer.agent.tools.send_alert import execute as send_alert_execute


def _ctx(**kwargs):
    ctx = MagicMock()
    for k, v in kwargs.items():
        setattr(ctx, k, v)
    return ctx


# --- check_health ---


def test_check_health_delegates_to_checks():
    with patch(
        "yeoman_overseer.trigger.checks.run_check"
    ) as mock_check:
        mock_check.return_value = MagicMock(value=72.5, detail="ok")
        result = check_health_execute(
            {"check": "disk_usage_above", "target": "/home"}, _ctx()
        )
        assert "ok" in result or "72" in result
        mock_check.assert_called_once()


def test_check_health_forwards_extra_kwargs():
    with patch("yeoman_overseer.trigger.checks.run_check") as mock_check:
        mock_check.return_value = MagicMock(value=False, detail="ok")
        check_health_execute(
            {"check": "disk_usage_above", "target": "/", "threshold": 85},
            _ctx(),
        )
        mock_check.assert_called_once_with(
            "disk_usage_above", target="/", threshold=85
        )


def test_check_health_runs_disk_usage_above_end_to_end(tmp_path):
    result = check_health_execute(
        {"check": "disk_usage_above", "target": str(tmp_path), "threshold": 0},
        _ctx(),
    )
    assert "ERROR" not in result
    assert "disk_usage_above" in result


def test_check_health_unknown_check():
    result = check_health_execute(
        {"check": "nonexistent_check", "target": "x"}, _ctx()
    )
    assert "error" in result.lower() or "unknown" in result.lower()


# --- git_log ---


def test_git_log_source(tmp_path):
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t.com"], cwd=tmp_path, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.name", "T"], cwd=tmp_path, capture_output=True
    )
    (tmp_path / "f.txt").write_text("x")
    subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True)
    ctx = _ctx(source_dir=tmp_path)
    result = git_log_execute({"repo": "source", "limit": 5}, ctx)
    assert "init" in result


def test_git_log_empty_repo(tmp_path):
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
    ctx = _ctx(source_dir=tmp_path)
    result = git_log_execute({"repo": "source", "limit": 5}, ctx)
    assert isinstance(result, str)


# --- send_alert ---


@pytest.mark.asyncio
async def test_send_alert_calls_comms():
    comms = MagicMock()
    comms.send = AsyncMock(return_value=None)
    ctx = _ctx(comms=comms, audit=MagicMock())
    result = await send_alert_execute({"message": "test alert"}, ctx)
    comms.send.assert_called_once_with("test alert")
    assert "sent" in result.lower() or "ok" in result.lower()
