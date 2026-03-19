"""Lock manager for runbook execution — prevents concurrent mutations."""
from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class LockEntry:
    """A held lock."""
    holder: str
    exclusive: bool
    acquired_at: float


@dataclass
class LockManager:
    """Manages exclusive/shared locks on resources and a global LLM lock."""
    lock_timeout_s: float = 300.0
    _locks: dict[str, list[LockEntry]] = field(default_factory=dict)
    _llm_holder: str | None = None
    _llm_acquired_at: float = 0.0

    def _prune_expired(self, resource: str) -> None:
        now = time.monotonic()
        if resource in self._locks:
            self._locks[resource] = [
                e for e in self._locks[resource]
                if now - e.acquired_at < self.lock_timeout_s
            ]
            if not self._locks[resource]:
                del self._locks[resource]

    def acquire(self, resource: str, holder: str, *, exclusive: bool) -> bool:
        self._prune_expired(resource)
        entries = self._locks.get(resource, [])
        if exclusive:
            if entries:
                return False
        else:
            if any(e.exclusive for e in entries):
                return False
        entry = LockEntry(holder=holder, exclusive=exclusive, acquired_at=time.monotonic())
        self._locks.setdefault(resource, []).append(entry)
        return True

    def release(self, resource: str, holder: str) -> None:
        if resource not in self._locks:
            return
        self._locks[resource] = [e for e in self._locks[resource] if e.holder != holder]
        if not self._locks[resource]:
            del self._locks[resource]

    def is_locked(self, resource: str) -> bool:
        self._prune_expired(resource)
        return resource in self._locks

    def acquire_llm(self, holder: str) -> bool:
        now = time.monotonic()
        if self._llm_holder is not None:
            if now - self._llm_acquired_at < self.lock_timeout_s:
                return False
        self._llm_holder = holder
        self._llm_acquired_at = now
        return True

    def release_llm(self, holder: str) -> None:
        if self._llm_holder == holder:
            self._llm_holder = None
