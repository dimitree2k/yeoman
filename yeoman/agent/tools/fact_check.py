"""Fact-check tool — producer-reviewer pattern via sync subagent."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from yeoman.agent.tools.base import Tool

if TYPE_CHECKING:
    from yeoman.agent.subagent import SubagentManager

_REVIEWER_PROMPT = """\
You are a fact-checking reviewer. The user will give you one or more claims.
For each claim:
1. Search the web if needed to verify.
2. Return a JSON object with this structure:
{"claims": [{"claim": "...", "verdict": "CONFIRMED|REFUTED|UNCERTAIN", "detail": "..."}]}

Be concise. Only output the JSON."""


class FactCheckTool(Tool):
    def __init__(self, manager: SubagentManager) -> None:
        self._manager = manager

    @property
    def name(self) -> str:
        return "fact_check"

    @property
    def description(self) -> str:
        return (
            "Verify factual claims before including them in your reply. "
            "Pass one or more claims as text. A reviewer subagent will search "
            "the web and return verdicts (CONFIRMED/REFUTED/UNCERTAIN) for each."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "claims": {
                    "type": "string",
                    "description": "The claims to verify, separated by newlines or periods.",
                },
            },
            "required": ["claims"],
        }

    async def execute(self, **kwargs: Any) -> str:
        claims = kwargs["claims"]
        task = f"{_REVIEWER_PROMPT}\n\nClaims to verify:\n{claims}"
        return await self._manager.spawn_sync(
            task=task,
            label="fact-check",
            timeout_seconds=60.0,
        )
