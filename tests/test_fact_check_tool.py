# tests/test_fact_check_tool.py
import pytest
from unittest.mock import AsyncMock, MagicMock
from dataclasses import dataclass
from yeoman.agent.tools.fact_check import FactCheckTool
from yeoman.agent.subagent import SubagentManager


@dataclass
class FakeResponse:
    content: str = '{"claims": [{"claim": "X is true", "verdict": "CONFIRMED", "detail": "Verified via source."}]}'
    has_tool_calls: bool = False
    tool_calls: list = None
    def __post_init__(self):
        self.tool_calls = self.tool_calls or []


@pytest.mark.asyncio
async def test_fact_check_returns_verdicts(tmp_path):
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.chat = AsyncMock(return_value=FakeResponse())
    bus = MagicMock()

    mgr = SubagentManager(provider=provider, workspace=tmp_path, bus=bus)
    tool = FactCheckTool(manager=mgr)

    result = await tool.execute(claims="X is true. Y happened in 2024.")
    assert "CONFIRMED" in result or "verdict" in result.lower()
