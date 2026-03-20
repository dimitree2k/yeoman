"""Agent loop — invoke Anthropic API with tools, enforce limits, return AgentResult."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from anthropic import Anthropic

from yeoman_overseer.agent.context import build_context
from yeoman_overseer.agent.tools import TOOL_DEFINITIONS, ToolContext, dispatch

if TYPE_CHECKING:
    from yeoman_overseer.agent.budget import BudgetTracker
    from yeoman_overseer.runbook.parser import Runbook


class BudgetExhaustedError(Exception):
    pass


@dataclass
class AgentResult:
    runbook_name: str
    domain: str
    summary: str
    tool_calls_made: int
    tokens_used: int
    llm_profile: str


class AgentLoop:
    def __init__(
        self,
        tool_ctx: ToolContext,
        budget: BudgetTracker,
        config: dict[str, Any],
    ) -> None:
        self._tool_ctx = tool_ctx
        self._budget = budget
        self._config = config

    async def run(self, runbook: Runbook, observations: dict[str, Any]) -> AgentResult:
        domain = runbook.meta.domain
        if not self._budget.can_call_llm(domain):
            raise BudgetExhaustedError(f"LLM budget exhausted for domain={domain}")

        from yeoman_overseer.runbook.schema import LLMBudget

        llm_budget = runbook.meta.llm_budget
        if llm_budget is None:
            llm_budget = LLMBudget()

        profile_name = llm_budget.llm_profile
        profile = self._config.get("models", {}).get("profiles", {}).get(profile_name, {})
        model = profile.get("model", "claude-haiku-4-5-20251001")

        context = build_context(runbook, observations, self._tool_ctx.audit)

        client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": context.user_message},
        ]
        tool_calls_made = 0
        total_tokens = 0
        summary = ""

        while tool_calls_made <= llm_budget.max_tool_calls:
            remaining_tokens = llm_budget.max_tokens - total_tokens
            if remaining_tokens <= 0:
                break

            response = client.messages.create(
                model=model,
                max_tokens=min(4096, remaining_tokens),
                system=context.system_prompt,
                messages=messages,
                tools=TOOL_DEFINITIONS,
            )
            total_tokens += response.usage.input_tokens + response.usage.output_tokens

            if response.stop_reason == "end_turn":
                for block in response.content:
                    if hasattr(block, "text"):
                        summary = block.text
                break

            if response.stop_reason == "tool_use":
                messages.append({"role": "assistant", "content": response.content})
                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        tool_calls_made += 1
                        try:
                            result = await dispatch(block.name, block.input, self._tool_ctx)
                        except Exception as exc:
                            result = f"ERROR: {exc}"
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": str(result),
                        })
                messages.append({"role": "user", "content": tool_results})
            else:
                break

        self._budget.consume(total_tokens, 1)

        return AgentResult(
            runbook_name=runbook.meta.name,
            domain=domain,
            summary=summary,
            tool_calls_made=tool_calls_made,
            tokens_used=total_tokens,
            llm_profile=profile_name,
        )
