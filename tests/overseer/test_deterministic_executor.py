"""Tests for deterministic action executor."""
from __future__ import annotations

import pytest
from yeoman_overseer.comms.cascading import CascadingComms
from yeoman_overseer.executor import deterministic
from yeoman_overseer.executor.deterministic import DeterministicExecutor
from yeoman_overseer.executor.stale_agent_sessions import CleanupResult


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
async def test_execute_restart_fails_when_service_is_not_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeProcess:
        def __init__(
            self,
            returncode: int,
            stdout: bytes = b"",
            stderr: bytes = b"",
        ) -> None:
            self.returncode = returncode
            self._stdout = stdout
            self._stderr = stderr

        async def communicate(self) -> tuple[bytes, bytes]:
            return self._stdout, self._stderr

    calls: list[tuple[str, ...]] = []

    async def fake_create_subprocess_exec(
        *args: str,
        stdout: object,
        stderr: object,
    ) -> FakeProcess:
        calls.append(args)
        if args[:3] == ("systemctl", "--user", "restart"):
            return FakeProcess(0)
        if args[:3] == ("systemctl", "--user", "is-active"):
            return FakeProcess(3, stdout=b"failed\n")
        raise AssertionError(f"unexpected command: {args}")

    monkeypatch.setattr(
        deterministic.asyncio,
        "create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    sleep_calls: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    monkeypatch.setattr(deterministic.asyncio, "sleep", fake_sleep)

    comms = CascadingComms(channels=[], local_log=True)
    executor = DeterministicExecutor(comms=comms)
    result = await executor.execute("restart_service", target="yeoman-bridge.service")

    assert result.success is False
    assert "not active" in result.detail
    assert calls == [
        ("systemctl", "--user", "restart", "yeoman-bridge.service"),
        ("systemctl", "--user", "is-active", "--quiet", "yeoman-bridge.service"),
    ]
    assert sleep_calls == [2.0]

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

@pytest.mark.asyncio
async def test_execute_cleanup_stale_agent_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[int, bool]] = []

    async def fake_cleanup(*, min_age_seconds: int, dry_run: bool) -> CleanupResult:
        calls.append((min_age_seconds, dry_run))
        return CleanupResult(killed_pids=[123, 456], skipped_young=1, skipped_non_agent=2)

    monkeypatch.setattr(deterministic, "cleanup_stale_agent_sessions", fake_cleanup)

    comms = CascadingComms(channels=[], local_log=True)
    executor = DeterministicExecutor(comms=comms)
    result = await executor.execute(
        "cleanup_stale_agent_sessions",
        target="mosh-agent-sessions",
        min_age_seconds="3600",
        dry_run="true",
    )

    assert result.success is True
    assert calls == [(3600, True)]
    assert "would kill 2" in result.detail
    assert "skipped_young=1" in result.detail
