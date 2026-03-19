"""Causal chain detection — direct triggers + state-mediated cycles."""
from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field


@dataclass
class ActionRecord:
    runbook: str
    triggered_by: str | None
    state_changed: list[str]
    state_read: list[str]
    ts: float


@dataclass
class CausalChainDetector:
    max_depth: int = 3
    window_s: float = 600.0

    _actions: list[ActionRecord] = field(default_factory=list)

    def record_action(
        self, runbook: str, *, triggered_by: str | None,
        state_changed: list[str], state_read: list[str],
    ) -> None:
        self._prune_window()
        self._actions.append(ActionRecord(
            runbook=runbook, triggered_by=triggered_by,
            state_changed=state_changed, state_read=state_read,
            ts=time.monotonic(),
        ))

    def _prune_window(self) -> None:
        cutoff = time.monotonic() - self.window_s
        self._actions = [a for a in self._actions if a.ts > cutoff]

    def detect_cycle(self) -> list[str] | None:
        graph: dict[str, set[str]] = defaultdict(set)
        for action in self._actions:
            if action.triggered_by:
                graph[action.triggered_by].add(action.runbook)
        writers: dict[str, set[str]] = defaultdict(set)
        readers: dict[str, set[str]] = defaultdict(set)
        for action in self._actions:
            for f in action.state_changed:
                writers[f].add(action.runbook)
            for f in action.state_read:
                readers[f].add(action.runbook)
        for file_key in set(writers) & set(readers):
            for writer in writers[file_key]:
                for reader in readers[file_key]:
                    if writer != reader:
                        graph[writer].add(reader)
        for start in graph:
            visited: set[str] = set()
            stack = [(start, 0)]
            while stack:
                node, depth = stack.pop()
                if depth > self.max_depth:
                    return self._extract_chain(graph, start, self.max_depth)
                if node in visited and node == start and depth > 0:
                    return self._extract_chain(graph, start, depth)
                visited.add(node)
                for neighbor in graph.get(node, set()):
                    stack.append((neighbor, depth + 1))
        return None

    def _extract_chain(self, graph: dict[str, set[str]], start: str, max_depth: int) -> list[str]:
        chain = [start]
        current = start
        for _ in range(max_depth):
            neighbors = graph.get(current, set())
            if not neighbors:
                break
            nxt = next(iter(neighbors))
            if nxt in chain:
                break
            chain.append(nxt)
            current = nxt
        return chain

    def clear(self) -> None:
        self._actions.clear()
