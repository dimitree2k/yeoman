# tests/test_core_memory_tools.py
import pytest
from pathlib import Path
from yeoman.memory.core_blocks import CoreMemoryBlock, CoreMemoryBlockStore
from yeoman.agent.tools.core_memory import (
    CoreMemoryReplaceTool,
    CoreMemoryAppendTool,
    CoreMemoryReadTool,
)


@pytest.fixture
def store(tmp_path):
    s = CoreMemoryBlockStore(tmp_path / "blocks.json")
    s.set("test:chat1", CoreMemoryBlock(label="user_facts", value="Likes coffee."))
    s.set("test:chat1", CoreMemoryBlock(label="scratchpad", value=""))
    return s


@pytest.mark.asyncio
async def test_replace_tool(store):
    tool = CoreMemoryReplaceTool(store)
    tool.set_session_key("test:chat1")
    result = await tool.execute(label="user_facts", old="coffee", new="tea")
    assert "OK" in result
    assert store.get("test:chat1", "user_facts").value == "Likes tea."


@pytest.mark.asyncio
async def test_replace_tool_missing_text(store):
    tool = CoreMemoryReplaceTool(store)
    tool.set_session_key("test:chat1")
    result = await tool.execute(label="user_facts", old="pizza", new="sushi")
    assert "not found" in result.lower()


@pytest.mark.asyncio
async def test_append_tool(store):
    tool = CoreMemoryAppendTool(store)
    tool.set_session_key("test:chat1")
    result = await tool.execute(label="scratchpad", text="Remember: check stocks.")
    assert "OK" in result
    assert store.get("test:chat1", "scratchpad").value == "Remember: check stocks."


@pytest.mark.asyncio
async def test_read_tool(store):
    tool = CoreMemoryReadTool(store)
    tool.set_session_key("test:chat1")
    result = await tool.execute()
    assert "user_facts" in result
    assert "Likes coffee" in result
