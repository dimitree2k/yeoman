"""Focused tests for Phase 0 learned taste retrieval."""

from __future__ import annotations

import json
from typing import Any

import pytest
from yeoman_gateway.consciousness.agent import ConsciousnessAgent


class _LearnedTasteTools:
    def __init__(
        self,
        *,
        learned_taste: dict[str, object] | None = None,
        fail_on_empty_memory_query: bool = False,
    ) -> None:
        self.learned_taste = learned_taste or {
            "status": "ok",
            "patterns": [
                {
                    "content": (
                        "Proactive speakup taste pattern: bring compact numbers "
                        "and avoid generic encouragement."
                    )
                }
            ],
        }
        self.fail_on_empty_memory_query = fail_on_empty_memory_query
        self.learned_taste_calls: list[dict[str, object]] = []
        self.memory_queries: list[dict[str, object]] = []

    def begin_run(self, *, trigger: str) -> None:
        self.trigger = trigger

    def current_trigger(self) -> str:
        return getattr(self, "trigger", "cron")

    async def read_eligible_chats(self) -> list[dict[str, object]]:
        return [
            {
                "channel": "whatsapp",
                "chat_id": "group@g.us",
                "profile": "balanced",
                "daily_cap": 1,
                "allowed_actions": ["observation", "surface_memory"],
                "preview": "off",
                "is_group": True,
            }
        ]

    async def read_daily_usage(
        self,
        chat_id: str,
        *,
        channel: str | None = None,
    ) -> dict[str, object]:
        return {
            "status": "ok",
            "chat_id": chat_id,
            "channel": channel,
            "daily_cap": 1,
            "sent_today": 0,
            "daily_remaining": 1,
        }

    async def read_chat_window(
        self,
        chat_id: str,
        n: int = 20,
        *,
        channel: str | None = None,
    ) -> dict[str, object]:
        return {"status": "ok", "chat_id": chat_id, "channel": channel, "messages": []}

    async def read_learned_chat_taste(
        self,
        chat_id: str,
        *,
        channel: str | None = None,
        limit: int = 5,
    ) -> dict[str, object]:
        self.learned_taste_calls.append(
            {"chat_id": chat_id, "channel": channel, "limit": limit}
        )
        return self.learned_taste

    async def search_memory(
        self,
        query: str,
        chat_id: str,
        limit: int = 5,
        *,
        channel: str | None = None,
    ) -> dict[str, object]:
        self.memory_queries.append(
            {"query": query, "chat_id": chat_id, "channel": channel, "limit": limit}
        )
        if self.fail_on_empty_memory_query and not query.strip():
            pytest.fail("proactive learned taste must not come from empty search_memory")
        return {"status": "ok", "hits": []}

    async def read_speakup_history(
        self,
        chat_id: str,
        n: int = 10,
        *,
        channel: str | None = None,
    ) -> dict[str, object]:
        return {"status": "ok", "chat_id": chat_id, "channel": channel, "history": []}

    async def read_persona_for_chat(
        self,
        chat_id: str,
        *,
        channel: str | None = None,
    ) -> dict[str, object]:
        return {"status": "ok", "chat_id": chat_id, "channel": channel, "persona": None}

    async def record_silent_pass(
        self,
        *,
        chat_id: str,
        channel: str,
        reason: str,
        trigger: str,
    ) -> dict[str, object]:
        return {
            "status": "silent_pass",
            "chat_id": chat_id,
            "channel": channel,
            "reason": reason,
            "trigger": trigger,
        }


@pytest.mark.asyncio
async def test_prompt_includes_explicit_learned_taste_from_chat_taste_tool() -> None:
    tools = _LearnedTasteTools()
    captured: dict[str, Any] = {}

    def planner(prompt: str) -> str:
        captured.update(json.loads(prompt))
        return json.dumps({"silence": True, "reason": "test"})

    agent = ConsciousnessAgent(tools=tools, planner=planner)  # type: ignore[arg-type]

    await agent.run_once(trigger="cron")

    assert tools.learned_taste_calls == [
        {"chat_id": "group@g.us", "channel": "whatsapp", "limit": 5}
    ]
    assert captured["learned_taste"] == tools.learned_taste
    assert "Proactive speakup taste pattern:" in json.dumps(captured["learned_taste"])


@pytest.mark.asyncio
async def test_agent_prompt_does_not_use_empty_memory_search_for_learned_taste() -> None:
    tools = _LearnedTasteTools(fail_on_empty_memory_query=True)

    def planner(prompt: str) -> str:
        payload = json.loads(prompt)
        assert payload["learned_taste"]["patterns"]
        return json.dumps({"silence": True, "reason": "test"})

    agent = ConsciousnessAgent(tools=tools, planner=planner)  # type: ignore[arg-type]

    await agent.run_once(trigger="cron")

    assert all(str(call["query"]).strip() for call in tools.memory_queries)
