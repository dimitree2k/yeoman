from __future__ import annotations

from pathlib import Path

from yeoman_gateway.cli import doctor_commands
from yeoman_shared.utils.helpers import get_operational_store_path


def test_doctor_checks_resolved_cron_store(monkeypatch, tmp_path):
    production_script = (
        Path(doctor_commands.__file__).resolve().parent.parent
        / "skills"
        / "agent-doctor"
        / "scripts"
        / "doctor.sh"
    )
    monkeypatch.setenv("YEOMAN_HOME", str(tmp_path))
    monkeypatch.setattr(doctor_commands, "_workspace_doctor_script", lambda: tmp_path / "missing.sh")
    monkeypatch.setattr(doctor_commands, "_builtin_doctor_script", lambda: tmp_path / "doctor.sh")
    (tmp_path / "doctor.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    captured = {}

    def fake_run(args, *, env, check):
        captured.update(env)
        return type("Completed", (), {"returncode": 0})()

    monkeypatch.setattr(doctor_commands.subprocess, "run", fake_run)

    doctor_commands.doctor()

    expected = get_operational_store_path("cron", data_dir=tmp_path / "data")
    assert captured["YEOMAN_CRON_STORE_PATH"] == str(expected)
    script = production_script.read_text(encoding="utf-8")
    assert 'cron_jobs_file="${YEOMAN_CRON_STORE_PATH:?YEOMAN_CRON_STORE_PATH is required}"' in script


def test_doctor_does_not_treat_frozen_session_state_as_active():
    script = (
        Path(doctor_commands.__file__).resolve().parent.parent
        / "skills"
        / "agent-doctor"
        / "scripts"
        / "doctor.sh"
    ).read_text(encoding="utf-8")
    problems = (
        Path(doctor_commands.__file__).resolve().parent.parent
        / "skills"
        / "agent-doctor"
        / "references"
        / "problems.md"
    ).read_text(encoding="utf-8")

    assert "MEM-004" not in script
    assert "MEM-004" not in problems
    assert "session-state WAL" not in script
    assert "session-state WAL" not in problems
