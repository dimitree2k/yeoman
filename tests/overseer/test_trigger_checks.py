"""Tests for built-in trigger check functions."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest
from yeoman_overseer.trigger.checks import (
    check_disk_usage_above,
    check_file_age_exceeds,
    check_process_alive,
    check_row_count_exceeds,
    check_systemd_active,
    check_whatsapp_bridge_connected,
    run_check,
)


def test_process_alive_current_process() -> None:
    result = check_process_alive(target=str(os.getpid()))
    assert result.value is True

def test_process_alive_nonexistent() -> None:
    result = check_process_alive(target="9999999")
    assert result.value is False

def test_process_alive_from_pid_file(tmp_path: Path) -> None:
    pid_file = tmp_path / "test.pid"
    pid_file.write_text(str(os.getpid()))
    result = check_process_alive(target=str(pid_file))
    assert result.value is True

def test_process_alive_stale_pid_file(tmp_path: Path) -> None:
    pid_file = tmp_path / "test.pid"
    pid_file.write_text("9999999")
    result = check_process_alive(target=str(pid_file))
    assert result.value is False

def test_file_age_exceeds(tmp_path: Path) -> None:
    f = tmp_path / "old.log"
    f.write_text("data")
    old_time = os.path.getmtime(f) - 7200
    os.utime(f, (old_time, old_time))
    result = check_file_age_exceeds(target=str(f), threshold="1h")
    assert result.value is True
    result2 = check_file_age_exceeds(target=str(f), threshold="3h")
    assert result2.value is False

def test_file_age_exceeds_missing_file(tmp_path: Path) -> None:
    result = check_file_age_exceeds(target=str(tmp_path / "nope"), threshold="1h")
    assert result.value is True

def test_disk_usage_above() -> None:
    result = check_disk_usage_above(target="/", threshold=0)
    assert result.value is True
    result2 = check_disk_usage_above(target="/", threshold=100)
    assert result2.value is False

def test_row_count_exceeds(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE items (id INTEGER)")
    conn.executemany("INSERT INTO items VALUES (?)", [(i,) for i in range(15)])
    conn.commit()
    conn.close()
    result = check_row_count_exceeds(target=str(db_path), query="SELECT COUNT(*) FROM items", threshold=10)
    assert result.value is True
    result2 = check_row_count_exceeds(target=str(db_path), query="SELECT COUNT(*) FROM items", threshold=20)
    assert result2.value is False

def test_run_check_dispatches() -> None:
    result = run_check("process_alive", target=str(os.getpid()))
    assert result.value is True

def test_systemd_active_uses_local_user_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    class Result:
        returncode = 0
        stderr = ""

    def fake_run(cmd: list[str], **kwargs: object) -> Result:
        calls.append(cmd)
        return Result()

    monkeypatch.setattr("yeoman_overseer.trigger.checks.subprocess.run", fake_run)

    result = check_systemd_active(target="yeoman-bridge.service")

    assert result.value is True
    assert calls == [["systemctl", "--user", "is-active", "--quiet", "yeoman-bridge.service"]]

def test_systemd_active_false_when_unit_inactive(monkeypatch: pytest.MonkeyPatch) -> None:
    class Result:
        returncode = 3
        stderr = ""

    monkeypatch.setattr("yeoman_overseer.trigger.checks.subprocess.run", lambda *args, **kwargs: Result())

    result = run_check("systemd_active", target="yeoman-bridge.service")

    assert result.value is False

def test_whatsapp_bridge_connected_reads_protocol_health(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_health(target: str, timeout_s: float) -> dict:
        assert target == "default"
        assert timeout_s == 3.0
        return {"whatsapp": {"connected": True, "running": True, "reconnectAttempts": 0}}

    monkeypatch.setattr(
        "yeoman_overseer.trigger.checks._whatsapp_bridge_health",
        fake_health,
    )

    result = check_whatsapp_bridge_connected(target="default")

    assert result.value is True
    assert "connected=True" in result.detail
    assert "running=True" in result.detail

def test_whatsapp_bridge_connected_false_when_logged_out(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_health(target: str, timeout_s: float) -> dict:
        return {
            "whatsapp": {
                "connected": False,
                "running": False,
                "reconnectAttempts": 31,
                "lastDisconnectStatus": 401,
                "lastError": "Reconnect attempts exhausted (30)",
            }
        }

    monkeypatch.setattr(
        "yeoman_overseer.trigger.checks._whatsapp_bridge_health",
        fake_health,
    )

    result = run_check("whatsapp_bridge_connected", target="default")

    assert result.value is False
    assert "connected=False" in result.detail
    assert "lastDisconnectStatus=401" in result.detail
    assert "Reconnect attempts exhausted" in result.detail

def test_run_check_unknown() -> None:
    with pytest.raises(ValueError, match="Unknown check"):
        run_check("nonexistent_check", target="x")
