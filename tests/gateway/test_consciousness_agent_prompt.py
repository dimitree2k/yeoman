"""Tests for consciousness agent prompt policy shaping."""

from __future__ import annotations

import json
from typing import Any

import pytest
from yeoman_gateway.consciousness.agent import ConsciousnessAgent


class _PromptTools:
    def __init__(self, *, trigger: str = "burst") -> None:
        self._trigger = trigger

    def current_trigger(self) -> str:
        return self._trigger

    async def read_daily_usage(
        self,
        chat_id: str,
        *,
        channel: str | None = None,
    ) -> dict[str, object]:
        del chat_id, channel
        return {
            "status": "ok",
            "daily_cap": 3,
            "base_daily_cap": 3,
            "max_daily_cap": 6,
            "sent_today": 0,
            "daily_remaining": 3,
            "budget_allowed": True,
            "budget_reason": "base_daily_cap_available",
        }

    async def read_chat_window(
        self,
        chat_id: str,
        n: int = 20,
        *,
        channel: str | None = None,
    ) -> dict[str, object]:
        del chat_id, n, channel
        return {
            "status": "ok",
            "messages": [
                {
                    "message_id": "m1",
                    "text": "tax flex banter",
                }
            ],
        }

    async def search_memory(
        self,
        query: str,
        chat_id: str,
        limit: int = 5,
        *,
        channel: str | None = None,
    ) -> dict[str, object]:
        del query, chat_id, limit, channel
        return {"status": "ok", "hits": []}

    async def read_learned_chat_taste(
        self,
        chat_id: str,
        limit: int = 5,
        *,
        channel: str | None = None,
    ) -> dict[str, object]:
        del chat_id, limit, channel
        return {
            "status": "ok",
            "patterns": [
                {
                    "content": "Low receptivity to proactive corrections in high-noise banter.",
                    "confidence": 0.86,
                }
            ],
        }

    async def read_speakup_history(
        self,
        chat_id: str,
        n: int = 20,
        *,
        channel: str | None = None,
    ) -> dict[str, object]:
        del chat_id, n, channel
        return {"status": "ok", "history": []}

    async def read_persona_for_chat(
        self,
        chat_id: str,
        *,
        channel: str | None = None,
    ) -> dict[str, object]:
        del chat_id, channel
        return {"status": "ok", "persona": "Speak tersely as Arvid."}


async def _prompt_for_profile(profile: str) -> dict[str, Any]:
    agent = ConsciousnessAgent(
        tools=_PromptTools(),  # type: ignore[arg-type]
        planner=lambda prompt: {"silence": True, "reason": prompt},
    )
    prompt = await agent._build_prompt(  # noqa: SLF001
        channel="whatsapp",
        chat_id="group@g.us",
        eligible=[
            {
                "channel": "whatsapp",
                "chat_id": "group@g.us",
                "profile": profile,
                "allowed_actions": [
                    "answer_open_question",
                    "contrarian",
                    "light_humor",
                    "observation",
                    "share_opinion",
                ],
                "daily_cap": 3,
                "preview": "none",
                "is_group": True,
            }
        ],
    )
    return json.loads(prompt)


@pytest.mark.asyncio
async def test_permissive_prompt_treats_learned_taste_as_caution_not_veto() -> None:
    payload = await _prompt_for_profile("permissive")
    joined_rules = "\n".join(payload["golden_rules"])

    assert "learned_taste is a caution, not a veto" in joined_rules
    assert "short low-risk line" in joined_rules


@pytest.mark.asyncio
async def test_permissive_prompt_allows_social_warmth_without_data_heavy_fact() -> None:
    payload = await _prompt_for_profile("permissive")
    joined_rules = "\n".join(payload["golden_rules"])

    assert "social warmth" in joined_rules
    assert "compact joke" in joined_rules
    assert "concrete opinion" in joined_rules
    assert "does not need a sourced fact every time" in joined_rules


@pytest.mark.asyncio
async def test_balanced_prompt_keeps_strict_weak_post_silence_rule() -> None:
    payload = await _prompt_for_profile("balanced")
    joined_rules = "\n".join(payload["golden_rules"])

    assert "Prefer silence over a weak post" in joined_rules
    assert "learned_taste is a caution, not a veto" not in joined_rules


@pytest.mark.asyncio
async def test_prompt_requires_correct_error_to_quote_actual_claim() -> None:
    payload = await _prompt_for_profile("permissive")
    joined_rules = "\n".join(payload["golden_rules"])

    assert "For action_type correct_error" in joined_rules
    assert "reply_to_message_id must identify the message containing the claim" in joined_rules
    assert "downgrade to answer_open_question" in joined_rules
