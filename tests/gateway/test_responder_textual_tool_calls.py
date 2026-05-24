from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from yeoman_gateway.adapters.policy_engine import EnginePolicyAdapter
from yeoman_gateway.adapters.responder_llm import LLMResponder
from yeoman_gateway.agent.tools.base import Tool
from yeoman_gateway.bus.queue import MessageBus
from yeoman_gateway.core.models import InboundEvent, PolicyDecision
from yeoman_gateway.media.router import ModelRouter
from yeoman_gateway.policy.engine import PolicyEngine
from yeoman_gateway.policy.schema import PolicyConfig
from yeoman_gateway.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from yeoman_gateway.session.manager import SessionManager
from yeoman_gateway.storage.private_handoff import PrivateHandoffStore
from yeoman_shared.config.schema import ModelProfile, ModelRoutingConfig


class _TextualToolThenAnswerProvider(LLMProvider):
    def __init__(self, first_response: str) -> None:
        super().__init__()
        self.first_response = first_response
        self.calls = 0
        self.second_messages: list[dict[str, Any]] = []

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
        if self.calls == 1:
            return LLMResponse(content=self.first_response)
        self.second_messages = messages
        return LLMResponse(content="eBay hat genug Baustellen: Search, Fees, Seller Trust.")

    def get_default_model(self) -> str:
        return "dummy/model"


class _RecordingDeepResearchTool(Tool):
    name = "deep_research"
    description = "record deep research calls"
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "depth": {"type": "string"},
            "max_results": {"type": "integer"},
        },
        "required": ["query"],
    }

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def execute(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return "research result: eBay marketplace execution has declined"


class _ReasoningToolThenAnswerProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0
        self.second_messages: list[dict[str, Any]] = []

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
        if self.calls == 1:
            return LLMResponse(
                content="I need fresh data.",
                reasoning_content="This requires a tool call before answering.",
                tool_calls=[
                    ToolCallRequest(
                        id="call_reasoning_1",
                        name="deep_research",
                        arguments={"query": "eBay strategic mistakes"},
                    )
                ],
            )
        self.second_messages = messages
        return LLMResponse(content="eBay hat genug Baustellen: Search, Fees, Seller Trust.")

    def get_default_model(self) -> str:
        return "deepseek-v4-flash"


class _RecordingProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[dict[str, Any]] = []

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning: dict[str, Any] | None = None,
    ) -> LLMResponse:
        self.calls.append(
            {
                "messages": messages,
                "tools": tools,
                "model": model,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "reasoning": reasoning,
            }
        )
        return LLMResponse(content="kurz.")

    def get_default_model(self) -> str:
        return "fallback/model"


def _event() -> InboundEvent:
    return InboundEvent(
        channel="whatsapp",
        chat_id="group@g.us",
        sender_id="u1",
        content="Arvid, mach mal deep search zu eBay",
        is_group=True,
        mentioned_bot=True,
    )


def _decision() -> PolicyDecision:
    return PolicyDecision(
        accept_message=True,
        should_respond=True,
        allowed_tools=frozenset({"deep_research"}),
        reason="test",
    )


