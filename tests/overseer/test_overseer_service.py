"""Tests for OverseerService lifecycle."""
from __future__ import annotations
import asyncio
from pathlib import Path
from textwrap import dedent
import pytest
from yeoman_overseer.service import OverseerService, OverseerConfig

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
    assert cfg.llm_calls_per_day == 20

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
