"""ToolRegistry logs a content-free line for every failed tool result."""
from __future__ import annotations

from typing import Any

import pytest
from loguru import logger
from yeoman_gateway.agent.tools.base import Tool
from yeoman_gateway.agent.tools.registry import ToolRegistry


class _Tool(Tool):
    def __init__(self, result: Any = "ok", exc: Exception | None = None) -> None:
        self._result = result
        self._exc = exc

    @property
    def name(self) -> str:
        return "make_meme"

    @property
    def description(self) -> str:
        return "test"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> Any:
        if self._exc is not None:
            raise self._exc
        return self._result


@pytest.fixture
def lines() -> list[str]:
    captured: list[str] = []
    sink = logger.add(lambda m: captured.append(m.record["message"]), level="WARNING")
    yield captured
    logger.remove(sink)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "kind"),
    [
        (_Tool(result="Error: template not found for Hallo"), "error_result"),
        (_Tool(exc=ValueError("secret text")), "exception:ValueError"),
    ],
)
async def test_failed_result_is_logged_without_content(lines, tool, kind) -> None:
    registry = ToolRegistry()
    registry.register(tool)
    result = await registry.execute("make_meme", {})
    assert result.startswith("Error")
    assert lines == [f"tool_result_failed tool=make_meme kind={kind}"]


@pytest.mark.asyncio
async def test_unknown_tool_and_success(lines) -> None:
    registry = ToolRegistry()
    registry.register(_Tool())
    assert await registry.execute("make_meme", {}) == "ok"
    await registry.execute("missing", {})
    assert lines == ["tool_result_failed tool=missing kind=not_found"]
