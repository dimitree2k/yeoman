"""Tests for the LLM agent loop."""
from __future__ import annotations

import json
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


def _fake_stop_response(summary: str = "all good") -> MagicMock:
    """Simulate an OpenAI response with finish_reason=stop."""
    message = MagicMock()
    message.content = summary
    message.tool_calls = None
    message.model_dump.return_value = {
        "role": "assistant",
        "content": summary,
        "tool_calls": None,
    }
    choice = MagicMock()
    choice.finish_reason = "stop"
    choice.message = message
    resp = MagicMock()
    resp.choices = [choice]
    resp.usage.prompt_tokens = 100
    resp.usage.completion_tokens = 50
    return resp


def _fake_tool_then_stop(
    tool_name: str = "send_alert",
    tool_input: dict | None = None,
    summary: str = "done",
) -> list[MagicMock]:
    """Simulate a tool_calls response followed by stop."""
    input_args = tool_input or {"message": "alert!"}

    tc = MagicMock()
    tc.id = "call_001"
    tc.function.name = tool_name
    tc.function.arguments = json.dumps(input_args)

    tool_message = MagicMock()
    tool_message.content = None
    tool_message.tool_calls = [tc]
    tool_message.model_dump.return_value = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_001",
                "type": "function",
                "function": {"name": tool_name, "arguments": json.dumps(input_args)},
            }
        ],
    }
    tool_choice = MagicMock()
    tool_choice.finish_reason = "tool_calls"
    tool_choice.message = tool_message
    tool_resp = MagicMock()
    tool_resp.choices = [tool_choice]
    tool_resp.usage.prompt_tokens = 200
    tool_resp.usage.completion_tokens = 30

    end_message = MagicMock()
    end_message.content = summary
    end_message.tool_calls = None
    end_message.model_dump.return_value = {
        "role": "assistant",
        "content": summary,
        "tool_calls": None,
    }
    end_choice = MagicMock()
    end_choice.finish_reason = "stop"
    end_choice.message = end_message
    end_resp = MagicMock()
    end_resp.choices = [end_choice]
    end_resp.usage.prompt_tokens = 250
    end_resp.usage.completion_tokens = 40

    return [tool_resp, end_resp]


async def test_budget_exhausted_raises() -> None:
    loop = AgentLoop(tool_ctx=MagicMock(), budget=_budget(exhausted=True), config={})
    with pytest.raises(BudgetExhaustedError):
        await loop.run(_runbook(), {})


async def test_end_turn_returns_agent_result() -> None:
    loop = AgentLoop(tool_ctx=MagicMock(), budget=_budget(), config={})
    with patch("yeoman_overseer.agent.loop.OpenAI") as mock_cls:
        client = mock_cls.return_value
        client.chat.completions.create.return_value = _fake_stop_response("memory is healthy")
        result = await loop.run(_runbook(), {"entries": 100})
    assert isinstance(result, AgentResult)
    assert result.summary == "memory is healthy"
    assert result.tokens_used == 150


async def test_tool_call_dispatched_and_result_appended() -> None:
    tool_ctx = MagicMock()
    loop = AgentLoop(tool_ctx=tool_ctx, budget=_budget(), config={})
    responses = _fake_tool_then_stop()
    with patch("yeoman_overseer.agent.loop.OpenAI") as mock_cls:
        with patch(
            "yeoman_overseer.agent.loop.dispatch", new_callable=AsyncMock
        ) as mock_dispatch:
            mock_dispatch.return_value = "alert sent"
            client = mock_cls.return_value
            client.chat.completions.create.side_effect = responses
            result = await loop.run(_runbook(), {})
    mock_dispatch.assert_called_once_with("send_alert", {"message": "alert!"}, tool_ctx)
    assert result.tool_calls_made == 1


async def test_budget_consumed_after_run() -> None:
    budget = _budget()
    loop = AgentLoop(tool_ctx=MagicMock(), budget=budget, config={})
    with patch("yeoman_overseer.agent.loop.OpenAI") as mock_cls:
        client = mock_cls.return_value
        client.chat.completions.create.return_value = _fake_stop_response()
        await loop.run(_runbook(), {})
    budget.consume.assert_called_once()
