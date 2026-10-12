from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from yeoman_gateway.adapters.responder_llm import LLMResponder
from yeoman_gateway.bus.queue import MessageBus
from yeoman_gateway.core.models import InboundEvent, PolicyDecision
from yeoman_gateway.knowledge._memory.service import MemoryService
from yeoman_gateway.providers.base import LLMProvider, LLMResponse
from yeoman_shared.config.schema import Config


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



@pytest.fixture(autouse=True)
def _project_whatsapp_responder_history(monkeypatch, request, tmp_path):
    from tests.gateway.history_test_helpers import install_projected_responder_history

    install_projected_responder_history(monkeypatch, request, tmp_path)



def test_remaining_copies_reports_frozen_session_state_files(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    state_dir = workspace / "data/memory/session-state"
    state_dir.mkdir(parents=True)
    cfg = Config()
    cfg.memory.db_path = str(tmp_path / "longterm.db")
    cfg.memory.wal.state_dir = str(state_dir)
    memory_service = MemoryService(workspace=workspace, config=cfg.memory)

    try:
        assert "session_state_markdown" not in memory_service.remaining_copies()
        (state_dir / "frozen.md").write_text("legacy copy", encoding="utf-8")
        assert "session_state_markdown" in memory_service.remaining_copies()
    finally:
        memory_service.close()


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
