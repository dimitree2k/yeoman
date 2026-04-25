"""Tests for Phase 1 owner-DM consciousness behavior."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from yeoman_gateway.bus.events import OutboundMessage
from yeoman_gateway.bus.queue import MessageBus
from yeoman_gateway.consciousness.agent import ConsciousnessAgent
from yeoman_gateway.consciousness.log import SpeakupLog
from yeoman_gateway.consciousness.service import ConsciousnessService
from yeoman_gateway.consciousness.tools import ConsciousnessTools
from yeoman_gateway.core.models import SecurityDecision, SecurityResult
from yeoman_gateway.policy.engine import PolicyEngine
from yeoman_gateway.policy.schema import PolicyConfig
from yeoman_gateway.storage.inbound_archive import InboundArchive
from yeoman_shared.config.schema import Config, ConsciousnessConfig


class _FakeSecurity:
    def __init__(self, action: str = "allow") -> None:
        self.action = action

    def check_output(
        self, text: str, context: dict[str, object] | None = None
    ) -> SecurityResult:
        del text, context
        return SecurityResult(
            stage="output",
            decision=SecurityDecision(action=self.action, reason=f"fake_{self.action}"),
        )


class _FakeMemory:
    def search(self, **kwargs: object) -> list[object]:
        del kwargs
        return []


def _config(**overrides: object) -> Config:
    payload = {
        "enabled": True,
        "ownerDmDefaultEnabled": True,
        "defaultDailyCap": 1,
        "maxSpeakupLengthChars": 200,
    }
    payload.update(overrides)
    return Config(consciousness=ConsciousnessConfig.model_validate(payload))


def _policy(*, quiet: bool = False) -> PolicyConfig:
    spontaneity: dict[str, object] = {"profile": "helpful"}
    if quiet:
        spontaneity["quietHoursStart"] = "00:00"
        spontaneity["quietHoursEnd"] = "23:59"
    return PolicyConfig.model_validate(
        {
            "owners": {"whatsapp": ["owner@s.whatsapp.net"]},
            "channels": {
                "whatsapp": {
                    "chats": {
                        "owner@s.whatsapp.net": {
                            "spontaneity": spontaneity,
                        }
                    }
                }
            },
        }
    )


def _tools(
    tmp_path: Path,
    *,
    config: Config | None = None,
    policy: PolicyConfig | None = None,
    security: object | None = None,
) -> ConsciousnessTools:
    return ConsciousnessTools(
        config=config or _config(),
        policy_engine=PolicyEngine(policy or _policy(), workspace=tmp_path),
        bus=MessageBus(),
        log=SpeakupLog(tmp_path / "speakups.db"),
        inbound_archive=InboundArchive(tmp_path / "inbound.db"),
        memory=_FakeMemory(),
        security=security or _FakeSecurity(),
        now=lambda: datetime(2026, 4, 25, 12, 0, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_global_kill_switch_prevents_service_start_and_commit(tmp_path: Path) -> None:
    cfg = _config(enabled=False)
    tools = _tools(tmp_path, config=cfg)
    agent = ConsciousnessAgent(
        tools=tools,
        planner=lambda prompt: json.dumps(
            {
                "chat_id": "owner@s.whatsapp.net",
                "message": "I noticed a useful thing.",
                "action_type": "observation",
                "confidence": 0.9,
            }
        ),
    )
    service = ConsciousnessService(config=cfg, agent=agent)

    await service.start()
    result = await agent.run_once(trigger="cron")

    assert service.started is False
    assert result["status"] == "silent_pass"
    assert result["reason"] == "no_eligible_chats"
    assert tools.bus.outbound_size == 0


@pytest.mark.asyncio
async def test_daily_cap_cannot_be_exceeded(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    await tools.log.record_sent(
        proposal_id="existing",
        channel="whatsapp",
        chat_id="owner@s.whatsapp.net",
        action_type="observation",
        profile="helpful",
        message="already sent",
        trigger="cron",
        context_snapshot={},
        now=time.time(),
    )

    proposal = await tools.propose_speakup(
        chat_id="owner@s.whatsapp.net",
        message="second message",
        action_type="observation",
        confidence=0.95,
    )
    committed = await tools.commit_speakup(str(proposal["proposal_id"]))

    assert proposal["status"] == "proposed"
    assert committed["status"] == "rejected"
    assert committed["reason"] == "daily_cap_reached"
    assert tools.bus.outbound_size == 0


@pytest.mark.asyncio
async def test_quiet_hours_prevent_eligibility(tmp_path: Path) -> None:
    tools = _tools(tmp_path, policy=_policy(quiet=True))

    eligible = await tools.read_eligible_chats()

    assert eligible == []


@pytest.mark.asyncio
async def test_non_eligible_chat_is_rejected(tmp_path: Path) -> None:
    tools = _tools(tmp_path)

    result = await tools.propose_speakup(
        chat_id="group@g.us",
        message="not allowed",
        action_type="observation",
        confidence=0.95,
    )

    assert result["status"] == "rejected"
    assert result["reason"] == "chat_not_eligible"


@pytest.mark.asyncio
async def test_fake_agent_can_propose_exactly_one_outbound_message(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    archive = tools.inbound_archive
    archive.record_inbound(
        channel="whatsapp",
        chat_id="owner@s.whatsapp.net",
        message_id="m1",
        participant=None,
        sender_id="owner@s.whatsapp.net",
        text="I need to remember the warranty expires tomorrow.",
        timestamp=int((datetime.now(UTC) - timedelta(minutes=5)).timestamp()),
    )
    agent = ConsciousnessAgent(
        tools=tools,
        planner=lambda prompt: json.dumps(
            {
                "chat_id": "owner@s.whatsapp.net",
                "message": "Reminder: check the warranty before it expires tomorrow.",
                "action_type": "surface_memory",
                "confidence": 0.9,
            }
        ),
    )

    result = await agent.run_once(trigger="cron")
    outbound = await tools.bus.consume_outbound()

    assert result["status"] == "sent"
    assert isinstance(outbound, OutboundMessage)
    assert outbound.channel == "whatsapp"
    assert outbound.chat_id == "owner@s.whatsapp.net"
    assert outbound.metadata["spontaneous"] is True
    assert tools.bus.outbound_size == 0


@pytest.mark.asyncio
async def test_fake_agent_can_stay_silent_and_logs_pass(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    agent = ConsciousnessAgent(
        tools=tools,
        planner=lambda prompt: json.dumps({"silence": True, "reason": "nothing useful"}),
    )

    result = await agent.run_once(trigger="cron")
    history = await tools.log.history("whatsapp", "owner@s.whatsapp.net", limit=5)

    assert result["status"] == "silent_pass"
    assert history[0]["status"] == "silent_pass"
    assert history[0]["message"] == ""


@pytest.mark.asyncio
async def test_agent_prompt_includes_daily_cap_state(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    await tools.log.record_sent(
        proposal_id="existing",
        channel="whatsapp",
        chat_id="owner@s.whatsapp.net",
        action_type="observation",
        profile="helpful",
        message="already sent",
        trigger="cron",
        context_snapshot={},
        now=datetime(2026, 4, 25, 9, 0, tzinfo=UTC).timestamp(),
    )
    captured: dict[str, object] = {}

    def planner(prompt: str) -> str:
        captured.update(json.loads(prompt))
        return json.dumps({"silence": True, "reason": "test"})

    agent = ConsciousnessAgent(tools=tools, planner=planner)

    await agent.run_once(trigger="cron")

    eligible = captured["eligible_chats"]
    assert isinstance(eligible, list)
    assert eligible[0]["daily_cap"] == 1
    assert eligible[0]["sent_today"] == 1
    assert eligible[0]["daily_remaining"] == 0
    assert "status 'denied' as owner feedback" in str(captured["instruction"])


@pytest.mark.asyncio
async def test_security_output_block_prevents_commit(tmp_path: Path) -> None:
    tools = _tools(tmp_path, security=_FakeSecurity(action="block"))
    proposal = await tools.propose_speakup(
        chat_id="owner@s.whatsapp.net",
        message="blocked by security",
        action_type="observation",
        confidence=0.95,
    )

    result = await tools.commit_speakup(str(proposal["proposal_id"]))

    assert result["status"] == "rejected"
    assert result["reason"] == "security_output_blocked"
    assert tools.bus.outbound_size == 0
