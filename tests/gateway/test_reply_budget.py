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

_LONG_REPLY = (
    "Das ist ein kompakter erster Satz, der fuer den Chat reicht. "
    "Danach kommt ein langer zweiter Satz mit zu viel erklaerendem Ballast, "
    "der im Verlauf nicht als alte Assistenzantwort weitergetragen werden soll."
)
_SHORT_REPLY = "Das ist ein kompakter erster Satz, der fuer den Chat reicht."


class _LongReplyProvider(LLMProvider):
    rewrite = _SHORT_REPLY

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
        # The budget's shorten request gets a rewrite that fits; every turn gets the long draft.
        if "You shorten a chat reply" in str(messages[0].get("content", "")):
            return LLMResponse(content=self.rewrite)
        return LLMResponse(content=_LONG_REPLY)

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



@pytest.fixture(autouse=True)
def _project_whatsapp_responder_history(monkeypatch, request, tmp_path):
    from tests.gateway.history_test_helpers import install_projected_responder_history

    install_projected_responder_history(monkeypatch, request, tmp_path)


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
    assert ctx.event.raw_metadata["ambient_context_window"] == ["a", "b", "c", "d"]
    assert ctx.event.reply_to_message_id == "quoted-1"
    assert ctx.event.reply_to_participant == "u2@s.whatsapp.net"
    assert ctx.event.reply_to_text == "quoted source"
    assert ctx.event.raw_metadata["reply_context_window"] == ["quoted source", "previous"]
    assert ctx.reply == "next reached"


@pytest.mark.parametrize("compact", [False, True])
def test_all_configured_ambient_messages_reach_the_prompt(tmp_path: Path, compact: bool) -> None:
    from yeoman_shared.config.schema import Config

    assert Config().channels.whatsapp.ambient_window_limit == 30
    lines = [f"[person] fact-{index:02d}" for index in range(30)]
    if compact:
        (tmp_path / "prompts").mkdir()
        (tmp_path / "prompts/RUNTIME.md").write_text("Runtime rules")
        (tmp_path / "prompts/AGENTS.md").write_text("Evidence rules")
    persona = "<!-- prompt-chain: compact -->\nPersona" if compact else None
    messages = ContextBuilder(tmp_path).build_messages(
        history=[], current_message="Calculate the result", persona_text=persona,
        current_metadata={"ambient_context_window": lines},
        channel="whatsapp", chat_id="group@g.us",
    )
    assert all(line in str(messages[-1]["content"]) for line in lines)


@pytest.mark.parametrize("compact", [False, True])
def test_reply_budget_prompt_is_trusted_system_context(tmp_path: Path, compact: bool) -> None:
    persona_text = None
    if compact:
        (tmp_path / "prompts").mkdir()
        (tmp_path / "prompts/RUNTIME.md").write_text("Runtime rules")
        (tmp_path / "prompts/AGENTS.md").write_text("Evidence rules")
        persona_text = "<!-- prompt-chain: compact -->\nPersona"
    messages = ContextBuilder(tmp_path).build_messages(
        history=[],
        current_message="Arvid, kurz dazu",
        persona_text=persona_text,
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
async def test_responder_applies_budget_before_return(tmp_path: Path, compact: bool) -> None:
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
                "hard_max_chars": 80,
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


    # WhatsApp reply history is archived by dispatch; generation applies its budget first.
    assert reply == _SHORT_REPLY



@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("extra_metadata", "content", "expected"),
    [
        ({}, "Arvid, kurze Einschaetzung", _SHORT_REPLY),
        # Wording no longer relaxes the ceiling; only a research tool the turn called does.
        ({}, "Arvid, was macht die Aktie heute?", _SHORT_REPLY),
        ({"reply_budget_research_used": True}, "Arvid, kurze Einschaetzung", _LONG_REPLY),
    ],
    ids=["short_no_research", "keywords_do_not_relax", "research_tool_used"],
)
async def test_compact_persona_reply_budget_keeps_short_and_long_form_branches(
    tmp_path: Path,
    extra_metadata: dict[str, object],
    content: str,
    expected: str,
) -> None:
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts/RUNTIME.md").write_text("Runtime rules")
    (tmp_path / "prompts/AGENTS.md").write_text("Evidence rules")
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
        content=content,
        is_group=True,
        mentioned_bot=True,
        raw_metadata={
            "conversation_state": {"answer_shape": "short_take"},
            "reply_budget": {
                "enabled": True,
                "answer_shape": "short_take",
                "target_chars": 80,
                "hard_max_chars": 80,
                "long_form_max_chars": 2200,
                "long_form_allowed": False,
                "hard_cap_enabled": True,
            },
            **extra_metadata,
        },
    )

    reply = await responder.generate_reply(
        event,
        PolicyDecision(
            accept_message=True,
            should_respond=True,
            allowed_tools=frozenset(),
            reason="test",
            persona_text="<!-- prompt-chain: compact -->\nPersona",
        ),
    )

    assert reply == expected


