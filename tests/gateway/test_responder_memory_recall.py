from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from yeoman_gateway.adapters.responder_llm import LLMResponder
from yeoman_gateway.bus.queue import MessageBus
from yeoman_gateway.core.models import InboundEvent, PolicyDecision
from yeoman_gateway.providers.base import LLMProvider, LLMResponse



@pytest.fixture(autouse=True)
def _history_scope_for_whatsapp_responder(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Give responder unit tests a real, isolated SQLite History snapshot per turn."""
    import sqlite3
    from types import SimpleNamespace

    from yeoman_gateway.history.context import history_knowledge_scope, history_turn
    from yeoman_gateway.history.reader import HistorySnapshot
    from yeoman_gateway.history.schema import create as create_history

    history_path = tmp_path / "synthetic-history.db"
    with sqlite3.connect(history_path) as connection:
        create_history(connection)

    async def read_turn():
        connection = sqlite3.connect(f"file:{history_path}?mode=ro", uri=True)
        return HistorySnapshot(1, (), connection)

    projector = SimpleNamespace(
        read_turn=read_turn,
        health=lambda: {"status": "ready", "generation": 1},
    )
    original_init = LLMResponder.__init__
    original_generate = LLMResponder.generate_reply

    def init_with_history(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self._history_projector = projector
        self._history_knowledge = None

    async def generate_in_history(self, event, decision, *args, **kwargs):
        if getattr(event, "channel", "") != "whatsapp":
            return await original_generate(self, event, decision, *args, **kwargs)
        timestamp = getattr(event, "timestamp", None)
        occurred_ms = int(timestamp.timestamp() * 1000) if timestamp is not None else 0
        event_id = str(getattr(event, "message_id", "") or f"unit-{occurred_ms}")
        with sqlite3.connect(history_path) as connection:
            connection.execute(
                "INSERT OR REPLACE INTO messages (message_id,channel,chat_id,native_message_id,"
                "sender_contact_id,sender_identifier,sender_basis,direction,sent_ms,"
                "time_certainty,text,provenance,source_refs) "
                "VALUES (?, 'whatsapp', ?, ?, NULL, ?, 'unknown', 'in', ?, 'unknown', ?, 'native', '[]')",
                (
                    f"unit:{event_id}", str(getattr(event, "chat_id", "")), event_id,
                    str(getattr(event, "sender_id", "")), occurred_ms,
                    str(getattr(event, "content", "") or ""),
                ),
            )
        async with history_turn(projector) as snapshot:
            with history_knowledge_scope(snapshot, self._history_knowledge):
                kwargs.setdefault("history_snapshot", snapshot)
                return await original_generate(self, event, decision, *args, **kwargs)

    monkeypatch.setattr(LLMResponder, "__init__", init_with_history)
    monkeypatch.setattr(LLMResponder, "generate_reply", generate_in_history)

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
