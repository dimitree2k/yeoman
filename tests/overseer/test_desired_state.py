"""Owner intent (desired state) keeps the Overseer from restarting a paused service."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent
from unittest.mock import AsyncMock

import pytest
from typer.testing import CliRunner
from yeoman_overseer.comms.cascading import CascadingComms
from yeoman_overseer.executor.deterministic import DeterministicExecutor
from yeoman_overseer.maintenance import MaintenanceManager
from yeoman_overseer.runbook.parser import parse_runbook
from yeoman_overseer.safety.causal import CausalChainDetector
from yeoman_overseer.safety.circuit_breaker import CircuitBreaker
from yeoman_overseer.safety.rate_limiter import RateLimiter
from yeoman_overseer.state import OverseerState
from yeoman_overseer.trigger.evaluator import TriggerEvaluator
from yeoman_overseer.trigger.lock import LockManager
from yeoman_shared.desired_state import DesiredStateStore, canonical_service, parse_duration


def test_default_is_running(tmp_path: Path) -> None:
    assert DesiredStateStore(tmp_path / "d.json").get("yeoman-bridge").desired == "running"


def test_pause_persists_across_instances(tmp_path: Path) -> None:
    path = tmp_path / "d.json"
    DesiredStateStore(path).set("bridge", "paused", reason="needs_human: 401")
    record = DesiredStateStore(path).get("yeoman-bridge.service")
    assert record.desired == "paused"
    assert record.reason == "needs_human: 401"


def test_pause_expires_on_wall_clock(tmp_path: Path) -> None:
    now = {"t": 1_000.0}
    store = DesiredStateStore(tmp_path / "d.json", clock=lambda: now["t"])
    store.set("bridge", "paused", until=1_060.0)
    assert store.is_held("bridge")
    now["t"] = 1_060.0
    assert not store.is_held("bridge")


def test_resume_removes_record(tmp_path: Path) -> None:
    store = DesiredStateStore(tmp_path / "d.json")
    store.set("telegram", "stopped")
    assert store.is_held("yeoman-gateway/telegram")
    store.set("telegram", "running")
    assert store.all() == {}


def test_unreadable_file_reads_as_running(tmp_path: Path) -> None:
    path = tmp_path / "d.json"
    path.write_text("{not json")
    assert DesiredStateStore(path).get("bridge").desired == "running"


def test_aliases_and_durations() -> None:
    assert canonical_service("whatsapp") == "yeoman-bridge"
    assert canonical_service("yeoman-gateway.service") == "yeoman-gateway"
    assert parse_duration("12h") == 43_200
    with pytest.raises(ValueError):
        parse_duration("12")


def _restart_runbook(tmp_path: Path) -> Path:
    path = tmp_path / "health-bridge.md"
    path.write_text(
        dedent("""\
        ---
        name: health-bridge
        domain: health
        enabled: true
        version: 1
        trigger:
          kind: poll
          interval_s: 1
          condition:
            check: process_alive
            target: "1"
            operator: "=="
            value: true
        escalate_to_llm: false
        safety:
          max_actions_per_hour: 10
          cooldown_s: 0
        ---
        # Bridge
        ## Actions
        - action: restart_service
          target: yeoman-bridge.service
        """)
    )
    return path


def _evaluator(rb, callback, store: DesiredStateStore) -> TriggerEvaluator:
    return TriggerEvaluator(
        runbooks=[rb],
        on_triggered=callback,
        lock_manager=LockManager(),
        circuit_breaker=CircuitBreaker(),
        rate_limiter=RateLimiter(),
        causal_detector=CausalChainDetector(),
        maintenance=MaintenanceManager(),
        state=OverseerState(),
        desired=store,
    )


@pytest.mark.asyncio
async def test_evaluator_skips_runbook_for_paused_unit(tmp_path: Path) -> None:
    rb = parse_runbook(_restart_runbook(tmp_path))
    store = DesiredStateStore(tmp_path / "d.json")
    store.set("bridge", "paused")
    callback = AsyncMock()
    await _evaluator(rb, callback, store).tick()
    callback.assert_not_called()


@pytest.mark.asyncio
async def test_evaluator_fires_when_unit_is_running(tmp_path: Path) -> None:
    rb = parse_runbook(_restart_runbook(tmp_path))
    callback = AsyncMock()
    await _evaluator(rb, callback, DesiredStateStore(tmp_path / "d.json")).tick()
    callback.assert_called_once()


@pytest.mark.asyncio
async def test_executor_refuses_restart_of_held_unit(monkeypatch: pytest.MonkeyPatch) -> None:
    DesiredStateStore().set("bridge", "stopped", reason="owner")

    async def boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("systemctl must not run for a held unit")

    monkeypatch.setattr("asyncio.create_subprocess_exec", boom)
    executor = DeterministicExecutor(comms=CascadingComms(channels=[], local_log=True))
    result = await executor.execute("restart_service", target="yeoman-bridge.service")
    assert result.success is True
    assert "Skipped restart" in result.detail
    DesiredStateStore().set("bridge", "running")


def test_cli_pause_and_resume(monkeypatch: pytest.MonkeyPatch) -> None:
    from yeoman_gateway.cli import overseer_commands
    from yeoman_gateway.cli.core import app

    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        overseer_commands, "_systemctl", lambda verb, unit: calls.append((verb, unit))
    )
    runner = CliRunner()

    result = runner.invoke(
        app, ["overseer", "pause", "bridge", "--for", "8h", "--reason", "relink"]
    )
    assert result.exit_code == 0, result.output
    assert DesiredStateStore().get("bridge").desired == "paused"
    assert calls == [("stop", "yeoman-bridge.service")]

    result = runner.invoke(app, ["overseer", "pause", "telegram"])
    assert result.exit_code == 0, result.output
    assert calls == [("stop", "yeoman-bridge.service")]  # in-process channel: no systemctl

    result = runner.invoke(app, ["overseer", "resume", "bridge"])
    assert result.exit_code == 0, result.output
    assert DesiredStateStore().get("bridge").desired == "running"
    assert calls[-1] == ("start", "yeoman-bridge.service")
    runner.invoke(app, ["overseer", "resume", "telegram"])