@pytest.mark.parametrize(
    "content",
    ["Du redest heute viel.", "Erklär mir ausführlich die Aktie", "Quelle: https://example.org/"],
)
def test_wording_does_not_change_the_budget(content: str) -> None:
    """Whether an answer needs room is the writing model's call, not a keyword match."""
    from yeoman_gateway.reply_budget import derive_reply_budget

    del content
    budget = derive_reply_budget(
        policy={"enabled": True, "targets": {"short_take": 80}, "hard_max_chars": 300},
        answer_shape="short_take",
        is_owner=False,
    )
    assert budget is not None
    assert budget.hard_cap_enabled and not budget.long_form_allowed
    assert "Aim for about 80 characters" in budget.instruction
    assert "stay under 300 characters" in budget.instruction


def test_owner_long_form_follows_policy_not_wording() -> None:
    from yeoman_gateway.reply_budget import derive_reply_budget, enforce_reply_budget

    budget = derive_reply_budget(
        policy={"enabled": True, "targets": {"short_take": 80}, "hard_max_chars": 100,
                "long_form_max_chars": 2000, "long_form_bypass": "owner_only"},
        answer_shape="short_take",
        is_owner=True,
    )
    assert budget is not None and budget.long_form_allowed
    assert "stay under 2000 characters" in budget.instruction
    _, result = enforce_reply_budget("x" * 500, budget.as_metadata())
    assert result["over_budget"] is False


def _short_take_budget(target: int = 420) -> dict[str, object]:
    from yeoman_gateway.reply_budget import derive_reply_budget

    budget = derive_reply_budget(
        policy={"enabled": True, "targets": {"short_take": target}, "hard_max_chars": target},
        answer_shape="short_take",
        is_owner=False,
    )
    assert budget is not None and budget.hard_cap_enabled
    return budget.as_metadata()


def test_over_budget_reply_is_never_cut() -> None:
    from yeoman_gateway.reply_budget import enforce_reply_budget

    # The Finanzgruppe failure: the findings after a one-line opener were cut away.
    text = "Gefunden, was es gibt und was nicht. " + "Eine Umfrage zeigt Ablehnung. " * 20
    reply, result = enforce_reply_budget(text, _short_take_budget(200))

    assert reply == text.strip()
    assert result == {
        "over_budget": True,
        "reason": "hard_max_chars",
        "limit": 200,
        "before_chars": len(text.strip()),
    }


@pytest.mark.parametrize(
    ("rewrite", "accepted"),
    [
        ("Kurz und passend.", True),
        ("", False),
        (None, False),
        ("x" * 81, False),
    ],
    ids=["fits", "empty", "missing", "still_too_long"],
)
def test_shortened_reply_is_accepted_only_when_it_fits(rewrite: str | None, accepted: bool) -> None:
    from yeoman_gateway.reply_budget import accept_shortened_reply

    result = accept_shortened_reply(_LONG_REPLY, rewrite, 80)

    assert (result == rewrite) if accepted else result is None


@pytest.mark.asyncio
async def test_responder_sends_full_reply_when_rewrite_still_too_long(tmp_path: Path) -> None:
    provider = _LongReplyProvider()
    provider.rewrite = _LONG_REPLY[:-5]
    responder = LLMResponder(
        bus=MessageBus(), provider=provider, workspace=tmp_path, max_iterations=1,
    )
    event = InboundEvent(
        channel="whatsapp",
        chat_id="group@g.us",
        sender_id="u1",
        content="Arvid, kurze Einschaetzung",
        is_group=True,
        mentioned_bot=True,
        raw_metadata={
            "reply_budget": {
                "enabled": True,
                "answer_shape": "short_take",
                "target_chars": 80,
                "hard_max_chars": 80,
                "hard_cap_enabled": True,
            },
        },
    )

    reply = await responder.generate_reply(
        event,
        PolicyDecision(
            accept_message=True, should_respond=True, allowed_tools=frozenset(), reason="test",
        ),
    )

    assert reply == _LONG_REPLY


def test_research_tool_turn_gets_the_long_form_limit() -> None:
    from yeoman_gateway.reply_budget import enforce_reply_budget

    budget = _short_take_budget(80)
    _, capped = enforce_reply_budget(_LONG_REPLY, budget)
    researched, result = enforce_reply_budget(
        _LONG_REPLY, budget, research_used=True
    )

    assert capped["over_budget"] is True
    assert researched == _LONG_REPLY
    assert result["over_budget"] is False