def test_responder_registers_extended_tavily_tools(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    responder = LLMResponder(
        bus=MessageBus(),
        provider=_TextualToolThenAnswerProvider("done"),
        workspace=workspace,
        max_iterations=1,
    )

    try:
        assert {"web_search", "web_fetch", "web_map", "web_crawl", "deep_research"} <= responder.tool_names
    finally:
        # No async resources were opened in this test.
        pass


@pytest.mark.asyncio
async def test_fenced_textual_tool_call_is_executed_before_reply(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    provider = _TextualToolThenAnswerProvider(
        '```deep_research(query="eBay Inc. strategic mistakes", min_pages=10)\n```'
    )
    tool = _RecordingDeepResearchTool()
    responder = LLMResponder(
        bus=MessageBus(),
        provider=provider,
        workspace=workspace,
        max_iterations=3,
    )
    responder.tools.register(tool)

    out = await responder.generate_reply(_event(), _decision())

    await responder.aclose()

    assert out == "eBay hat genug Baustellen: Search, Fees, Seller Trust."
    assert provider.calls == 2
    assert tool.calls == [{"query": "eBay Inc. strategic mistakes", "min_pages": 10}]
    assert provider.second_messages[-2]["role"] == "assistant"
    assert provider.second_messages[-2]["tool_calls"][0]["function"]["name"] == "deep_research"
    assert provider.second_messages[-1]["role"] == "tool"
    assert provider.second_messages[-1]["name"] == "deep_research"


@pytest.mark.asyncio
async def test_json_textual_tool_call_is_executed_before_reply(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    provider = _TextualToolThenAnswerProvider(
        '{"tool": "deep_research", "arguments": {"query": "eBay failures", "max_results": 4}}'
    )
    tool = _RecordingDeepResearchTool()
    responder = LLMResponder(
        bus=MessageBus(),
        provider=provider,
        workspace=workspace,
        max_iterations=3,
    )
    responder.tools.register(tool)

    out = await responder.generate_reply(_event(), _decision())

    await responder.aclose()

    assert out == "eBay hat genug Baustellen: Search, Fees, Seller Trust."
    assert provider.calls == 2
    assert tool.calls == [{"query": "eBay failures", "max_results": 4}]


@pytest.mark.asyncio
async def test_reasoning_content_is_replayed_after_tool_call(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    provider = _ReasoningToolThenAnswerProvider()
    tool = _RecordingDeepResearchTool()
    responder = LLMResponder(
        bus=MessageBus(),
        provider=provider,
        workspace=workspace,
        max_iterations=3,
    )
    responder.tools.register(tool)

    out = await responder.generate_reply(_event(), _decision())

    await responder.aclose()

    assert out == "eBay hat genug Baustellen: Search, Fees, Seller Trust."
    assert provider.calls == 2
    assert provider.second_messages[-2]["role"] == "assistant"
    assert provider.second_messages[-2]["reasoning_content"] == (
        "This requires a tool call before answering."
    )
    assert provider.second_messages[-2]["tool_calls"][0]["id"] == "call_reasoning_1"
    assert provider.second_messages[-1]["role"] == "tool"


@pytest.mark.asyncio
async def test_responder_applies_resolved_profile_temperature(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    provider = _RecordingProvider()
    router = ModelRouter(
        ModelRoutingConfig(
            profiles={
                "assistant_default": ModelProfile(
                    kind="chat",
                    model="deepseek-v4-flash",
                    provider="deepseek",
                    temperature=0.42,
                )
            },
            routes={"assistant.reply": "assistant_default"},
        )
    )
    responder = LLMResponder(
        bus=MessageBus(),
        provider=_RecordingProvider(),
        workspace=workspace,
        model_router=router,
        routed_provider_factory=lambda model, provider_name: provider,
        max_iterations=1,
    )

    out = await responder.generate_reply(
        _event(),
        PolicyDecision(
            accept_message=True,
            should_respond=True,
            allowed_tools=frozenset(),
            reason="test",
            model_profile="assistantDefault",
        ),
    )

    await responder.aclose()

    assert out == "kurz."
    assert provider.calls[0]["model"] == "deepseek-v4-flash"
    assert provider.calls[0]["temperature"] == 0.42


class _HandoffSendVoiceProvider(LLMProvider):
    def __init__(self, *, chat_id: str = "4915234038957") -> None:
        super().__init__()
        self.chat_id = chat_id

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
            content=None,
            tool_calls=[
                ToolCallRequest(
                    id="voice_1",
                    name="send_voice",
                    arguments={
                        "chat_id": self.chat_id,
                        "content": "Glueckwunsch zur Million.",
                    },
                )
            ],
        )

    def get_default_model(self) -> str:
        return "test/model"


class _HandoffRepairThenAnswerProvider(LLMProvider):
    def __init__(self, *, chat_id: str = "4915234038957") -> None:
        super().__init__()
        self.calls = 0
        self.chat_id = chat_id

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
        if self.calls == 1:
            return LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        id="voice_1",
                        name="send_voice",
                        arguments={
                            "chat_id": self.chat_id,
                            "content": "Glueckwunsch zur Million.",
                        },
                    )
                ],
            )
        return LLMResponse(content="Soll ich das hier in die Gruppe oder privat schicken?")

    def get_default_model(self) -> str:
        return "test/model"


class _HandoffTextProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[dict[str, Any]] = []

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
        self.messages = messages
        return LLMResponse(content="Sehr gern, und dann zurueck in die Gruppe damit.")

    def get_default_model(self) -> str:
        return "test/model"


class _HandoffDeliveredVoiceTool(Tool):
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    @property
    def name(self) -> str:
        return "send_voice"

    @property
    def description(self) -> str:
        return "test voice sender"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "content": {"type": "string"},
                "chat_id": {"type": "string"},
            },
            "required": ["content"],
        }

    async def execute(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        target = str(kwargs.get("chat_id") or "").strip()
        if target and "@" not in target:
            target = f"{target}@s.whatsapp.net"
        return f"Voice message delivered to whatsapp:{target}."


def _handoff_decision(*, tools: frozenset[str] = frozenset({"send_voice"})) -> PolicyDecision:
    return PolicyDecision(
        accept_message=True,
        should_respond=True,
        allowed_tools=tools,
        reason="test",
    )


def _handoff_group_event(content: str) -> InboundEvent:
    return InboundEvent(
        channel="whatsapp",
        chat_id="group@g.us",
        sender_id="4915234038957",
        content=content,
        is_group=True,
        mentioned_bot=True,
    )


@pytest.mark.asyncio
async def test_private_voice_send_records_hidden_marker_and_handoff(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    sessions = SessionManager(workspace, sessions_dir=workspace / "sessions")
    handoffs = PrivateHandoffStore(workspace / "private_handoffs.json")
    voice_tool = _HandoffDeliveredVoiceTool()
    responder = LLMResponder(
        bus=MessageBus(),
        provider=_HandoffSendVoiceProvider(),
        workspace=workspace,
        session_manager=sessions,
        private_handoff_store=handoffs,
        max_iterations=1,
    )
    responder.tools.register(voice_tool)

    out = await responder.generate_reply(
        _handoff_group_event("@bot schick mir das privat als Voice"),
        _handoff_decision(),
    )

    await responder.aclose()

    assert out is None
    assert len(voice_tool.calls) == 1
    session = sessions.get_or_create("whatsapp:group@g.us")
    history = session.get_history()
    assert history[-2]["role"] == "user"
    assert history[-1]["role"] == "assistant"
    assert "Voice message delivered" in history[-1]["content"]
    assert "Do not send it again" in history[-1]["content"]

    handoff = handoffs.find_active(
        channel="whatsapp",
        chat_id="4915234038957@s.whatsapp.net",
        sender_id="4915234038957",
    )
    assert handoff is not None
    assert handoff.target_chat_id == "4915234038957@s.whatsapp.net"
    assert handoff.origin_chat_id == "group@g.us"
    assert handoff.remaining_replies == 5


@pytest.mark.asyncio
async def test_group_request_cannot_silently_dm_without_private_intent(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    voice_tool = _HandoffDeliveredVoiceTool()
    responder = LLMResponder(
        bus=MessageBus(),
        provider=_HandoffRepairThenAnswerProvider(chat_id="499999999999"),
        workspace=workspace,
        max_iterations=2,
    )
    responder.tools.register(voice_tool)

    out = await responder.generate_reply(
        _handoff_group_event("@bot gratuliere mir mal in einer Voice"),
        _handoff_decision(),
    )

    await responder.aclose()

    assert out == "Soll ich das hier in die Gruppe oder privat schicken?"
    assert voice_tool.calls == []


@pytest.mark.asyncio
async def test_private_delivery_requires_resolvable_target(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    voice_tool = _HandoffDeliveredVoiceTool()
    responder = LLMResponder(
        bus=MessageBus(),
        provider=_HandoffRepairThenAnswerProvider(chat_id="499999999999"),
        workspace=workspace,
        max_iterations=2,
    )
    responder.tools.register(voice_tool)

    out = await responder.generate_reply(
        _handoff_group_event("@bot schick das privat rueber"),
        _handoff_decision(),
    )

    await responder.aclose()

    assert out == "Soll ich das hier in die Gruppe oder privat schicken?"
    assert voice_tool.calls == []


def test_policy_adapter_allows_active_private_handoff_without_chat_policy(tmp_path: Path) -> None:
    policy = PolicyConfig.model_validate(
        {
            "channels": {
                "whatsapp": {
                    "default": {
                        "whoCanTalk": {"mode": "owner_only"},
                        "whenToReply": {"mode": "all"},
                    }
                }
            }
        }
    )
    engine = PolicyEngine(policy, workspace=tmp_path, apply_channels={"whatsapp"})
    handoffs = PrivateHandoffStore(tmp_path / "private_handoffs.json")
    handoffs.open(
        channel="whatsapp",
        target_chat_id="4915234038957@s.whatsapp.net",
        target_sender_id="4915234038957",
        origin_chat_id="group@g.us",
        origin_label="group@g.us",
    )
    adapter = EnginePolicyAdapter(
        engine=engine,
        known_tools={"message", "send_voice", "web_search"},
        workspace=tmp_path,
        private_handoff_store=handoffs,
    )

    decision = adapter.evaluate(
        InboundEvent(
            channel="whatsapp",
            chat_id="45973447385305@lid",
            sender_id="4915234038957",
            content="Danke dir",
            is_group=False,
        )
    )

    assert decision.accept_message is True
    assert decision.should_respond is True
    assert decision.reason == "private_handoff"
    assert decision.allowed_tools == frozenset()
    assert decision.private_handoff_active is True
    assert decision.private_handoff_origin_chat_id == "group@g.us"
    assert decision.private_handoff_remaining_replies == 5


@pytest.mark.asyncio
async def test_private_handoff_reply_consumes_budget_and_prompts_final_boundary(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    handoffs = PrivateHandoffStore(workspace / "private_handoffs.json")
    opened = handoffs.open(
        channel="whatsapp",
        target_chat_id="4915234038957@s.whatsapp.net",
        target_sender_id="4915234038957",
        origin_chat_id="group@g.us",
        origin_label="Kegelgruppe",
    )
    for _ in range(4):
        handoffs.consume_reply(opened.id)
    active = handoffs.find_active(
        channel="whatsapp",
        chat_id="45973447385305@lid",
        sender_id="4915234038957",
    )
    assert active is not None
    provider = _HandoffTextProvider()
    responder = LLMResponder(
        bus=MessageBus(),
        provider=provider,
        workspace=workspace,
        private_handoff_store=handoffs,
        max_iterations=1,
    )

    out = await responder.generate_reply(
        InboundEvent(
            channel="whatsapp",
            chat_id="45973447385305@lid",
            sender_id="4915234038957",
            content="haha danke",
            is_group=False,
        ),
        PolicyDecision(
            accept_message=True,
            should_respond=True,
            allowed_tools=frozenset(),
            reason="private_handoff",
            private_handoff_active=True,
            private_handoff_id=active.id,
            private_handoff_origin_chat_id=active.origin_chat_id,
            private_handoff_origin_label=active.origin_label,
            private_handoff_remaining_replies=active.remaining_replies,
        ),
    )

    await responder.aclose()

    assert out == "Sehr gern, und dann zurueck in die Gruppe damit."
    assert handoffs.find_active(
        channel="whatsapp",
        chat_id="45973447385305@lid",
        sender_id="4915234038957",
    ) is None
    prompt_text = "\n".join(
        str(message.get("content") or "") for message in provider.messages
    )
    assert "origin_chat: Kegelgruppe" in prompt_text
    assert "final allowed private reply" in prompt_text
    assert "Do not use a fixed template" in prompt_text
