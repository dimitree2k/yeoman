from __future__ import annotations

import os
from pathlib import Path

from yeoman_gateway.cli import doctor_commands


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

    expected = tmp_path / "data" / "ops" / "cron-jobs.json"
    assert captured["YEOMAN_CRON_STORE_PATH"] == str(expected)
    script = production_script.read_text(encoding="utf-8")
    assert 'cron_jobs_file="${YEOMAN_CRON_STORE_PATH:?YEOMAN_CRON_STORE_PATH is required}"' in script
