"""Tests for Phase 1 owner-DM consciousness behavior."""

from __future__ import annotations

import asyncio
import json
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


class _FakeEntry:
    def __init__(
        self,
        content: str,
        *,
        confidence: float = 0.8,
        updated_at: str = "2026-04-25T12:00:00+00:00",
    ) -> None:
        self.content = content
        self.confidence = confidence
        self.updated_at = updated_at


class _FakeHit:
    def __init__(self, content: str) -> None:
        self.entry = _FakeEntry(content)


class _FakeMemory:
    def __init__(self, learned_taste: list[str] | None = None) -> None:
        self.learned_taste = learned_taste or []
        self.search_calls: list[dict[str, object]] = []
        self.learned_taste_calls: list[dict[str, object]] = []

    def search(self, **kwargs: object) -> list[object]:
        self.search_calls.append(dict(kwargs))
        return []

    def learned_chat_taste(self, **kwargs: object) -> list[object]:
        self.learned_taste_calls.append(dict(kwargs))
        return [_FakeHit(content) for content in self.learned_taste]


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
    memory: object | None = None,
) -> ConsciousnessTools:
    return ConsciousnessTools(
        config=config or _config(),
        policy_engine=PolicyEngine(policy or _policy(), workspace=tmp_path),
        bus=MessageBus(),
        log=SpeakupLog(tmp_path / "speakups.db"),
        inbound_archive=InboundArchive(tmp_path / "inbound.db"),
        memory=memory if memory is not None else _FakeMemory(),
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
        now=datetime(2026, 4, 25, 12, 0, tzinfo=UTC).timestamp(),
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
async def test_agent_prompt_includes_learned_chat_taste(tmp_path: Path) -> None:
    memory = _FakeMemory(
        learned_taste=[
            "Proactive speakup taste pattern: Short market comments work better than broad jokes."
        ]
    )
    tools = _tools(tmp_path, memory=memory)
    captured: dict[str, object] = {}

    def planner(prompt: str) -> str:
        captured.update(json.loads(prompt))
        return json.dumps({"silence": True, "reason": "test"})

    agent = ConsciousnessAgent(tools=tools, planner=planner)

    await agent.run_once(trigger="cron")

    learned_taste = captured["learned_taste"]
    assert isinstance(learned_taste, dict)
    assert learned_taste["status"] == "ok"
    assert learned_taste["patterns"] == [
        {
            "content": (
                "Proactive speakup taste pattern: Short market comments work better "
                "than broad jokes."
            ),
            "confidence": 0.8,
            "updated_at": "2026-04-25T12:00:00+00:00",
        }
    ]
    assert memory.learned_taste_calls == [
        {"channel": "whatsapp", "chat_id": "owner@s.whatsapp.net", "limit": 5}
    ]


@pytest.mark.asyncio
async def test_agent_does_not_search_memory_with_empty_query(tmp_path: Path) -> None:
    memory = _FakeMemory()
    tools = _tools(tmp_path, memory=memory)
    captured: dict[str, object] = {}

    def planner(prompt: str) -> str:
        captured.update(json.loads(prompt))
        return json.dumps({"silence": True, "reason": "test"})

    agent = ConsciousnessAgent(tools=tools, planner=planner)

    await agent.run_once(trigger="cron")

    assert captured["memory"] == {"status": "ok", "hits": []}
    assert memory.search_calls == []
    assert memory.learned_taste_calls == [
        {"channel": "whatsapp", "chat_id": "owner@s.whatsapp.net", "limit": 5}
    ]


@pytest.mark.asyncio
async def test_agent_searches_memory_with_chat_window_text(tmp_path: Path) -> None:
    memory = _FakeMemory()
    tools = _tools(tmp_path, memory=memory)
    tools.inbound_archive.record_inbound(
        channel="whatsapp",
        chat_id="owner@s.whatsapp.net",
        message_id="m1",
        participant=None,
        sender_id="owner@s.whatsapp.net",
        text="NVDA earnings reaction looks overextended.",
        timestamp=int(datetime(2026, 4, 25, 11, 59, tzinfo=UTC).timestamp()),
    )

    agent = ConsciousnessAgent(
        tools=tools,
        planner=lambda prompt: json.dumps({"silence": True, "reason": "test"}),
    )

    await agent.run_once(trigger="cron")

    assert memory.search_calls == [
        {
            "query": "NVDA earnings reaction looks overextended.",
            "channel": "whatsapp",
            "chat_id": "owner@s.whatsapp.net",
            "scope": "chat",
            "limit": 5,
        }
    ]


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


class TestParseDecision:
    def test_plain_json(self) -> None:
        result = ConsciousnessAgent._parse_decision('{"silence": true, "reason": "x"}')
        assert result == {"silence": True, "reason": "x"}

    def test_dict_passthrough(self) -> None:
        payload = {"silence": True}
        assert ConsciousnessAgent._parse_decision(payload) is payload

    def test_empty_returns_silence(self) -> None:
        result = ConsciousnessAgent._parse_decision("")
        assert result == {"silence": True, "reason": "empty_planner_response"}

    def test_markdown_code_fence(self) -> None:
        raw = '```json\n{"silence": true, "reason": "fenced"}\n```'
        assert ConsciousnessAgent._parse_decision(raw) == {
            "silence": True,
            "reason": "fenced",
        }

    def test_code_fence_without_language(self) -> None:
        raw = '```\n{"chat_id": "x", "message": "hi", "confidence": 0.5}\n```'
        result = ConsciousnessAgent._parse_decision(raw)
        assert result["message"] == "hi"

    def test_prose_around_json(self) -> None:
        raw = 'Sure, here is my decision:\n{"silence": true, "reason": "trivial"}\nLet me know.'
        assert ConsciousnessAgent._parse_decision(raw) == {
            "silence": True,
            "reason": "trivial",
        }

    def test_truly_invalid_falls_back(self) -> None:
        result = ConsciousnessAgent._parse_decision("not json at all, no braces here")
        assert result == {"silence": True, "reason": "invalid_planner_json"}

    def test_non_dict_json_falls_back(self) -> None:
        result = ConsciousnessAgent._parse_decision('["a", "b"]')
        assert result == {"silence": True, "reason": "invalid_planner_json"}


@pytest.mark.asyncio
async def test_prompt_includes_golden_rules_and_anti_patterns(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    captured: dict[str, object] = {}

    def planner(prompt: str) -> str:
        captured.update(json.loads(prompt))
        return json.dumps({"silence": True, "reason": "test"})

    agent = ConsciousnessAgent(tools=tools, planner=planner)
    await agent.run_once(trigger="cron")

    rules = captured.get("golden_rules")
    assert isinstance(rules, list) and len(rules) >= 4
    rules_text = " ".join(str(r) for r in rules)
    assert "echo" in rules_text.lower() or "paraphrase" in rules_text.lower()
    assert "silence" in rules_text.lower()

    anti = captured.get("anti_patterns")
    assert isinstance(anti, list) and len(anti) >= 3

    assert "persona" in captured


@pytest.mark.asyncio
async def test_persona_loaded_when_persona_file_set(tmp_path: Path) -> None:
    persona_dir = tmp_path / "personas"
    persona_dir.mkdir()
    (persona_dir / "alpha.md").write_text("You are Alpha. Be terse.", encoding="utf-8")

    policy = PolicyConfig.model_validate(
        {
            "owners": {"whatsapp": ["owner@s.whatsapp.net"]},
            "channels": {
                "whatsapp": {
                    "chats": {
                        "owner@s.whatsapp.net": {
                            "personaFile": "personas/alpha.md",
                            "spontaneity": {"profile": "helpful"},
                        }
                    }
                }
            },
        }
    )
    tools = _tools(tmp_path, policy=policy)
    captured: dict[str, object] = {}

    def planner(prompt: str) -> str:
        captured.update(json.loads(prompt))
        return json.dumps({"silence": True, "reason": "test"})

    agent = ConsciousnessAgent(tools=tools, planner=planner)
    await agent.run_once(trigger="cron")

    assert captured.get("persona") == "You are Alpha. Be terse."


@pytest.mark.asyncio
async def test_cron_prompt_window_uses_recent_context_not_seven_day_backfill(
    tmp_path: Path,
) -> None:
    cfg = _config(lullActivityWindowMinutes=120)
    now = datetime(2026, 5, 2, 12, 0, tzinfo=UTC)
    tools = _tools(tmp_path, config=cfg)
    tools._now = lambda: now
    tools.inbound_archive.record_inbound(
        channel="whatsapp",
        chat_id="owner@s.whatsapp.net",
        message_id="old-market-thread",
        participant="timo@s.whatsapp.net",
        sender_id="timo@s.whatsapp.net",
        text="Old market thread from days ago",
        timestamp=int((now - timedelta(days=3)).timestamp()),
        sender_name="Timo",
    )
    tools.inbound_archive.record_inbound(
        channel="whatsapp",
        chat_id="owner@s.whatsapp.net",
        message_id="fresh-thread",
        participant="robin@s.whatsapp.net",
        sender_id="robin@s.whatsapp.net",
        text="Fresh topic from this hour",
        timestamp=int((now - timedelta(minutes=30)).timestamp()),
        sender_name="Robin",
    )
    captured: dict[str, object] = {}

    def planner(prompt: str) -> str:
        captured.update(json.loads(prompt))
        return json.dumps({"silence": True, "reason": "test"})

    agent = ConsciousnessAgent(tools=tools, planner=planner)

    result = await agent.run_once(trigger="cron")

    assert result["status"] == "silent_pass"
    messages = captured["chat_window"]["messages"]
    assert [message["message_id"] for message in messages] == ["fresh-thread"]
    assert "old-market-thread" not in json.dumps(captured)


@pytest.mark.asyncio
async def test_reply_to_message_id_validates_against_archive(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    tools.inbound_archive.record_inbound(
        channel="whatsapp",
        chat_id="owner@s.whatsapp.net",
        message_id="msg-real",
        participant=None,
        sender_id="sender-1",
        text="Original message text",
        timestamp=int(datetime(2026, 4, 25, 11, 50, tzinfo=UTC).timestamp()),
        sender_name="Robin",
    )

    valid = await tools.propose_speakup(
        chat_id="owner@s.whatsapp.net",
        message="anchored reply",
        action_type="observation",
        confidence=0.9,
        reply_to_message_id="msg-real",
    )
    assert valid["status"] == "proposed"
    proposal = tools._proposals[str(valid["proposal_id"])]
    assert proposal.reply_to_message_id == "msg-real"

    invalid = await tools.propose_speakup(
        chat_id="owner@s.whatsapp.net",
        message="hallucinated reply",
        action_type="observation",
        confidence=0.9,
        reply_to_message_id="msg-fake",
    )
    assert invalid["status"] == "proposed"
    proposal2 = tools._proposals[str(invalid["proposal_id"])]
    assert proposal2.reply_to_message_id is None


@pytest.mark.asyncio
async def test_commit_speakup_propagates_reply_to_in_outbound(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    tools.inbound_archive.record_inbound(
        channel="whatsapp",
        chat_id="owner@s.whatsapp.net",
        message_id="msg-anchor",
        participant=None,
        sender_id="sender-1",
        text="anchor text",
        timestamp=int(datetime(2026, 4, 25, 11, 50, tzinfo=UTC).timestamp()),
        sender_name="Robin",
    )
    proposal = await tools.propose_speakup(
        chat_id="owner@s.whatsapp.net",
        message="grounded answer",
        action_type="observation",
        confidence=0.9,
        reply_to_message_id="msg-anchor",
    )

    committed = await tools.commit_speakup(str(proposal["proposal_id"]))

    assert committed["status"] == "sent"
    sent = await tools.bus.consume_outbound()
    assert sent.reply_to == "msg-anchor"


@pytest.mark.asyncio
async def test_owner_dm_preview_includes_quoted_context(tmp_path: Path) -> None:
    from yeoman_gateway.consciousness.approval import SpeakupApprovalStore

    policy = PolicyConfig.model_validate(
        {
            "owners": {"whatsapp": ["owner@s.whatsapp.net"]},
            "channels": {
                "whatsapp": {
                    "chats": {
                        "group@g.us": {
                            "spontaneity": {
                                "enabled": True,
                                "profile": "balanced",
                                "preview": "owner_dm",
                            }
                        }
                    }
                }
            },
        }
    )
    approval_store = SpeakupApprovalStore(tmp_path / "approvals.json")
    tools = ConsciousnessTools(
        config=_config(),
        policy_engine=PolicyEngine(policy, workspace=tmp_path),
        bus=MessageBus(),
        log=SpeakupLog(tmp_path / "speakups.db"),
        inbound_archive=InboundArchive(tmp_path / "inbound.db"),
        memory=_FakeMemory(),
        security=_FakeSecurity(),
        approval_store=approval_store,
        now=lambda: datetime(2026, 4, 25, 12, 0, tzinfo=UTC),
    )
    tools.inbound_archive.record_inbound(
        channel="whatsapp",
        chat_id="group@g.us",
        message_id="msg-original",
        participant="part-1",
        sender_id="sender-1",
        text="prinzipiell ist das ne super geile spannde frage",
        timestamp=int(datetime(2026, 4, 25, 11, 50, tzinfo=UTC).timestamp()),
        sender_name="Robin",
    )

    proposal = await tools.propose_speakup(
        chat_id="group@g.us",
        message="Konkrete Gegenposition mit Zahl.",
        action_type="share_opinion",
        confidence=0.9,
        reply_to_message_id="msg-original",
    )
    committed = await tools.commit_speakup(str(proposal["proposal_id"]))

    assert committed["status"] == "queued_for_approval"
    dm = await tools.bus.consume_outbound()
    assert "In reply to Robin" in dm.content
    assert "spannde frage" in dm.content
    assert dm.metadata.get("reply_to_message_id") == "msg-original"

    pending = await approval_store.list_pending()
    assert len(pending) == 1
    assert pending[0].reply_to_message_id == "msg-original"


@pytest.mark.asyncio
async def test_persona_is_null_when_no_persona_file(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    captured: dict[str, object] = {}

    def planner(prompt: str) -> str:
        captured.update(json.loads(prompt))
        return json.dumps({"silence": True, "reason": "test"})

    agent = ConsciousnessAgent(tools=tools, planner=planner)
    await agent.run_once(trigger="cron")

    assert "persona" in captured
    assert captured["persona"] is None


@pytest.mark.asyncio
async def test_service_tick_once_serializes_overlapping_runs(tmp_path: Path) -> None:
    cfg = _config()

    class _BlockingAgent:
        def __init__(self) -> None:
            self.started = 0
            self.finished = 0
            self.release = asyncio.Event()

        async def run_once(
            self,
            *,
            trigger: str,
            target_channel: str | None = None,
            target_chat_id: str | None = None,
        ) -> dict[str, object]:
            del target_channel, target_chat_id
            self.started += 1
            await self.release.wait()
            self.finished += 1
            return {"status": "silent_pass", "trigger": trigger}

    agent = _BlockingAgent()
    service = ConsciousnessService(config=cfg, agent=agent)

    first = asyncio.create_task(service.tick_once(trigger="cron"))
    await asyncio.sleep(0)
    second = asyncio.create_task(service.tick_once(trigger="burst"))
    await asyncio.sleep(0)

    assert agent.started == 1
    assert agent.finished == 0

    agent.release.set()
    assert await first == {"status": "silent_pass", "trigger": "cron"}
    assert await second == {"status": "silent_pass", "trigger": "burst"}
    assert agent.started == 2
    assert agent.finished == 2


@pytest.mark.asyncio
async def test_tools_route_duplicate_chat_ids_by_channel(tmp_path: Path) -> None:
    policy = PolicyConfig.model_validate(
        {
            "owners": {
                "whatsapp": ["same-id"],
                "telegram": ["same-id"],
            },
            "channels": {
                "whatsapp": {
                    "chats": {
                        "same-id": {
                            "spontaneity": {
                                "enabled": True,
                                "profile": "helpful",
                                "dailyCap": 1,
                            }
                        }
                    }
                },
                "telegram": {
                    "chats": {
                        "same-id": {
                            "spontaneity": {
                                "enabled": True,
                                "profile": "helpful",
                                "dailyCap": 2,
                            }
                        }
                    }
                },
            },
        }
    )
    tools = _tools(tmp_path, policy=policy)
    await tools.log.record_sent(
        proposal_id="telegram-sent",
        channel="telegram",
        chat_id="same-id",
        action_type="observation",
        profile="helpful",
        message="already sent on telegram",
        trigger="manual",
        context_snapshot={},
        now=datetime(2026, 4, 25, 12, 0, tzinfo=UTC).timestamp(),
    )

    whatsapp_usage = await tools.read_daily_usage("same-id", channel="whatsapp")
    telegram_usage = await tools.read_daily_usage("same-id", channel="telegram")
    ambiguous = await tools.read_daily_usage("same-id")

    assert whatsapp_usage["status"] == "ok"
    assert whatsapp_usage["daily_cap"] == 1
    assert whatsapp_usage["sent_today"] == 0
    assert telegram_usage["status"] == "ok"
    assert telegram_usage["daily_cap"] == 2
    assert telegram_usage["sent_today"] == 1
    assert ambiguous == {"status": "rejected", "reason": "ambiguous_chat_id"}


@pytest.mark.asyncio
async def test_agent_target_channel_selects_channel_before_prompt(tmp_path: Path) -> None:
    policy = PolicyConfig.model_validate(
        {
            "owners": {
                "whatsapp": ["same-id"],
                "telegram": ["same-id"],
            },
            "channels": {
                "whatsapp": {
                    "chats": {
                        "same-id": {
                            "spontaneity": {
                                "enabled": True,
                                "profile": "helpful",
                            }
                        }
                    }
                },
                "telegram": {
                    "chats": {
                        "same-id": {
                            "spontaneity": {
                                "enabled": True,
                                "profile": "helpful",
                            }
                        }
                    }
                },
            },
        }
    )
    tools = _tools(tmp_path, policy=policy)
    tools.inbound_archive.record_inbound(
        channel="whatsapp",
        chat_id="same-id",
        message_id="wa-1",
        participant=None,
        sender_id="wa-user",
        text="whatsapp message",
        timestamp=int(datetime(2026, 4, 25, 11, 50, tzinfo=UTC).timestamp()),
    )
    tools.inbound_archive.record_inbound(
        channel="telegram",
        chat_id="same-id",
        message_id="tg-1",
        participant=None,
        sender_id="tg-user",
        text="telegram message",
        timestamp=int(datetime(2026, 4, 25, 11, 55, tzinfo=UTC).timestamp()),
    )
    captured: dict[str, object] = {}

    def planner(prompt: str) -> str:
        captured.update(json.loads(prompt))
        return json.dumps({"silence": True, "reason": "test"})

    agent = ConsciousnessAgent(tools=tools, planner=planner)

    await agent.run_once(trigger="manual", target_channel="telegram")

    eligible = captured["eligible_chats"]
    assert isinstance(eligible, list)
    assert [chat["channel"] for chat in eligible] == ["telegram"]
    window = captured["chat_window"]
    assert isinstance(window, dict)
    assert window["status"] == "ok"
    assert [message["text"] for message in window["messages"]] == ["telegram message"]


@pytest.mark.asyncio
async def test_agent_planner_selected_chat_id_resolves_channel_from_eligible_list(
    tmp_path: Path,
) -> None:
    policy = PolicyConfig.model_validate(
        {
            "owners": {
                "whatsapp": ["wa-owner"],
                "telegram": ["tg-owner"],
            },
            "channels": {
                "whatsapp": {
                    "chats": {
                        "wa-owner": {
                            "spontaneity": {
                                "enabled": True,
                                "profile": "helpful",
                            }
                        }
                    }
                },
                "telegram": {
                    "chats": {
                        "tg-owner": {
                            "spontaneity": {
                                "enabled": True,
                                "profile": "helpful",
                            }
                        }
                    }
                },
            },
        }
    )
    tools = _tools(tmp_path, policy=policy)

    def planner(prompt: str) -> str:
        del prompt
        return json.dumps(
            {
                "chat_id": "tg-owner",
                "message": "telegram observation",
                "action_type": "observation",
                "confidence": 0.9,
            }
        )

    agent = ConsciousnessAgent(tools=tools, planner=planner)

    result = await agent.run_once(trigger="manual")
    outbound = await tools.bus.consume_outbound()

    assert result["status"] == "sent"
    assert outbound.channel == "telegram"
    assert outbound.chat_id == "tg-owner"


@pytest.mark.asyncio
async def test_agent_missing_target_channel_skips_planner(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    planner_calls = 0

    def planner(prompt: str) -> str:
        nonlocal planner_calls
        planner_calls += 1
        del prompt
        return json.dumps({"silence": True, "reason": "planner_called"})

    agent = ConsciousnessAgent(tools=tools, planner=planner)

    result = await agent.run_once(trigger="manual", target_channel="missing")

    assert result == {
        "status": "silent_pass",
        "reason": "target_channel_not_eligible",
        "channel": "missing",
    }
    assert planner_calls == 0


@pytest.mark.asyncio
async def test_agent_missing_target_channel_with_chat_skips_planner(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    planner_calls = 0

    def planner(prompt: str) -> str:
        nonlocal planner_calls
        planner_calls += 1
        del prompt
        return json.dumps({"silence": True, "reason": "planner_called"})

    agent = ConsciousnessAgent(tools=tools, planner=planner)

    result = await agent.run_once(
        trigger="manual",
        target_channel="missing",
        target_chat_id="owner@s.whatsapp.net",
    )

    assert result == {
        "status": "silent_pass",
        "reason": "target_channel_not_eligible",
        "channel": "missing",
    }
    assert planner_calls == 0
