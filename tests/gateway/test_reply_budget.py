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
    # Compact personas are no longer exempt: the configured budget clips both branches.
    assert reply == _SHORT_REPLY
    assert assistant_rows[-1]["content"] == reply


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("extra_metadata", "content", "expected"),
    [
        ({}, "Arvid, kurze Einschaetzung", _SHORT_REPLY),
        ({"reply_budget_tool_used": True}, "Arvid, kurze Einschaetzung", _SHORT_REPLY),
        ({}, "Arvid, was macht die Aktie heute?", _LONG_REPLY),
    ],
    ids=["short_no_tool_no_signal", "short_tool_used", "long_current_data_signal"],
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
                "hard_max_chars": 600,
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


@pytest.mark.parametrize("word", ["heute", "jetzt", "gerade", "aktuell", "stand", "current", "today", "latest"])
def test_casual_time_words_do_not_relax_reply_budget(word: str) -> None:
    from yeoman_gateway.reply_budget import derive_reply_budget, enforce_reply_budget

    content = f"Du redest {word} viel."
    budget = derive_reply_budget(
        policy={"enabled": True, "targets": {"social_one_liner": 80}},
        answer_shape="social_one_liner", content=content, is_owner=False,
    )
    assert budget is not None and budget.hard_cap_enabled
    reply, _ = enforce_reply_budget(
        _LONG_REPLY, budget.as_metadata(), user_content=content, tool_used=True,
    )
    assert reply == _SHORT_REPLY


@pytest.mark.parametrize("content", ["Quelle: https://example.org/ " + _LONG_REPLY, "Der Aktienkurs ist unsicher. " + _LONG_REPLY])
def test_evidence_still_uses_long_form_limit(content: str) -> None:
    from yeoman_gateway.reply_budget import derive_reply_budget, enforce_reply_budget

    budget = derive_reply_budget(policy={"enabled": True, "targets": {"short_take": 80}}, answer_shape="short_take", content="Kurz bitte", is_owner=False)
    assert budget is not None
    reply, _ = enforce_reply_budget(content, budget.as_metadata())
    assert reply == content
