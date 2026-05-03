from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from yeoman_gateway.adapters.responder_llm import LLMResponder
from yeoman_gateway.agent.tools.base import Tool
from yeoman_gateway.bus.queue import MessageBus
from yeoman_gateway.core.models import InboundEvent, PolicyDecision
from yeoman_gateway.providers.base import LLMProvider, LLMResponse


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
