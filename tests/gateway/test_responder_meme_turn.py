"""A chat that offers make_meme tells the model so, and every turn logs its tool names."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from loguru import logger
from yeoman_gateway.adapters.responder_llm import LLMResponder
from yeoman_gateway.agent.context import MEME_TOOL_NOTE
from yeoman_gateway.agent.tools.base import Tool
from yeoman_gateway.bus.queue import MessageBus
from yeoman_gateway.providers.base import LLMResponse, ToolCallRequest

from tests.gateway.test_responder_async_handoff import (
    _decision,
    _event,
    _ScriptedProvider,
)


@pytest.fixture(autouse=True)
def _project_whatsapp_responder_history(monkeypatch, request, tmp_path):
    from tests.gateway.history_test_helpers import install_projected_responder_history

    install_projected_responder_history(monkeypatch, request, tmp_path)


class _FakeMemeTool(Tool):
    name = "make_meme"
    description = "make a meme"
    parameters = {"type": "object", "properties": {"captions": {"type": "array"}}}

    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, **kwargs: Any) -> str:
        del kwargs
        self.calls += 1
        return "Meme photo delivered to the chat."


class _OtherTool(Tool):
    name = "web_search"
    description = "search"
    parameters = {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> str:
        del kwargs
        return "nothing"


async def _turn(tmp_path: Path, provider: _ScriptedProvider, *allowed: str) -> list[str]:
    lines: list[str] = []
    sink = logger.add(lambda message: lines.append(str(message)), level="INFO")
    responder = LLMResponder(
        bus=MessageBus(), provider=provider, workspace=tmp_path, max_iterations=3
    )
    responder.tools.register(_OtherTool())
    try:
        await responder.generate_reply(_event(), _decision(*allowed))
    finally:
        logger.remove(sink)
        await responder.aclose()
    return [line for line in lines if "turn_tools" in line]


def _system_text(provider: _ScriptedProvider) -> str:
    return "\n".join(
        str(message.get("content"))
        for message in provider.messages[0]
        if message.get("role") == "system"
    )


@pytest.mark.asyncio
async def test_meme_note_is_in_the_prompt_only_when_make_meme_is_offered(
    tmp_path: Path,
) -> None:
    with_meme = _ScriptedProvider([LLMResponse(content="ok")])
    await _turn(tmp_path / "a", with_meme, "make_meme", "web_search")
    assert MEME_TOOL_NOTE in _system_text(with_meme)

    without = _ScriptedProvider([LLMResponse(content="ok")])
    await _turn(tmp_path / "b", without, "web_search")
    assert MEME_TOOL_NOTE not in _system_text(without)


def test_meme_note_covers_memes_stickers_and_the_false_refusal() -> None:
    assert "meme" in MEME_TOOL_NOTE and "sticker" in MEME_TOOL_NOTE
    assert "Never claim you can't make images" in MEME_TOOL_NOTE


@pytest.mark.asyncio
async def test_turn_log_names_offered_and_called_tools_without_content(
    tmp_path: Path,
) -> None:
    provider = _ScriptedProvider(
        [
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        id="c1", name="web_search", arguments={"query": "secret words"}
                    )
                ],
            ),
            LLMResponse(content="done"),
        ]
    )
    (line,) = await _turn(tmp_path, provider, "web_search", "not_registered")
    assert "offered=web_search " in line and "called=web_search" in line
    assert "secret words" not in line and "not_registered" not in line


@pytest.mark.asyncio
async def test_turn_log_shows_a_text_only_turn(tmp_path: Path) -> None:
    provider = _ScriptedProvider([LLMResponse(content="kein Meme-Tool")])
    (line,) = await _turn(tmp_path, provider, "web_search")
    assert line.rstrip().endswith("called=-")
