from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from loguru import logger
from yeoman_gateway.adapters.responder_llm import LLMResponder
from yeoman_gateway.agent.tools.base import Tool
from yeoman_gateway.bus.queue import MessageBus
from yeoman_gateway.core.models import InboundEvent, PolicyDecision
from yeoman_gateway.providers.base import LLMProvider, LLMResponse, ToolCallRequest


class _UsageProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0
        self.responses = [
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(id="call-1", name="record", arguments={"value": "x"})
                ],
                usage={"prompt_tokens": 100, "completion_tokens": 20},
            ),
            LLMResponse(
                content="done",
                usage={"prompt_tokens": 100, "completion_tokens": 20},
            ),
        ]

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
        return response

    def get_default_model(self) -> str:
        return "test/provider"


class _RecordingTool(Tool):
    name = "record"
    description = "record one call"
    parameters = {"type": "object", "properties": {"value": {"type": "string"}}}

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def execute(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return "recorded"


@pytest.mark.asyncio
async def test_reply_loop_logs_per_call_and_turn_usage(tmp_path: Path) -> None:
    provider = _UsageProvider()
    responder = LLMResponder(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        max_iterations=3,
    )
    tool = _RecordingTool()
    responder.tools.register(tool)
    messages: list[str] = []
    sink = logger.add(lambda message: messages.append(message.record["message"]), level="INFO")
    try:
        result = await responder.generate_reply(
            InboundEvent(
                channel="telegram",
                chat_id="group",
                sender_id="10001",
                content="record please",
                is_group=True,
            ),
            PolicyDecision(
                accept_message=True,
                should_respond=True,
                allowed_tools=frozenset({"record"}),
                reason="test",
            ),
        )
    finally:
        logger.remove(sink)
        await responder.aclose()

    assert result == "done"
    assert len(tool.calls) == 1

    call_lines = [line for line in messages if line.startswith("reply_llm_call ")]
    turn_lines = [line for line in messages if line.startswith("reply_turn_usage ")]
    assert len(call_lines) == 2
    assert len(turn_lines) == 1
    assert "finish_reason=stop" in call_lines[0]
    assert "tool_calls=1" in call_lines[0]
    assert "tool_calls=0" in call_lines[1]
    usage_line = turn_lines[0]
    assert "iterations=2" in usage_line
    assert "prompt_tokens_total=200" in usage_line
    assert "completion_tokens_total=40" in usage_line
    assert "outcome=completed" in usage_line
    assert "record please" not in usage_line
    assert "done" not in usage_line
