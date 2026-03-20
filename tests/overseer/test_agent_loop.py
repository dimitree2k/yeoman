"""Tests for the LLM agent loop."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from yeoman_overseer.agent.budget import BudgetTracker
from yeoman_overseer.agent.loop import AgentLoop, AgentResult, BudgetExhaustedError
from yeoman_overseer.runbook.parser import Runbook
from yeoman_overseer.runbook.schema import LLMBudget, RunbookFrontmatter, TriggerConfig


def _runbook(
    profile: str = "overseerDefault",
    max_tool_calls: int = 10,
    max_tokens: int = 5000,
) -> Runbook:
    meta = RunbookFrontmatter(
        name="test-runbook",
        domain="memory",
        trigger=TriggerConfig(kind="cron", expr="0 3 * * *"),
        escalate_to_llm=True,
        llm_budget=LLMBudget(
            llm_profile=profile,
            max_tool_calls=max_tool_calls,
            max_tokens=max_tokens,
        ),
    )
    return Runbook(meta=meta, body="Check memory health.", path=Path("/tmp/test.md"))


def _budget(exhausted: bool = False) -> MagicMock:
    budget = MagicMock(spec=BudgetTracker)
    budget.can_call_llm.return_value = not exhausted
    return budget


def _fake_end_turn_response(summary: str = "all good") -> MagicMock:
    """Simulate an Anthropic response with stop_reason=end_turn."""
    block = MagicMock()
    block.type = "text"
    block.text = summary
    resp = MagicMock()
    resp.stop_reason = "end_turn"
    resp.content = [block]
    resp.usage.input_tokens = 100
    resp.usage.output_tokens = 50
    return resp


def _fake_tool_then_end(
    tool_name: str = "send_alert",
    tool_input: dict | None = None,
    summary: str = "done",
) -> list[MagicMock]:
    """Simulate a tool_use response followed by end_turn."""
    tool_block = MagicMock()
    tool_block.type = "tool_use"
    tool_block.name = tool_name
    tool_block.id = "tu_001"
    tool_block.input = tool_input or {"message": "alert!"}

    tool_resp = MagicMock()
    tool_resp.stop_reason = "tool_use"
    tool_resp.content = [tool_block]
    tool_resp.usage.input_tokens = 200
    tool_resp.usage.output_tokens = 30

    end_block = MagicMock()
    end_block.type = "text"
    end_block.text = summary
    end_resp = MagicMock()
    end_resp.stop_reason = "end_turn"
    end_resp.content = [end_block]
    end_resp.usage.input_tokens = 250
    end_resp.usage.output_tokens = 40
    return [tool_resp, end_resp]


async def test_budget_exhausted_raises() -> None:
    loop = AgentLoop(tool_ctx=MagicMock(), budget=_budget(exhausted=True), config={})
    with pytest.raises(BudgetExhaustedError):
        await loop.run(_runbook(), {})


async def test_end_turn_returns_agent_result() -> None:
    loop = AgentLoop(tool_ctx=MagicMock(), budget=_budget(), config={})
    with patch("yeoman_overseer.agent.loop.Anthropic") as mock_cls:
        client = mock_cls.return_value
        client.messages.create.return_value = _fake_end_turn_response("memory is healthy")
        result = await loop.run(_runbook(), {"entries": 100})
    assert isinstance(result, AgentResult)
    assert result.summary == "memory is healthy"
    assert result.tokens_used == 150


async def test_tool_call_dispatched_and_result_appended() -> None:
    tool_ctx = MagicMock()
    loop = AgentLoop(tool_ctx=tool_ctx, budget=_budget(), config={})
    responses = _fake_tool_then_end()
    with patch("yeoman_overseer.agent.loop.Anthropic") as mock_cls:
        with patch(
            "yeoman_overseer.agent.loop.dispatch", new_callable=AsyncMock
        ) as mock_dispatch:
            mock_dispatch.return_value = "alert sent"
            client = mock_cls.return_value
            client.messages.create.side_effect = responses
            result = await loop.run(_runbook(), {})
    mock_dispatch.assert_called_once_with("send_alert", {"message": "alert!"}, tool_ctx)
    assert result.tool_calls_made == 1


async def test_budget_consumed_after_run() -> None:
    budget = _budget()
    loop = AgentLoop(tool_ctx=MagicMock(), budget=budget, config={})
    with patch("yeoman_overseer.agent.loop.Anthropic") as mock_cls:
        client = mock_cls.return_value
        client.messages.create.return_value = _fake_end_turn_response()
        await loop.run(_runbook(), {})
    budget.consume.assert_called_once()
