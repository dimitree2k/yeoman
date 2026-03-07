"""Tools for agent self-editing of core memory blocks."""

from __future__ import annotations

from typing import Any

from yeoman.agent.tools.base import Tool
from yeoman.memory.core_blocks import CoreMemoryBlockStore


class _CoreMemoryToolBase(Tool):
    """Shared state for core memory tools."""

    def __init__(self, store: CoreMemoryBlockStore) -> None:
        self._store = store
        self._session_key: str | None = None

    def set_session_key(self, key: str) -> None:
        self._session_key = key

    def _require_session(self) -> str:
        if not self._session_key:
            raise RuntimeError("Core memory tool used without session context")
        return self._session_key


class CoreMemoryReplaceTool(_CoreMemoryToolBase):
    @property
    def name(self) -> str:
        return "core_memory_replace"

    @property
    def description(self) -> str:
        return (
            "Replace a substring in a core memory block. "
            "Use this to update facts, correct outdated info, or refine notes. "
            "The old text must match exactly."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "label": {
                    "type": "string",
                    "description": "Memory block label (e.g. 'user_facts', 'scratchpad').",
                },
                "old": {
                    "type": "string",
                    "description": "Exact substring to find and replace.",
                },
                "new": {
                    "type": "string",
                    "description": "Replacement text.",
                },
            },
            "required": ["label", "old", "new"],
        }

    async def execute(self, **kwargs: Any) -> str:
        sk = self._require_session()
        label, old, new = kwargs["label"], kwargs["old"], kwargs["new"]
        block = self._store.get(sk, label)
        if block is None:
            return f"Error: no block '{label}' exists."
        try:
            block.replace(old, new)
            self._store.save()
            return f"OK — '{label}' updated. Current value ({len(block.value)} chars):\n{block.value}"
        except ValueError as e:
            return f"Error: {e}"


class CoreMemoryAppendTool(_CoreMemoryToolBase):
    @property
    def name(self) -> str:
        return "core_memory_append"

    @property
    def description(self) -> str:
        return (
            "Append text to a core memory block. "
            "Use this to add new facts, observations, or notes."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "label": {
                    "type": "string",
                    "description": "Memory block label (e.g. 'user_facts', 'scratchpad').",
                },
                "text": {
                    "type": "string",
                    "description": "Text to append.",
                },
            },
            "required": ["label", "text"],
        }

    async def execute(self, **kwargs: Any) -> str:
        sk = self._require_session()
        label, text = kwargs["label"], kwargs["text"]
        block = self._store.get(sk, label)
        if block is None:
            return f"Error: no block '{label}' exists."
        try:
            block.append(text)
            self._store.save()
            return f"OK — appended to '{label}' ({len(block.value)} chars):\n{block.value}"
        except ValueError as e:
            return f"Error: {e}"


class CoreMemoryReadTool(_CoreMemoryToolBase):
    @property
    def name(self) -> str:
        return "core_memory_read"

    @property
    def description(self) -> str:
        return "Read all core memory blocks and their current contents."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> str:
        sk = self._require_session()
        blocks = self._store.list_blocks(sk)
        if not blocks:
            return "No core memory blocks exist for this session."
        parts = []
        for b in blocks:
            parts.append(f"## {b.label} ({len(b.value)}/{b.max_chars} chars)\n{b.value or '(empty)'}")
        return "\n\n".join(parts)
