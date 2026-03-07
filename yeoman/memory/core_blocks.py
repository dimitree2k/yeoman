"""Core memory blocks — mutable context the agent can self-edit."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class CoreMemoryBlock:
    """A labeled, mutable text buffer pinned into the system prompt."""

    label: str
    value: str = ""
    max_chars: int = 2000

    def replace(self, old: str, new: str) -> None:
        if old not in self.value:
            raise ValueError(f"'{old}' not found in block '{self.label}'")
        updated = self.value.replace(old, new, 1)
        if len(updated) > self.max_chars:
            raise ValueError(f"Replace would exceed {self.max_chars} char limit")
        self.value = updated

    def append(self, text: str) -> None:
        if len(self.value) + len(text) > self.max_chars:
            raise ValueError(
                f"Append would exceed {self.max_chars} char limit "
                f"({len(self.value)} + {len(text)} > {self.max_chars})"
            )
        self.value += text


class CoreMemoryBlockStore:
    """Persist core memory blocks per session key as JSON."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._data: dict[str, dict[str, CoreMemoryBlock]] = {}
        if self._path.exists():
            self._load()

    def get(self, session_key: str, label: str) -> CoreMemoryBlock | None:
        return self._data.get(session_key, {}).get(label)

    def set(self, session_key: str, block: CoreMemoryBlock) -> None:
        self._data.setdefault(session_key, {})[block.label] = block

    def list_blocks(self, session_key: str) -> list[CoreMemoryBlock]:
        return list(self._data.get(session_key, {}).values())

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        serialized: dict[str, dict[str, dict]] = {}
        for sk, blocks in self._data.items():
            serialized[sk] = {
                label: {"label": b.label, "value": b.value, "max_chars": b.max_chars}
                for label, b in blocks.items()
            }
        self._path.write_text(json.dumps(serialized, indent=2))

    def _load(self) -> None:
        raw = json.loads(self._path.read_text())
        for sk, blocks in raw.items():
            self._data[sk] = {
                label: CoreMemoryBlock(**b) for label, b in blocks.items()
            }
