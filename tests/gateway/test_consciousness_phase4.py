"""Tests for Phase 4 burst-triggered consciousness wakeups."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from yeoman_gateway.bus.events import InboundObservedEvent
from yeoman_gateway.bus.queue import MessageBus
from yeoman_gateway.consciousness.agent import ConsciousnessAgent
from yeoman_gateway.consciousness.approval import SpeakupApprovalStore
from yeoman_gateway.consciousness.burst import BurstObserver
from yeoman_gateway.consciousness.log import SpeakupLog
from yeoman_gateway.consciousness.service import ConsciousnessService
from yeoman_gateway.consciousness.tools import ConsciousnessTools
from yeoman_gateway.policy.engine import PolicyEngine
from yeoman_gateway.policy.schema import PolicyConfig
from yeoman_gateway.storage.inbound_archive import InboundArchive
from yeoman_shared.config.schema import Config, ConsciousnessConfig


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
    mentioned_bot: bool = False,
    reply_to_bot: bool = False,
    from_me: bool = False,
) -> InboundObservedEvent:
    return InboundObservedEvent(
        channel="whatsapp",
        chat_id=chat_id,
        sender_id=sender_id,
        content="message",
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
async def test_burst_tick_targets_trigger_chat_and_uses_existing_rails(tmp_path: Path) -> None:
    cfg = _config()
    bus = MessageBus()
    log = SpeakupLog(tmp_path / "speakups.db")
    tools = ConsciousnessTools(
        config=cfg,
        policy_engine=PolicyEngine(_policy(), workspace=tmp_path),
        bus=bus,
        log=log,
        inbound_archive=InboundArchive(tmp_path / "inbound.db"),
        memory=None,
        security=_FakeSecurity(),
        approval_store=None,
        now=lambda: datetime(2026, 4, 26, 12, 0, tzinfo=UTC),
    )
    prompts: list[str] = []

    async def planner(prompt: str) -> dict[str, object]:
        prompts.append(prompt)
        return {
            "chat_id": "group@g.us",
            "message": "burst response",
            "action_type": "observation",
            "confidence": 0.95,
        }

    service = ConsciousnessService(
        config=cfg,
        agent=ConsciousnessAgent(tools=tools, planner=planner),
    )

    result = await service.tick_once(trigger="burst", target_chat_id="other@g.us")

    assert result == {
        "status": "silent_pass",
        "reason": "target_chat_not_eligible",
        "chat_id": "other@g.us",
    }
    assert prompts == []
    assert bus.outbound_size == 0


@pytest.mark.asyncio
async def test_burst_tick_keeps_group_preview_approval_rail(tmp_path: Path) -> None:
    cfg = _config()
    bus = MessageBus()
    log = SpeakupLog(tmp_path / "speakups.db")
    approval_store = SpeakupApprovalStore(tmp_path / "pending_approvals.json")
    tools = ConsciousnessTools(
        config=cfg,
        policy_engine=PolicyEngine(_policy(), workspace=tmp_path),
        bus=bus,
        log=log,
        inbound_archive=InboundArchive(tmp_path / "inbound.db"),
        memory=None,
        security=_FakeSecurity(),
        approval_store=approval_store,
        now=lambda: datetime(2026, 4, 26, 12, 0, tzinfo=UTC),
    )

    async def planner(prompt: str) -> dict[str, object]:
        assert "group@g.us" in prompt
        return {
            "chat_id": "group@g.us",
            "message": "burst response",
            "action_type": "observation",
            "confidence": 0.95,
        }

    service = ConsciousnessService(
        config=cfg,
        agent=ConsciousnessAgent(tools=tools, planner=planner),
    )

    result = await service.tick_once(
        trigger="burst",
        target_channel="whatsapp",
        target_chat_id="group@g.us",
    )
    preview = await bus.consume_outbound()

    assert result["status"] == "queued_for_approval"
    assert preview.chat_id == "owner@s.whatsapp.net"
    assert preview.metadata["preview"] is True
    assert preview.metadata["target_chat_id"] == "group@g.us"
    assert await log.count_sent_today(
        channel="whatsapp",
        chat_id="group@g.us",
        now=datetime(2026, 4, 26, 12, 0, tzinfo=UTC),
    ) == 0


@pytest.mark.asyncio
async def test_burst_tick_keeps_daily_cap_rail(tmp_path: Path) -> None:
    cfg = _config()
    bus = MessageBus()
    log = SpeakupLog(tmp_path / "speakups.db")
    await log.record_sent(
        proposal_id="existing",
        channel="whatsapp",
        chat_id="group@g.us",
        action_type="observation",
        profile="balanced",
        message="already sent",
        trigger="cron",
        context_snapshot={},
        now=datetime(2026, 4, 26, 12, 0, tzinfo=UTC).timestamp(),
    )
    tools = ConsciousnessTools(
        config=cfg,
        policy_engine=PolicyEngine(_policy(daily_cap=1), workspace=tmp_path),
        bus=bus,
        log=log,
        inbound_archive=InboundArchive(tmp_path / "inbound.db"),
        memory=None,
        security=_FakeSecurity(),
        approval_store=SpeakupApprovalStore(tmp_path / "pending_approvals.json"),
        now=lambda: datetime(2026, 4, 26, 12, 5, tzinfo=UTC),
    )

    async def planner(prompt: str) -> dict[str, object]:
        assert '"daily_remaining": 0' in prompt
        return {
            "chat_id": "group@g.us",
            "message": "over cap",
            "action_type": "observation",
            "confidence": 0.95,
        }

    service = ConsciousnessService(
        config=cfg,
        agent=ConsciousnessAgent(tools=tools, planner=planner),
    )

    result = await service.tick_once(
        trigger="burst",
        target_channel="whatsapp",
        target_chat_id="group@g.us",
    )

    assert result == {"status": "rejected", "reason": "daily_cap_reached"}
    assert bus.outbound_size == 0
