"""Tests for the lull-triggered consciousness observer."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from yeoman_gateway.bus.events import InboundObservedEvent
from yeoman_gateway.bus.queue import MessageBus
from yeoman_gateway.consciousness.log import SpeakupLog
from yeoman_gateway.consciousness.lull import LullObserver
from yeoman_gateway.consciousness.tools import ConsciousnessTools
from yeoman_gateway.policy.engine import PolicyEngine
from yeoman_gateway.policy.schema import PolicyConfig
from yeoman_gateway.storage.inbound_archive import InboundArchive
from yeoman_shared.config.schema import Config, ConsciousnessConfig


def _config(**overrides: object) -> Config:
    payload: dict[str, object] = {
        "enabled": True,
        "lullEnabled": True,
        "lullSilenceMinutes": 15,
        "lullActivityWindowMinutes": 60,
        "lullMinRecentActivity": 3,
        "lullCheckIntervalSeconds": 10,
        "defaultDailyCap": 3,
        "ownerDmDefaultEnabled": False,
    }
    payload.update(overrides)
    return Config(consciousness=ConsciousnessConfig.model_validate(payload))


class _FakeSecurity:
    def check_output(self, text: str, context: dict[str, object] | None = None) -> object:
        del text, context

        class _Decision:
            action = "allow"
            reason = "fake_allow"

        class _Result:
            decision = _Decision()
            sanitized_text = None

        return _Result()


def _policy(*, group_enabled: bool = True, daily_cap: int = 1) -> PolicyConfig:
    return PolicyConfig.model_validate(
        {
            "owners": {"whatsapp": ["owner@s.whatsapp.net"]},
            "channels": {
                "whatsapp": {
                    "chats": {
                        "group@g.us": {
                            "spontaneity": {
                                "enabled": group_enabled,
                                "profile": "balanced",
                                "dailyCap": daily_cap,
                                "preview": "owner_dm",
                            }
                        }
                    }
                }
            },
        }
    )


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
async def test_lull_observer_fires_after_silence_following_recent_activity(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, str]] = []
    base = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)
    now_holder = {"value": base.timestamp()}

    observer = LullObserver(
        config=_config(),
        state_path=tmp_path / "lull.json",
        on_lull=lambda channel, chat_id: calls.append((channel, chat_id)),
        is_eligible=lambda channel, chat_id: True,
        clock=lambda: now_holder["value"],
    )

    for index in range(4):
        await observer.handle(_event(at=base + timedelta(minutes=index)))

    now_holder["value"] = (base + timedelta(minutes=10)).timestamp()
    await observer._tick()
    assert calls == []

    now_holder["value"] = (base + timedelta(minutes=21)).timestamp()
    await observer._tick()
    assert calls == [("whatsapp", "group@g.us")]


@pytest.mark.asyncio
async def test_lull_observer_clears_activity_after_callback(tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []
    base = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)
    now_holder = {"value": base.timestamp()}

    observer = LullObserver(
        config=_config(),
        state_path=tmp_path / "lull.json",
        on_lull=lambda channel, chat_id: calls.append((channel, chat_id))
        or {"status": "silent_pass"},
        is_eligible=lambda channel, chat_id: True,
        clock=lambda: now_holder["value"],
    )

    for index in range(4):
        await observer.handle(_event(at=base + timedelta(minutes=index)))

    now_holder["value"] = (base + timedelta(minutes=21)).timestamp()
    await observer._tick()
    now_holder["value"] = (base + timedelta(minutes=22)).timestamp()
    await observer._tick()

    assert calls == [("whatsapp", "group@g.us")]


@pytest.mark.asyncio
async def test_lull_observer_skips_when_too_few_recent_activity(tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []
    base = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)
    now_holder = {"value": base.timestamp()}

    observer = LullObserver(
        config=_config(lullMinRecentActivity=5),
        state_path=tmp_path / "lull.json",
        on_lull=lambda channel, chat_id: calls.append((channel, chat_id)),
        is_eligible=lambda channel, chat_id: True,
        clock=lambda: now_holder["value"],
    )

    for index in range(3):
        await observer.handle(_event(at=base + timedelta(minutes=index)))

    now_holder["value"] = (base + timedelta(minutes=30)).timestamp()
    await observer._tick()
    assert calls == []


@pytest.mark.asyncio
async def test_lull_observer_skips_direct_bot_interaction(tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []
    base = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)
    now_holder = {"value": base.timestamp()}

    observer = LullObserver(
        config=_config(),
        state_path=tmp_path / "lull.json",
        on_lull=lambda channel, chat_id: calls.append((channel, chat_id)),
        is_eligible=lambda channel, chat_id: True,
        clock=lambda: now_holder["value"],
    )

    await observer.handle(_event(at=base, mentioned_bot=True))
    await observer.handle(_event(at=base + timedelta(minutes=1), reply_to_bot=True))
    await observer.handle(_event(at=base + timedelta(minutes=2), from_me=True))

    now_holder["value"] = (base + timedelta(minutes=30)).timestamp()
    await observer._tick()
    assert calls == []


@pytest.mark.asyncio
async def test_lull_observer_skips_plain_name_interaction(tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []
    base = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)
    now_holder = {"value": base.timestamp()}

    observer = LullObserver(
        config=_config(),
        state_path=tmp_path / "lull.json",
        on_lull=lambda channel, chat_id: calls.append((channel, chat_id)),
        is_eligible=lambda channel, chat_id: True,
        clock=lambda: now_holder["value"],
    )

    await observer.handle(_event(at=base, content="Arvid kannst du Nokia checken"))
    for index in range(1, 4):
        await observer.handle(_event(at=base + timedelta(minutes=index)))

    now_holder["value"] = (base + timedelta(minutes=30)).timestamp()
    await observer._tick()
    assert calls == []


@pytest.mark.asyncio
async def test_lull_observer_respects_daily_cap(tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []
    base = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)
    now_holder = {"value": base.timestamp()}

    observer = LullObserver(
        config=_config(defaultDailyCap=2, lullMinRecentActivity=2),
        state_path=tmp_path / "lull.json",
        on_lull=lambda channel, chat_id: calls.append((channel, chat_id)),
        is_eligible=lambda channel, chat_id: True,
        clock=lambda: now_holder["value"],
    )

    for fire in range(3):
        bucket_start = base + timedelta(hours=fire)
        await observer.handle(_event(at=bucket_start))
        await observer.handle(_event(at=bucket_start + timedelta(minutes=1)))
        now_holder["value"] = (bucket_start + timedelta(minutes=20)).timestamp()
        await observer._tick()

    assert calls == [("whatsapp", "group@g.us")] * 2


@pytest.mark.asyncio
async def test_lull_observer_disabled_when_flag_off(tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []
    base = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)
    now_holder = {"value": base.timestamp()}

    observer = LullObserver(
        config=_config(lullEnabled=False),
        state_path=tmp_path / "lull.json",
        on_lull=lambda channel, chat_id: calls.append((channel, chat_id)),
        is_eligible=lambda channel, chat_id: True,
        clock=lambda: now_holder["value"],
    )

    for index in range(4):
        await observer.handle(_event(at=base + timedelta(minutes=index)))

    now_holder["value"] = (base + timedelta(minutes=30)).timestamp()
    await observer._tick()
    assert calls == []


@pytest.mark.asyncio
async def test_lull_observer_persists_count(tmp_path: Path) -> None:
    state_path = tmp_path / "lull.json"
    base = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)
    now_holder = {"value": base.timestamp()}
    calls: list[tuple[str, str]] = []

    first = LullObserver(
        config=_config(),
        state_path=state_path,
        on_lull=lambda channel, chat_id: calls.append((channel, chat_id)),
        is_eligible=lambda channel, chat_id: True,
        clock=lambda: now_holder["value"],
    )
    for index in range(3):
        await first.handle(_event(at=base + timedelta(minutes=index)))
    now_holder["value"] = (base + timedelta(minutes=20)).timestamp()
    await first._tick()
    assert calls == [("whatsapp", "group@g.us")]

    saved = json.loads(state_path.read_text())
    assert saved["fires_today"]["whatsapp:group@g.us"]["count"] == 1


@pytest.mark.asyncio
async def test_lull_rejects_reply_to_outside_activity_window(tmp_path: Path) -> None:
    cfg = _config(lullActivityWindowMinutes=60)
    now = datetime(2026, 5, 2, 7, 30, tzinfo=UTC)
    tools = ConsciousnessTools(
        config=cfg,
        policy_engine=PolicyEngine(_policy(), workspace=tmp_path),
        bus=MessageBus(),
        log=SpeakupLog(tmp_path / "speakups.db"),
        inbound_archive=InboundArchive(tmp_path / "inbound.db"),
        memory=None,
        security=_FakeSecurity(),
        approval_store=None,
        now=lambda: now,
    )
    tools.begin_run(trigger="lull")
    tools.inbound_archive.record_inbound(
        channel="whatsapp",
        chat_id="group@g.us",
        message_id="yesterday-options",
        participant="timo@s.whatsapp.net",
        sender_id="timo@s.whatsapp.net",
        text="31 Win Streak options trader from yesterday",
        timestamp=int((now - timedelta(days=1)).timestamp()),
        sender_name="Timo",
    )

    result = await tools.propose_speakup(
        channel="whatsapp",
        chat_id="group@g.us",
        message="Random callback: streaks like that are statistically suspicious.",
        action_type="surface_memory",
        confidence=0.95,
        reply_to_message_id="yesterday-options",
    )

    assert result == {"status": "rejected", "reason": "stale_reply_to_message"}
