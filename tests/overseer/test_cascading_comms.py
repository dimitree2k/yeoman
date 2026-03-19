"""Tests for cascading communication delivery."""
from __future__ import annotations
import pytest
from yeoman_overseer.comms.cascading import CascadingComms, CommsChannel

class FakeChannel(CommsChannel):
    def __init__(self, *, should_fail: bool = False) -> None:
        self.sent: list[str] = []
        self._should_fail = should_fail
    async def send(self, message: str) -> None:
        if self._should_fail:
            raise ConnectionError("channel unavailable")
        self.sent.append(message)
    @property
    def name(self) -> str:
        return "fake"

@pytest.mark.asyncio
async def test_sends_via_first_available() -> None:
    ch1 = FakeChannel()
    ch2 = FakeChannel()
    comms = CascadingComms(channels=[ch1, ch2])
    await comms.send("test message")
    assert ch1.sent == ["test message"]
    assert ch2.sent == []

@pytest.mark.asyncio
async def test_falls_back_on_failure() -> None:
    ch1 = FakeChannel(should_fail=True)
    ch2 = FakeChannel()
    comms = CascadingComms(channels=[ch1, ch2])
    await comms.send("test message")
    assert ch2.sent == ["test message"]

@pytest.mark.asyncio
async def test_all_channels_fail_raises() -> None:
    ch1 = FakeChannel(should_fail=True)
    ch2 = FakeChannel(should_fail=True)
    comms = CascadingComms(channels=[ch1, ch2])
    with pytest.raises(RuntimeError, match="All.*channels.*failed"):
        await comms.send("test message")

@pytest.mark.asyncio
async def test_local_fallback_always_works() -> None:
    ch1 = FakeChannel(should_fail=True)
    comms = CascadingComms(channels=[ch1], local_log=True)
    await comms.send("test message")
    assert len(comms.local_messages) == 1
