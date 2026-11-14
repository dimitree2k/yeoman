"""Channel start/stop failures name the exception type, even when its message is empty."""
from __future__ import annotations

import pytest
from loguru import logger
from yeoman_gateway.channels.manager import ChannelManager


class _FailingChannel:
    async def start(self) -> None:
        raise TimeoutError

    async def stop(self) -> None:
        raise TimeoutError


def _manager() -> ChannelManager:
    manager = ChannelManager.__new__(ChannelManager)
    manager.channels = {"whatsapp": _FailingChannel()}  # type: ignore[dict-item]
    manager._dispatch_task = None
    manager._reaction_dispatch_task = None
    return manager


@pytest.fixture
def errors() -> list[str]:
    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(str(m)), level="ERROR", format="{message}")
    yield lines
    logger.remove(sink)


@pytest.mark.asyncio
async def test_stop_failure_logs_error_type(errors: list[str]) -> None:
    await _manager().stop_all()
    assert errors == ["Error stopping whatsapp error_type=TimeoutError: \n"]


@pytest.mark.asyncio
async def test_start_failure_logs_error_type(errors: list[str]) -> None:
    await _manager()._start_channel("whatsapp", _FailingChannel())  # type: ignore[arg-type]
    assert errors == ["Failed to start channel whatsapp error_type=TimeoutError: \n"]
