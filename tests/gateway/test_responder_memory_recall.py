from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from yeoman_gateway.adapters.responder_llm import LLMResponder
from yeoman_gateway.bus.queue import MessageBus
from yeoman_gateway.core.models import InboundEvent, PolicyDecision
from yeoman_gateway.memory.service import MemoryService
from yeoman_gateway.providers.base import LLMProvider, LLMResponse
from yeoman_shared.config.schema import Config
from yeoman_shared.telemetry import InMemoryTelemetry


class CaptureMemoryProvider(LLMProvider):
    def __init__(self, wal_file: Path) -> None:
        super().__init__()
        self.wal_file = wal_file
        self.messages_seen: list[list[dict[str, Any]]] = []
        self.pre_write_seen = False

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning: dict[str, Any] | None = None,
    ) -> LLMResponse:
        del tools, model, max_tokens, temperature, reasoning
        self.messages_seen.append(messages)
        if self.wal_file.exists() and " PRE" in self.wal_file.read_text(encoding="utf-8"):
            self.pre_write_seen = True
        return LLMResponse(content="ok")

    def get_default_model(self) -> str:
        return "dummy/model"


class CountProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning: dict[str, Any] | None = None,
    ) -> LLMResponse:
        del messages, tools, model, max_tokens, temperature, reasoning
        self.calls += 1
        return LLMResponse(content=f"ok-{self.calls}")

    def get_default_model(self) -> str:
        return "dummy/model"


class TalkativeLlmProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning: dict[str, Any] | None = None,
    ) -> LLMResponse:
        del tools, model, max_tokens, temperature, reasoning
        self.calls += 1
        first = str(messages[0].get("content", "")) if messages else ""
        if "You write one short playful cooldown message" in first:
            return LLMResponse(content="Kurz Pause, Bro. Fuer 24/7 goenn dir ein OpenAI/Kimi/Anthropic-Abo.")
        return LLMResponse(content=f"ok-{self.calls}")

    def get_default_model(self) -> str:
        return "dummy/model"


class SequenceProvider(LLMProvider):
    def __init__(self, responses: list[str]) -> None:
        super().__init__()
        self.responses = responses
        self.calls = 0

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning: dict[str, Any] | None = None,
    ) -> LLMResponse:
        del messages, tools, model, max_tokens, temperature, reasoning
        response = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        return LLMResponse(content=response)

    def get_default_model(self) -> str:
        return "dummy/model"


