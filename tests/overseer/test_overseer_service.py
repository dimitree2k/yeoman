"""Tests for OverseerService lifecycle."""
from __future__ import annotations

import asyncio
from pathlib import Path
from textwrap import dedent

import pytest
from yeoman_overseer.executor import deterministic
from yeoman_overseer.executor.stale_agent_sessions import CleanupResult
from yeoman_overseer.service import OverseerConfig, OverseerService
from yeoman_overseer.trigger.checks import CheckResult

RUNBOOK = dedent("""\
    ---
    name: test-noop
    domain: ops
    enabled: true
    version: 1
    trigger:
      kind: cron
      expr: "0 0 1 1 *"
    escalate_to_llm: false
    safety:
      max_actions_per_hour: 10
      cooldown_s: 0
    ---
    # Test
    ## Actions
    1. noop
""")

CLEANUP_RUNBOOK = dedent("""\
    ---
    name: ops-stale-agent-session-cleanup
    domain: ops
    enabled: true
    version: 1
    trigger:
      kind: cron
      expr: "0 4 * * *"
    escalate_to_llm: false
    safety:
      max_actions_per_hour: 5
      cooldown_s: 3600
    ---
    # Stale Agent Session Cleanup
    ## Actions
    - action: cleanup_stale_agent_sessions
      target: mosh-agent-sessions
      min_age_seconds: "3600"
""")

@pytest.fixture
def overseer_dir(tmp_path: Path) -> Path:
    d = tmp_path / "overseer"
    runbooks = d / "runbooks"
    runbooks.mkdir(parents=True)
    (runbooks / "test-noop.md").write_text(RUNBOOK)
    return d

def test_config_defaults() -> None:
    cfg = OverseerConfig()
    assert cfg.tick_interval_s == 1.0
    assert cfg.actions_per_hour == 30
    assert cfg.llm_calls_per_day == 80
    assert cfg.llm_tokens_per_day == 2_000_000

@pytest.mark.asyncio
async def test_service_init_and_stop(overseer_dir: Path) -> None:
    cfg = OverseerConfig()
    service = OverseerService(data_dir=overseer_dir, socket_path=overseer_dir / "test.sock", config=cfg)
    await service.init()
    assert (overseer_dir / ".git").is_dir()
    assert len(service.runbooks) == 1
    assert service.runbooks[0].meta.name == "test-noop"
    await service.stop()

@pytest.mark.asyncio
async def test_service_run_single_tick(overseer_dir: Path) -> None:
    cfg = OverseerConfig(tick_interval_s=0.01)
    service = OverseerService(data_dir=overseer_dir, socket_path=overseer_dir / "test.sock", config=cfg)
    await service.init()
    task = asyncio.create_task(service.run())
    await asyncio.sleep(0.05)
    service.request_stop()
    await task
    assert (overseer_dir / "state.json").exists()

@pytest.mark.asyncio
async def test_service_executes_deterministic_runbook_actions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    d = tmp_path / "overseer"
    runbooks = d / "runbooks"
    runbooks.mkdir(parents=True)
    (runbooks / "cleanup.md").write_text(CLEANUP_RUNBOOK)

    async def fake_cleanup(*, min_age_seconds: int, dry_run: bool) -> CleanupResult:
        assert min_age_seconds == 3600
        assert dry_run is False
        return CleanupResult(killed_pids=[123], skipped_young=1, skipped_non_agent=0)

    monkeypatch.setattr(deterministic, "cleanup_stale_agent_sessions", fake_cleanup)

    service = OverseerService(data_dir=d, socket_path=d / "test.sock", config=OverseerConfig())
    await service.init()
    await service._on_runbook_triggered(service.runbooks[0], check_result=CheckResult(value=True))

    assert service._audit is not None
    entries = service._audit.read_recent(limit=1)
    assert entries[0]["runbook"] == "ops-stale-agent-session-cleanup"
    assert entries[0]["action"] == "cleanup_stale_agent_sessions"
    assert "killed 1" in entries[0]["result"]
