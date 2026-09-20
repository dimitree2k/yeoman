"""Tests for Participation burst-triggered wakeups."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from yeoman_gateway.bus.events import InboundObservedEvent
from yeoman_gateway.consciousness.burst import BurstObserver
from yeoman_shared.config.schema import Config, ConsciousnessConfig


def _config(**overrides: object) -> Config:
    payload = {
        "enabled": True,
        "burstEnabled": True,
        "burstThresholdMessages": 3,
        "burstWindowMinutes": 10,
        "ownerDmDefaultEnabled": False,
        "defaultDailyCap": 1,
    }
    payload.update(overrides)
    return Config(consciousness=ConsciousnessConfig.model_validate(payload))


def _event(
    *,
    at: datetime,
    chat_id: str = "group@g.us",
    sender_id: str = "user@s.whatsapp.net",
    content: str = "message",
    mentioned_bot: bool = False,
    reply_to_bot: bool = False,
    from_me: bool = False,
) -> InboundObservedEvent:
    return InboundObservedEvent(
        channel="whatsapp",
        chat_id=chat_id,
        sender_id=sender_id,
        content=content,
        timestamp=at.timestamp(),
        is_group=True,
        metadata={
            "mentioned_bot": mentioned_bot,
            "reply_to_bot": reply_to_bot,
            "from_me": from_me,
        },
    )


@pytest.mark.asyncio
async def test_burst_observer_fires_only_after_threshold_inside_window(tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []
    observer = BurstObserver(
        config=_config(),
        state_path=tmp_path / "burst.json",
        on_burst=lambda channel, chat_id: calls.append((channel, chat_id)),
        is_eligible=lambda channel, chat_id: True,
    )
    base = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)

    await observer.handle(_event(at=base))
    await observer.handle(_event(at=base + timedelta(minutes=9)))
    await observer.handle(_event(at=base + timedelta(minutes=11)))
    await observer.handle(_event(at=base + timedelta(minutes=12)))

    assert calls == [("whatsapp", "group@g.us")]


@pytest.mark.asyncio
async def test_burst_observer_clears_window_after_fire(tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []
    observer = BurstObserver(
        config=_config(defaultDailyCap=3),
        state_path=tmp_path / "burst.json",
        on_burst=lambda channel, chat_id: calls.append((channel, chat_id)),
        is_eligible=lambda channel, chat_id: True,
    )
    base = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)

    for index in range(4):
        await observer.handle(_event(at=base + timedelta(minutes=index)))

    assert calls == [("whatsapp", "group@g.us")]


@pytest.mark.asyncio
async def test_burst_observer_is_disabled_by_default(tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []
    observer = BurstObserver(
        config=_config(burstEnabled=False),
        state_path=tmp_path / "burst.json",
        on_burst=lambda channel, chat_id: calls.append((channel, chat_id)),
        is_eligible=lambda channel, chat_id: True,
    )
    base = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)

    for index in range(3):
        await observer.handle(_event(at=base + timedelta(minutes=index)))

    assert calls == []


@pytest.mark.asyncio
async def test_burst_observer_ignores_direct_bot_interaction_messages(tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []
    observer = BurstObserver(
        config=_config(),
        state_path=tmp_path / "burst.json",
        on_burst=lambda channel, chat_id: calls.append((channel, chat_id)),
        is_eligible=lambda channel, chat_id: True,
    )
    base = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)

    await observer.handle(_event(at=base, mentioned_bot=True))
    await observer.handle(_event(at=base + timedelta(minutes=1), reply_to_bot=True))
    await observer.handle(_event(at=base + timedelta(minutes=2), from_me=True))
    await observer.handle(_event(at=base + timedelta(minutes=3)))
    await observer.handle(_event(at=base + timedelta(minutes=4)))

    assert calls == []


@pytest.mark.asyncio
async def test_burst_observer_suppresses_window_after_direct_bot_interaction(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, str]] = []
    observer = BurstObserver(
        config=_config(burstThresholdMessages=3, burstWindowMinutes=10),
        state_path=tmp_path / "burst.json",
        on_burst=lambda channel, chat_id: calls.append((channel, chat_id)),
        is_eligible=lambda channel, chat_id: True,
    )
    base = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)

    await observer.handle(_event(at=base, mentioned_bot=True))
    for index in range(1, 4):
        await observer.handle(_event(at=base + timedelta(minutes=index)))

    assert calls == []


@pytest.mark.asyncio
async def test_burst_observer_suppresses_plain_name_interaction_window(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, str]] = []
    observer = BurstObserver(
        config=_config(burstThresholdMessages=3, burstWindowMinutes=10),
        state_path=tmp_path / "burst.json",
        on_burst=lambda channel, chat_id: calls.append((channel, chat_id)),
        is_eligible=lambda channel, chat_id: True,
    )
    base = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)

    await observer.handle(_event(at=base, content="Arvid kannst du Nokia checken"))
    for index in range(1, 4):
        await observer.handle(_event(at=base + timedelta(minutes=index)))

    assert calls == []


@pytest.mark.asyncio
async def test_burst_observer_suppresses_recent_assistant_followup_window(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, str]] = []
    base = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)
    assistant_wall_time = datetime.fromtimestamp(base.timestamp()) - timedelta(seconds=5)

    class _Sessions:
        def get_or_create(self, key: str) -> object:
            assert key == "whatsapp:group@g.us"

            class _Session:
                messages = [
                    {
                        "role": "assistant",
                        "content": "Nokia ist kurzfristig newsgetrieben.",
                        "timestamp": assistant_wall_time.isoformat(),
                    }
                ]

            return _Session()

    observer = BurstObserver(
        config=_config(burstThresholdMessages=3, burstWindowMinutes=10),
        state_path=tmp_path / "burst.json",
        on_burst=lambda channel, chat_id: calls.append((channel, chat_id)),
        is_eligible=lambda channel, chat_id: True,
        session_manager=_Sessions(),
    )

    await observer.handle(_event(at=base, content="was meinst du bei Intel"))
    for index in range(1, 4):
        await observer.handle(_event(at=base + timedelta(minutes=index)))

    assert calls == []


@pytest.mark.asyncio
async def test_burst_debounce_state_survives_restart(tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []
    state_path = tmp_path / "burst.json"
    base = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)
    first = BurstObserver(
        config=_config(),
        state_path=state_path,
        on_burst=lambda channel, chat_id: calls.append((channel, chat_id)),
        is_eligible=lambda channel, chat_id: True,
    )
    for index in range(3):
        await first.handle(_event(at=base + timedelta(minutes=index)))

    restarted = BurstObserver(
        config=_config(),
        state_path=state_path,
        on_burst=lambda channel, chat_id: calls.append((channel, chat_id)),
        is_eligible=lambda channel, chat_id: True,
    )
    for index in range(3):
        await restarted.handle(_event(at=base + timedelta(minutes=20 + index)))

    assert calls == [("whatsapp", "group@g.us")]


@pytest.mark.asyncio
async def test_burst_state_is_not_saved_when_callback_fails(tmp_path: Path) -> None:
    state_path = tmp_path / "burst.json"
    base = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)

    async def failing_burst(channel: str, chat_id: str) -> None:
        del channel, chat_id
        raise RuntimeError("planner failed")

    first = BurstObserver(
        config=_config(),
        state_path=state_path,
        on_burst=failing_burst,
        is_eligible=lambda channel, chat_id: True,
    )

    with pytest.raises(RuntimeError, match="planner failed"):
        for index in range(3):
            await first.handle(_event(at=base + timedelta(minutes=index)))

    assert not state_path.exists()

    calls: list[tuple[str, str]] = []
    restarted = BurstObserver(
        config=_config(),
        state_path=state_path,
        on_burst=lambda channel, chat_id: calls.append((channel, chat_id)),
        is_eligible=lambda channel, chat_id: True,
    )
    for index in range(3):
        await restarted.handle(_event(at=base + timedelta(minutes=20 + index)))

    assert calls == [("whatsapp", "group@g.us")]


@pytest.mark.asyncio
async def test_burst_observer_respects_configurable_daily_cap(tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []
    cfg = _config(defaultDailyCap=3)
    state_path = tmp_path / "burst.json"
    base = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)

    observer = BurstObserver(
        config=cfg,
        state_path=state_path,
        on_burst=lambda channel, chat_id: calls.append((channel, chat_id)),
        is_eligible=lambda channel, chat_id: True,
    )

    for fire in range(4):
        offset = fire * 30
        for index in range(3):
            await observer.handle(_event(at=base + timedelta(minutes=offset + index)))

    assert calls == [("whatsapp", "group@g.us")] * 3

    saved = json.loads(state_path.read_text())
    fires_today = saved["fires_today"]["whatsapp:group@g.us"]
    assert fires_today["count"] == 3
    assert fires_today["date"] == "2026-04-26"


@pytest.mark.asyncio
async def test_burst_observer_migrates_legacy_state_format(tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []
    state_path = tmp_path / "burst.json"
    state_path.write_text(
        json.dumps({"last_fired_day": {"whatsapp:group@g.us": "2026-04-26"}})
    )
    cfg = _config(defaultDailyCap=2)
    base = datetime(2026, 4, 26, 12, 30, tzinfo=UTC)

    observer = BurstObserver(
        config=cfg,
        state_path=state_path,
        on_burst=lambda channel, chat_id: calls.append((channel, chat_id)),
        is_eligible=lambda channel, chat_id: True,
    )

    for index in range(3):
        await observer.handle(_event(at=base + timedelta(minutes=index)))

    assert calls == [("whatsapp", "group@g.us")]
    saved = json.loads(state_path.read_text())
    assert saved["fires_today"]["whatsapp:group@g.us"]["count"] == 2
