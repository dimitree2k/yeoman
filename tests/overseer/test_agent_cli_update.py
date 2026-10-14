"""Tests for the update_agent_clis deterministic action."""
from __future__ import annotations

from pathlib import Path

import pytest
from yeoman_overseer.comms.cascading import CascadingComms
from yeoman_overseer.executor import agent_cli_update
from yeoman_overseer.executor.agent_cli_update import update_agent_clis
from yeoman_overseer.executor.deterministic import (
    DeterministicExecutor,
    parse_deterministic_actions,
)
from yeoman_overseer.runbook.parser import parse_runbook

STARTER_RUNBOOK = (
    Path(__file__).resolve().parents[2]
    / "packages/overseer/yeoman_overseer/starter_runbooks/ops-agent-cli-update.md"
)


class _Comms:
    def __init__(self) -> None:
        self.sent: list[str] = []

    @property
    def name(self) -> str:
        return "fake"

    async def send(self, message: str) -> None:
        self.sent.append(message)


def _fake_cli(bin_dir: Path, name: str, *, new_version: str, update_exit: int = 0) -> None:
    """A CLI whose `update` bumps the version that `--version` prints."""
    state = bin_dir / f"{name}.version"
    state.write_text("1.0.0\n")
    script = bin_dir / name
    script.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = "--version" ]; then cat "{state}"; exit 0; fi\n'
        f'if [ "$1" = "update" ]; then\n'
        f'  if [ {update_exit} -ne 0 ]; then echo "network unreachable"; exit {update_exit}; fi\n'
        f'  echo "{new_version}" > "{state}"; echo "updated"; exit 0\n'
        "fi\n"
        "exit 2\n"
    )
    script.chmod(0o755)


@pytest.mark.asyncio
async def test_update_reports_before_and_after_versions(tmp_path: Path) -> None:
    _fake_cli(tmp_path, "claude", new_version="1.1.0")
    _fake_cli(tmp_path, "codex", new_version="1.0.0")

    results = await update_agent_clis(("claude", "codex"), bin_dir=tmp_path)

    assert [r.describe() for r in results] == [
        "claude: 1.0.0 -> 1.1.0",
        "codex: 1.0.0 (unchanged)",
    ]


@pytest.mark.asyncio
async def test_update_failure_and_missing_binary_are_reported(tmp_path: Path) -> None:
    _fake_cli(tmp_path, "claude", new_version="1.1.0", update_exit=1)

    claude, codex = await update_agent_clis(("claude", "codex"), bin_dir=tmp_path)

    assert claude.success is False and claude.error == "network unreachable"
    assert codex.success is False and "not found" in codex.error


@pytest.mark.asyncio
async def test_action_succeeds_quietly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_cli(tmp_path, "claude", new_version="1.1.0")
    monkeypatch.setattr(agent_cli_update.Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / ".local").mkdir()
    (tmp_path / ".local" / "bin").symlink_to(tmp_path)
    comms = _Comms()

    result = await DeterministicExecutor(comms=CascadingComms(channels=[comms])).execute(
        "update_agent_clis", target="claude"
    )

    assert result.success is True and result.detail == "claude: 1.0.0 -> 1.1.0"
    assert comms.sent == []


@pytest.mark.asyncio
async def test_action_alerts_owner_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agent_cli_update.Path, "home", classmethod(lambda cls: tmp_path))
    comms = _Comms()

    result = await DeterministicExecutor(comms=CascadingComms(channels=[comms])).execute(
        "update_agent_clis", target="codex"
    )

    assert result.success is False
    assert len(comms.sent) == 1 and "Agent CLI update problem: codex: failed" in comms.sent[0]


def test_starter_runbook_runs_twice_weekly_without_llm() -> None:
    runbook = parse_runbook(STARTER_RUNBOOK)
    assert runbook.meta.trigger.kind == "cron"
    assert runbook.meta.trigger.expr == "30 4 * * 1,4"
    assert runbook.meta.escalate_to_llm is False
    [action] = parse_deterministic_actions(runbook.body)
    assert action.action == "update_agent_clis" and action.target == "claude,codex"
