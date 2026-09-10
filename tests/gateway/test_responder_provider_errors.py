from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from yeoman_gateway.adapters import responder_llm as responder_module
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


@pytest.mark.asyncio
async def test_langfuse_v4_records_generation_and_closes_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[tuple[str, Any]] = []

    class _Handle:
        def __init__(self, span_id: str) -> None:
            self.span_id = span_id

    trace = object()
    iteration = _Handle("iteration-1")
    generation = _Handle("generation-1")

    def _start_trace(**kwargs: Any) -> object:
        events.append(("start_trace", kwargs))
        return trace

    def _start_span(**kwargs: Any) -> _Handle:
        events.append(("start_span", kwargs))
        return iteration

    def _start_generation(**kwargs: Any) -> _Handle:
        events.append(("start_generation", kwargs))
        return generation

    def _end_generation(handle: _Handle | None, **kwargs: Any) -> None:
        events.append(("end_generation", {"handle": handle, **kwargs}))

    def _end_span(handle: object | None, **kwargs: Any) -> None:
        events.append(("end_span", {"handle": handle, **kwargs}))

    def _legacy_log_generation(**kwargs: Any) -> None:
        del kwargs
        raise AssertionError("responder must use the v4 generation lifecycle directly")

    monkeypatch.setattr(responder_module.lf, "start_trace", _start_trace)
    monkeypatch.setattr(responder_module.lf, "start_span", _start_span)
    monkeypatch.setattr(responder_module.lf, "start_generation", _start_generation)
    monkeypatch.setattr(responder_module.lf, "end_generation", _end_generation)
    monkeypatch.setattr(responder_module.lf, "end_span", _end_span)
    monkeypatch.setattr(responder_module.lf, "log_generation", _legacy_log_generation)

    provider = _Provider(
        [
            LLMResponse(
                content="answer",
                usage={"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
            )
        ]
    )

    responder, result = await _run(tmp_path, provider, _decision())
    try:
        assert result == "answer"
        generation_start = next(item for item in events if item[0] == "start_generation")
        assert generation_start[1]["parent"] is iteration
        assert generation_start[1]["input"]["message_count"] > 0
        assert generation_start[1]["input"]["has_tools"] is False

        generation_end = next(item for item in events if item[0] == "end_generation")
        assert generation_end[1]["handle"] is generation
        assert generation_end[1]["output"] == "answer"
        assert generation_end[1]["usage"] == {
            "prompt_tokens": 4,
            "completion_tokens": 2,
            "total_tokens": 6,
        }

        root_end = [item for item in events if item[0] == "end_span"][-1]
        assert root_end[1]["handle"] is trace
        assert root_end[1]["output"] == {"outcome": "completed", "content_chars": 6}
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
