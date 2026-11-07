"""Owner pause and a rejected token keep the Telegram channel from polling."""

from __future__ import annotations

import pytest
from telegram.error import InvalidToken, NetworkError
from yeoman_gateway.bus.queue import MessageBus
from yeoman_gateway.channels import telegram as telegram_module
from yeoman_gateway.channels.telegram import TELEGRAM_SERVICE, TelegramChannel
from yeoman_shared.config.schema import TelegramConfig
from yeoman_shared.desired_state import DesiredStateStore


@pytest.fixture(autouse=True)
def _clear_hold():
    yield
    DesiredStateStore().set(TELEGRAM_SERVICE, "running")


@pytest.mark.asyncio
async def test_paused_channel_never_builds_the_bot(monkeypatch: pytest.MonkeyPatch) -> None:
    DesiredStateStore().set("telegram", "paused", reason="owner")
    channel = TelegramChannel(TelegramConfig(token="123:abc"), MessageBus())
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        channel._running = False

    async def must_not_poll() -> None:
        raise AssertionError("paused channel must not poll")

    monkeypatch.setattr(telegram_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(channel, "_poll_until_held", must_not_poll)
    await channel.start()
    assert sleeps == [30.0]


def test_invalid_token_pauses_channel() -> None:
    channel = TelegramChannel(TelegramConfig(token="123:abc"), MessageBus())
    channel._on_polling_error(InvalidToken())
    record = DesiredStateStore().get(TELEGRAM_SERVICE)
    assert record.desired == "paused"
    assert record.set_by == "gateway"
    assert "InvalidToken" in record.reason


def test_network_error_does_not_pause() -> None:
    channel = TelegramChannel(TelegramConfig(token="123:abc"), MessageBus())
    channel._on_polling_error(NetworkError("down"))
    assert DesiredStateStore().get(TELEGRAM_SERVICE).desired == "running"


def test_network_errors_spend_the_budget_then_stop_polling() -> None:
    from yeoman_shared.utils.helpers import get_operational_store_path

    get_operational_store_path("telegram_connect_budget").unlink(missing_ok=True)
    channel = TelegramChannel(TelegramConfig(token="123:abc"), MessageBus())
    for _ in range(4):
        channel._on_polling_error(NetworkError("down"))
        assert channel._budget_stop is False
    channel._on_polling_error(NetworkError("down"))
    assert channel._budget_stop is True
    channel._connect_budget().reset()
