"""A turn that hands work off to the background owes the chat an acknowledgement.

The delegation tool publishes a turn signal; the reply path must then budget and guard
the turn as a status line instead of an answer, and must not keep working on the task
it just handed off.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from yeoman_gateway.adapters.responder_llm import LLMResponder
from yeoman_gateway.agent.tools.base import Tool
from yeoman_gateway.bus.queue import MessageBus
from yeoman_gateway.core.models import InboundEvent, PolicyDecision
from yeoman_gateway.processing.tool_context import (
    ASYNC_HANDOFF_SIGNAL,
    publish_turn_signal,
)
from yeoman_gateway.providers.base import LLMProvider, LLMResponse, ToolCallRequest

_BUDGET = {
    "enabled": True,
    "targets": {"short_take": 420, "ack": 150},
    "hardMaxChars": 600,
    "longFormMaxChars": 2200,
    "longFormBypass": "owner_only",
}


class _ScriptedProvider(LLMProvider):
    def __init__(self, responses: list[LLMResponse]) -> None:
        super().__init__()
        self.responses = responses
        self.calls = 0
        self.tool_definitions: list[list[dict[str, Any]]] = []
        self.messages: list[list[dict[str, Any]]] = []

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning: dict[str, Any] | None = None,
    ) -> LLMResponse:
        del model, max_tokens, temperature, reasoning
        self.messages.append(list(messages))
        self.tool_definitions.append(list(tools or []))
        response = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        return response

    def get_default_model(self) -> str:
        return "test/provider"


class _HandoffTool(Tool):
    """Stands in for a delegation tool that accepted background work."""

    name = "delegate"
    description = "hand work off to a background worker"
    parameters = {"type": "object", "properties": {}}

    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, **kwargs: Any) -> str:
        del kwargs
        self.calls += 1
        publish_turn_signal(
            ASYNC_HANDOFF_SIGNAL,
            {"state": "accepted", "worker": "hermes", "skill": "research.deep"},
        )
        return "[hermes | research.deep | TASK_STATE_WORKING | task-1]\nRuntime note: accepted."


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
        content="mach mal deep research",
    )


def _decision(*tools: str) -> PolicyDecision:
    return PolicyDecision(
        accept_message=True,
        should_respond=True,
        allowed_tools=frozenset(tools),
        reason="test",
        reply_budget=dict(_BUDGET),
    )


async def _run(
    tmp_path: Path,
    provider: _ScriptedProvider,
    decision: PolicyDecision,
    *tools: Tool,
) -> tuple[LLMResponder, str | None]:
    responder = LLMResponder(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        max_iterations=3,
    )
    for tool in tools:
        responder.tools.register(tool)
    result = await responder.generate_reply(_event(), decision)
    return responder, result


@pytest.mark.asyncio
async def test_handoff_turn_is_budgeted_as_acknowledgement_and_stops_working(
    tmp_path: Path,
) -> None:
    handoff = _HandoffTool()
    recorder = _RecordingTool()
    provider = _ScriptedProvider(
        [
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(id="c1", name="delegate", arguments={}),
                    ToolCallRequest(id="c2", name="record", arguments={"value": "x"}),
                ],
            ),
            LLMResponse(content="Läuft, ich melde mich gleich mit dem Ergebnis. " + "y" * 400),
        ]
    )

    responder, result = await _run(
        tmp_path, provider, _decision("delegate", "record"), handoff, recorder
    )
    try:
        assert provider.calls == 2
        # The call after the handoff runs without tools at all.
        assert provider.tool_definitions[1] == []
        # The tool call that merely accompanied the delegation was not executed.
        assert recorder.calls == []
        assert any(
            "Not executed" in str(message)
            for message in provider.messages[1]
        )
        # The request was classified short_take (420); the acknowledgement is not: the
        # 447-character answer is compressed to its first sentence.
        assert result == "Läuft, ich melde mich gleich mit dem Ergebnis."
    finally:
        await responder.aclose()


@pytest.mark.asyncio
async def test_short_acknowledgement_is_not_rejected_as_deferred_work(tmp_path: Path) -> None:
    """After a handoff, "I'll come back to you" is backed by real, running work."""
    handoff = _HandoffTool()
    provider = _ScriptedProvider(
        [
            LLMResponse(
                content=None,
                tool_calls=[ToolCallRequest(id="c1", name="delegate", arguments={})],
            ),
            LLMResponse(content="Ich checke das kurz, einen Moment."),
        ]
    )

    responder, result = await _run(tmp_path, provider, _decision("delegate"), handoff)
    try:
        assert result == "Ich checke das kurz, einen Moment."
        assert provider.calls == 2, "the promise was not treated as unbacked work"
    finally:
        await responder.aclose()


@pytest.mark.asyncio
async def test_turn_without_handoff_keeps_its_classified_budget(tmp_path: Path) -> None:
    """Control: without a handoff nothing is re-budgeted and tools stay available."""
    recorder = _RecordingTool()
    provider = _ScriptedProvider([LLMResponse(content="z" * 400)])

    responder, result = await _run(tmp_path, provider, _decision("record"), recorder)
    try:
        assert provider.tool_definitions[0] != [], "tools are only locked after a handoff"
        assert result is not None
        assert len(result) == 400
    finally:
        await responder.aclose()
