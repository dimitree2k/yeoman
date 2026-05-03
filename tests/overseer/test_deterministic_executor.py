"""Tests for deterministic action executor."""
from __future__ import annotations

import pytest
from yeoman_overseer.comms.cascading import CascadingComms
from yeoman_overseer.executor.deterministic import DeterministicExecutor


class FakeCommsChannel:
    def __init__(self) -> None:
        self.sent: list[str] = []
    @property
    def name(self) -> str:
        return "fake"
    async def send(self, message: str) -> None:
        self.sent.append(message)

@pytest.mark.asyncio
async def test_execute_alert() -> None:
    ch = FakeCommsChannel()
    comms = CascadingComms(channels=[ch])
    executor = DeterministicExecutor(comms=comms)
    result = await executor.execute("alert", target="owner", message="gateway down")
    assert result.success is True
    assert ch.sent == ["🔴 CRITICAL gateway down"]

@pytest.mark.asyncio
async def test_execute_restart_unknown_service() -> None:
    comms = CascadingComms(channels=[], local_log=True)
    executor = DeterministicExecutor(comms=comms)
    result = await executor.execute("restart_service", target="nonexistent-service-12345")
    assert result.success is False

@pytest.mark.asyncio
async def test_execute_unknown_action() -> None:
    comms = CascadingComms(channels=[], local_log=True)
    executor = DeterministicExecutor(comms=comms)
    result = await executor.execute("nonexistent_action", target="x")
    assert result.success is False
    assert "unknown" in result.detail.lower()

@pytest.mark.asyncio
async def test_execute_noop() -> None:
    comms = CascadingComms(channels=[], local_log=True)
    executor = DeterministicExecutor(comms=comms)
    result = await executor.execute("noop", target="x")
    assert result.success is True
