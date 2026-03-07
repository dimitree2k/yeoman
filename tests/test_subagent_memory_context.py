# tests/test_subagent_memory_context.py
import pytest
from unittest.mock import AsyncMock, MagicMock
from dataclasses import dataclass
from yeoman.agent.subagent import SubagentManager


@dataclass
class FakeResponse:
    content: str = "Done."
    has_tool_calls: bool = False
    tool_calls: list = None
    def __post_init__(self):
        self.tool_calls = self.tool_calls or []


@pytest.mark.asyncio
async def test_sync_subagent_receives_memory_context(tmp_path):
    captured_messages = []

    async def capture_chat(**kwargs):
        captured_messages.append(kwargs["messages"])
        return FakeResponse()

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.chat = capture_chat
    bus = MagicMock()

    mgr = SubagentManager(provider=provider, workspace=tmp_path, bus=bus)
    await mgr.spawn_sync(
        task="Check something",
        memory_context="User prefers metric units. Lives in Berlin.",
    )

    assert len(captured_messages) == 1
    system_prompt = captured_messages[0][0]["content"]
    assert "metric units" in system_prompt
    assert "Berlin" in system_prompt


@pytest.mark.asyncio
async def test_sync_subagent_without_memory_context(tmp_path):
    captured_messages = []

    async def capture_chat(**kwargs):
        captured_messages.append(kwargs["messages"])
        return FakeResponse()

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.chat = capture_chat
    bus = MagicMock()

    mgr = SubagentManager(provider=provider, workspace=tmp_path, bus=bus)
    await mgr.spawn_sync(task="Check something")

    system_prompt = captured_messages[0][0]["content"]
    assert "Recalled Context" not in system_prompt
