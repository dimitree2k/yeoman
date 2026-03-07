"""Synchronous subagent spawn tool."""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from yeoman.agent.tools.base import Tool

if TYPE_CHECKING:
    from yeoman.agent.subagent import SubagentManager


class SpawnSyncTool(Tool):
    """Spawn a subagent and wait for its result."""

    def __init__(self, manager: SubagentManager) -> None:
        self._manager = manager

    @property
    def name(self) -> str:
        return "spawn_subagent_sync"

    @property
    def description(self) -> str:
        return (
            "Spawn a subagent to research or analyze something, and wait for the result. "
            "Use this when you need information before composing your reply. "
            "The subagent has access to web search, file reading, and analysis tools."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "Clear description of what to research or analyze.",
                },
            },
            "required": ["task"],
        }

    async def execute(self, **kwargs: Any) -> str:
        return await self._manager.spawn_sync(task=kwargs["task"])
