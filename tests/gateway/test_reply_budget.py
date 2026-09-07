from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from yeoman_gateway.adapters.responder_llm import LLMResponder
from yeoman_gateway.agent.context import ContextBuilder
from yeoman_gateway.bus.queue import MessageBus
from yeoman_gateway.core.models import InboundEvent, PolicyDecision
from yeoman_gateway.core.pipeline import PipelineContext
from yeoman_gateway.pipeline.reply_budget import ReplyBudgetMiddleware
from yeoman_gateway.policy.engine import PolicyEngine
from yeoman_gateway.policy.schema import PolicyConfig
from yeoman_gateway.providers.base import LLMProvider, LLMResponse


class _LongReplyProvider(LLMProvider):
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
        return LLMResponse(
            content=(
                "Das ist ein kompakter erster Satz, der fuer den Chat reicht. "
                "Danach kommt ein langer zweiter Satz mit zu viel erklaerendem Ballast, "
                "der im Verlauf nicht als alte Assistenzantwort weitergetragen werden soll."
            )
        )

    def get_default_model(self) -> str:
        return "test/model"


def test_policy_resolves_reply_budget_block(tmp_path: Path) -> None:
    policy = PolicyConfig.model_validate(
        {
            "defaults": {
                "replyBudget": {
                    "enabled": True,
                    "targets": {"short_take": 240, "researched_answer": 900},
                    "hardMaxChars": 480,
                    "longFormBypass": "owner_only",
                    "sessionHistoryLimit": 8,
                    "ambientWindowLimit": 3,
                }
            },
            "channels": {
                "whatsapp": {
                    "chats": {
                        "group@g.us": {
                            "replyBudget": {
                                "targets": {"short_take": 180},
                            }
                        }
                    }
                }
            },
        }
    )

    resolved = PolicyEngine(policy, workspace=tmp_path).resolve_policy("whatsapp", "group@g.us")

    assert resolved.reply_budget["enabled"] is True
    assert resolved.reply_budget["targets"]["short_take"] == 180
    assert resolved.reply_budget["targets"]["researched_answer"] == 900
    assert resolved.reply_budget["hard_max_chars"] == 480
    assert resolved.reply_budget["session_history_limit"] == 8
    assert resolved.reply_budget["ambient_window_limit"] == 3


@pytest.mark.asyncio
async def test_reply_budget_middleware_derives_turn_budget_and_preserves_reply_context() -> None:
    ctx = PipelineContext(
        event=InboundEvent(
            channel="whatsapp",
            chat_id="group@g.us",
            sender_id="u1",
            content="Arvid, kurze Meinung?",
            is_group=True,
            mentioned_bot=True,
            reply_to_message_id="quoted-1",
            reply_to_participant="u2@s.whatsapp.net",
            reply_to_text="quoted source",
            raw_metadata={
                "conversation_state": {
                    "answer_shape": "short_take",
                    "address_mode": "explicit_mention",
                    "preferred_action": "answer",
                },
                "ambient_context_window": ["a", "b", "c", "d"],
                "reply_context_window": ["quoted source", "previous"],
            },
        ),
        decision=PolicyDecision(
            accept_message=True,
            should_respond=True,
            allowed_tools=frozenset(),
            reason="test",
            reply_budget={
                "enabled": True,
                "targets": {"short_take": 180},
                "hard_max_chars": 500,
                "ambient_window_limit": 2,
            },
        ),
    )

    async def _next(inner: PipelineContext) -> None:
        inner.reply = "next reached"

    await ReplyBudgetMiddleware()(ctx, _next)

    budget = ctx.event.raw_metadata["reply_budget"]
    assert isinstance(budget, dict)
    assert budget["answer_shape"] == "short_take"
    assert budget["target_chars"] == 180
    assert budget["hard_cap_enabled"] is True
    assert ctx.event.raw_metadata["ambient_context_window"] == ["c", "d"]
    assert ctx.event.reply_to_message_id == "quoted-1"
    assert ctx.event.reply_to_participant == "u2@s.whatsapp.net"
    assert ctx.event.reply_to_text == "quoted source"
    assert ctx.event.raw_metadata["reply_context_window"] == ["quoted source", "previous"]
    assert ctx.reply == "next reached"


def test_reply_budget_prompt_is_trusted_system_context(tmp_path: Path) -> None:
    messages = ContextBuilder(tmp_path).build_messages(
        history=[],
        current_message="Arvid, kurz dazu",
        current_metadata={
            "sender_id": "u1",
            "reply_budget": {
                "enabled": True,
                "answer_shape": "short_take",
                "target_chars": 180,
                "hard_cap_enabled": True,
                "session_history_limit": 8,
                "instruction": "Answer briefly.",
            },
        },
        channel="whatsapp",
        chat_id="group@g.us",
    )

    budget_messages = [
        msg for msg in messages if msg["role"] == "system" and "[Reply Budget]" in str(msg["content"])
    ]
    assert len(budget_messages) == 1
    assert "target_chars: 180" in budget_messages[0]["content"]
    assert "UNTRUSTED INBOUND MESSAGE" not in budget_messages[0]["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("compact", [False, True])
async def test_responder_persists_budgeted_reply_in_session(tmp_path: Path, compact: bool) -> None:
    persona_text = None
    if compact:
        (tmp_path / "prompts").mkdir()
        (tmp_path / "prompts/RUNTIME.md").write_text("Runtime rules")
        (tmp_path / "prompts/AGENTS.md").write_text("Evidence rules")
        persona_text = "<!-- prompt-chain: compact -->\nPersona"
    responder = LLMResponder(
        bus=MessageBus(),
        provider=_LongReplyProvider(),
        workspace=tmp_path,
        max_iterations=1,
    )
    event = InboundEvent(
        channel="whatsapp",
        chat_id="group@g.us",
        sender_id="u1",
        content="Arvid, kurze Einschaetzung",
        is_group=True,
        mentioned_bot=True,
        raw_metadata={
            "conversation_state": {"answer_shape": "short_take"},
            "reply_budget": {
                "enabled": True,
                "answer_shape": "short_take",
                "target_chars": 80,
                "hard_cap_enabled": True,
            },
        },
    )

    reply = await responder.generate_reply(
        event,
        PolicyDecision(
            accept_message=True,
            should_respond=True,
            allowed_tools=frozenset(),
            reason="test",
            persona_text=persona_text,
        ),
    )

    session = responder.sessions.get_or_create("whatsapp:group@g.us")
    assistant_rows = [row for row in session.messages if row.get("role") == "assistant"]
    expected = (await _LongReplyProvider().chat([])).content if compact else "Das ist ein kompakter erster Satz, der fuer den Chat reicht."
    assert reply == expected
    assert assistant_rows[-1]["content"] == reply