@pytest.mark.asyncio
async def test_responder_injects_retrieved_memory_and_wal(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()

    cfg = Config()
    cfg.memory.db_path = str(tmp_path / "longterm.db")
    memory_service = MemoryService(workspace=workspace, config=cfg.memory)

    # Seed one manual memory entry.
    memory_service.record_manual(
        channel="cli",
        chat_id="direct",
        sender_id="direct",
        scope_type="user",
        kind="preference",
        text="I prefer concise responses",
        importance=0.9,
    )

    wal_file = memory_service.state_store.state_dir / "cli_test.md"
    provider = CaptureMemoryProvider(wal_file=wal_file)
    telemetry = InMemoryTelemetry()
    responder = LLMResponder(
        bus=MessageBus(),
        provider=provider,
        workspace=workspace,
        memory_service=memory_service,
        telemetry=telemetry,
    )

    out = await responder.process_direct(
        "Please keep concise responses.",
        session_key="cli:test",
        channel="cli",
        chat_id="direct",
    )

    await responder.aclose()
    memory_service.close()

    assert out == "ok"
    assert provider.pre_write_seen is True

    sent = provider.messages_seen[-1]
    memory_system_msgs = [
        msg["content"]
        for msg in sent
        if msg.get("role") == "system"
        and isinstance(msg.get("content"), str)
        and "[Retrieved Memory]" in msg["content"]
    ]
    assert memory_system_msgs

    assert wal_file.exists()
    wal_text = wal_file.read_text(encoding="utf-8")
    assert "PRE" in wal_text
    assert "POST" in wal_text
    assert telemetry.get_counter("memory_recall_hit") == 1
    assert telemetry.get_counter("memory_prompt_chars") > 0
    assert telemetry.get_counter("memory_capture_dropped_low_conf") >= 1


@pytest.mark.asyncio
async def test_group_talkative_same_topic_triggers_cooldown_reply(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()

    provider = CountProvider()
    responder = LLMResponder(
        bus=MessageBus(),
        provider=provider,
        workspace=workspace,
    )

    decision = PolicyDecision(
        accept_message=True,
        should_respond=True,
        allowed_tools=frozenset(),
        reason="test",
        talkative_cooldown_enabled=True,
        talkative_cooldown_streak_threshold=7,
        talkative_cooldown_topic_overlap_threshold=0.34,
        talkative_cooldown_cooldown_seconds=60,
        talkative_cooldown_delay_seconds=0.0,
    )

    out = ""
    for i in range(7):
        event = InboundEvent(
            channel="whatsapp",
            chat_id="group@g.us",
            sender_id="u1",
            content=f"nano erkläre christian wolf investment These detail {i}",
            is_group=True,
            mentioned_bot=True,
        )
        out = await responder.generate_reply(event, decision) or ""

    await responder.aclose()

    assert provider.calls == 6
    assert out == "Bro, du nervst gerade mit dem gleichen Thema. Kurz Pause."


@pytest.mark.asyncio
async def test_group_talkative_same_topic_uses_llm_message_when_enabled(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()

    provider = TalkativeLlmProvider()
    responder = LLMResponder(
        bus=MessageBus(),
        provider=provider,
        workspace=workspace,
    )

    decision = PolicyDecision(
        accept_message=True,
        should_respond=True,
        allowed_tools=frozenset(),
        reason="test",
        talkative_cooldown_enabled=True,
        talkative_cooldown_streak_threshold=7,
        talkative_cooldown_topic_overlap_threshold=0.34,
        talkative_cooldown_cooldown_seconds=60,
        talkative_cooldown_delay_seconds=0.0,
        talkative_cooldown_use_llm_message=True,
    )

    out = ""
    for i in range(7):
        event = InboundEvent(
            channel="whatsapp",
            chat_id="group@g.us",
            sender_id="u1",
            content=f"nano erklaere christian wolf investment these detail {i}",
            is_group=True,
            mentioned_bot=True,
        )
        out = await responder.generate_reply(event, decision) or ""

    await responder.aclose()

    assert provider.calls == 7
    assert out == "Kurz Pause, Bro. Fuer 24/7 goenn dir ein OpenAI/Kimi/Anthropic-Abo."


@pytest.mark.asyncio
async def test_group_reply_to_landed_social_line_holds_back(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()

    provider = SequenceProvider(
        ["Fakten sind der ultimative Cringe-Killer. Naechstes Mal mit Glitzer."]
    )
    responder = LLMResponder(
        bus=MessageBus(),
        provider=provider,
        workspace=workspace,
    )
    decision = PolicyDecision(
        accept_message=True,
        should_respond=True,
        allowed_tools=frozenset(),
        reason="test",
    )

    first = await responder.generate_reply(
        InboundEvent(
            channel="whatsapp",
            chat_id="group@g.us",
            sender_id="u1",
            content="Cringe direkt auf Mutter zu gehen",
            is_group=True,
            reply_to_bot=True,
        ),
        decision,
    )
    second = await responder.generate_reply(
        InboundEvent(
            channel="whatsapp",
            chat_id="group@g.us",
            sender_id="u1",
            content="haha safe",
            is_group=True,
            reply_to_bot=True,
        ),
        decision,
    )

    await responder.aclose()

    assert first == "Fakten sind der ultimative Cringe-Killer. Naechstes Mal mit Glitzer."
    assert second is None
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_group_social_reply_ends_rhetorical_question_with_full_stop(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()

    provider = SequenceProvider(["Fakten sind der ultimative Cringe-Killer. Naechstes Mal mit Glitzer?"])
    responder = LLMResponder(
        bus=MessageBus(),
        provider=provider,
        workspace=workspace,
    )
    decision = PolicyDecision(
        accept_message=True,
        should_respond=True,
        allowed_tools=frozenset(),
        reason="test",
    )

    out = await responder.generate_reply(
        InboundEvent(
            channel="whatsapp",
            chat_id="group@g.us",
            sender_id="u1",
            content="Cringe direkt auf Mutter zu gehen",
            is_group=True,
            reply_to_bot=True,
        ),
        decision,
    )

    await responder.aclose()

    assert out == "Fakten sind der ultimative Cringe-Killer. Naechstes Mal mit Glitzer."


@pytest.mark.asyncio
async def test_group_genuine_clarifying_question_is_not_rewritten(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()

    provider = SequenceProvider(["Was genau meinst du?"])
    responder = LLMResponder(
        bus=MessageBus(),
        provider=provider,
        workspace=workspace,
    )
    decision = PolicyDecision(
        accept_message=True,
        should_respond=True,
        allowed_tools=frozenset(),
        reason="test",
    )

    out = await responder.generate_reply(
        InboundEvent(
            channel="whatsapp",
            chat_id="group@g.us",
            sender_id="u1",
            content="Das da",
            is_group=True,
            reply_to_bot=True,
        ),
        decision,
    )

    await responder.aclose()

    assert out == "Was genau meinst du?"
