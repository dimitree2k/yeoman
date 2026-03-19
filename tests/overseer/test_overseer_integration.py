"""Integration smoke test — full overseer lifecycle."""
from __future__ import annotations
import asyncio
import json
from pathlib import Path
from textwrap import dedent
import pytest
from yeoman_overseer.service import OverseerService, OverseerConfig

HEALTH_RUNBOOK = dedent("""\
    ---
    name: integration-health
    domain: health
    enabled: true
    version: 1
    trigger:
      kind: poll
      interval_s: 1
      condition:
        check: disk_usage_above
        target: /
        operator: ">="
        value: 0
    escalate_to_llm: false
    safety:
      max_actions_per_hour: 100
      cooldown_s: 0
    ---
    # Integration Health Check
    ## Actions
    1. noop
""")

CRON_RUNBOOK = dedent("""\
    ---
    name: integration-cron
    domain: ops
    enabled: true
    version: 1
    trigger:
      kind: cron
      expr: "* * * * *"
    escalate_to_llm: false
    safety:
      max_actions_per_hour: 100
      cooldown_s: 0
    ---
    # Integration Cron
    ## Actions
    1. noop
""")

@pytest.fixture
def overseer_env(tmp_path: Path) -> Path:
    d = tmp_path / "overseer"
    runbooks = d / "runbooks"
    runbooks.mkdir(parents=True)
    (runbooks / "health.md").write_text(HEALTH_RUNBOOK)
    (runbooks / "cron.md").write_text(CRON_RUNBOOK)
    return d

@pytest.mark.asyncio
async def test_full_lifecycle(overseer_env: Path) -> None:
    config = OverseerConfig(tick_interval_s=0.05)
    service = OverseerService(data_dir=overseer_env, socket_path=overseer_env / "test.sock", config=config)
    await service.init()
    assert len(service.runbooks) == 2
    assert (overseer_env / ".git").is_dir()
    task = asyncio.create_task(service.run())
    await asyncio.sleep(0.2)
    service.request_stop()
    await task
    state_path = overseer_env / "state.json"
    assert state_path.exists()
    state = json.loads(state_path.read_text())
    assert state["heartbeat_ts"] is not None
    audit_dir = overseer_env / "audit"
    assert audit_dir.is_dir()
    log_files = list(audit_dir.glob("????-??-??.jsonl"))
    assert len(log_files) >= 1
    entries = log_files[0].read_text().strip().splitlines()
    assert len(entries) >= 1
    entry = json.loads(entries[0])
    assert "runbook" in entry
    assert "ts" in entry
