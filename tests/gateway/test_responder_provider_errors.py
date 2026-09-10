from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from yeoman_gateway.adapters.responder_llm import LLMResponder
from yeoman_gateway.agent.tools.base import Tool
from yeoman_gateway.bus.queue import MessageBus
from yeoman_gateway.core.models import InboundEvent, PolicyDecision
from yeoman_gateway.providers.base import LLMProvider, LLMResponse, ToolCallRequest


class _Provider(LLMProvider):
    def __init__(self, responses: list[LLMResponse]) -> None:
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


def _event() -> InboundEvent:
    return InboundEvent(
        channel="whatsapp",
        chat_id="chat@s.whatsapp.net",
        sender_id="sender@s.whatsapp.net",
        content="hello",
    )


def _decision(*tools: str) -> PolicyDecision:
    return PolicyDecision(
        accept_message=True,
        should_respond=True,
        allowed_tools=frozenset(tools),
        reason="test",
    )


async def _run(
    tmp_path: Path,
    provider: _Provider,
    decision: PolicyDecision,
    tool: _RecordingTool | None = None,
) -> tuple[LLMResponder, str | None]:
    responder = LLMResponder(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        max_iterations=2,
    )
    if tool is not None:
        responder.tools.register(tool)
    result = await responder.generate_reply(_event(), decision)
    return responder, result


@pytest.mark.asyncio
async def test_provider_error_does_not_become_assistant_content(tmp_path: Path) -> None:
    provider = _Provider(
        [LLMResponse(content="Error calling LLM: secret=provider-key", finish_reason="error")]
    )

    responder, result = await _run(tmp_path, provider, _decision())
    try:
        session = responder.sessions.get_or_create("whatsapp:chat@s.whatsapp.net")
        assert result is None
        assert all(message.get("role") != "assistant" for message in session.messages)
        assert all("provider-key" not in str(message) for message in session.messages)
        assert getattr(responder, "_current_session", None) is None
        assert getattr(responder, "_current_trace", None) is None
    finally:
        await responder.aclose()


@pytest.mark.asyncio
async def test_provider_error_with_tool_calls_does_not_execute_tools(tmp_path: Path) -> None:
    tool = _RecordingTool()
    provider = _Provider(
        [
            LLMResponse(
                content="Error calling LLM: secret=provider-key",
                finish_reason="error",
                tool_calls=[
                    ToolCallRequest(
                        id="call-1",
                        name="record",
                        arguments={"value": "must not run"},
                    )
                ],
            )
        ]
    )

    responder, result = await _run(tmp_path, provider, _decision("record"), tool)
    try:
        assert result is None
        assert tool.calls == []
        assert getattr(responder, "_current_session", None) is None
        assert getattr(responder, "_current_trace", None) is None
    finally:
        await responder.aclose()


@pytest.mark.asyncio
async def test_normal_error_word_text_and_tool_loop_remain_functional(tmp_path: Path) -> None:
    provider = _Provider(
        [
            LLMResponse(
                content="Error is a normal technical word",
                finish_reason="stop",
            )
        ]
    )
    responder, result = await _run(tmp_path, provider, _decision())
    try:
        assert result == "Error is a normal technical word"
    finally:
        await responder.aclose()

    tool = _RecordingTool()
    tool_provider = _Provider(
        [
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(id="call-1", name="record", arguments={"value": "ok"})
                ],
            ),
            LLMResponse(content="::reaction::🤙"),
        ]
    )
    responder, result = await _run(tmp_path / "tool", tool_provider, _decision("record"), tool)
    try:
        assert result == "::reaction::🤙"
        assert tool.calls == [{"value": "ok"}]
    finally:
        await responder.aclose()
