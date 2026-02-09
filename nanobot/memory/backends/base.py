"""Backend protocol for long-term memory storage."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from nanobot.memory.models import MemoryEntry, MemoryHit, MemoryKind


class MemoryBackend(Protocol):
    """Storage backend contract for long-term memory."""

    def upsert_entry(self, entry: MemoryEntry) -> tuple[MemoryEntry, bool]:
        """Insert or merge one memory entry. Returns (entry, inserted_new)."""

    def search(
        self,
        *,
        workspace_id: str,
        query: str,
        scope_keys: list[str],
        kinds: set[MemoryKind] | None = None,
        limit: int = 8,
    ) -> list[MemoryHit]:
        """Search relevant memories by query and scope."""

    def list_entries(
        self,
        *,
        workspace_id: str,
        scope_keys: list[str] | None = None,
        kinds: set[MemoryKind] | None = None,
        limit: int = 100,
        include_deleted: bool = False,
    ) -> list[MemoryEntry]:
        """List entries by filters."""

    def soft_delete(self, *, workspace_id: str, entry_id: str) -> bool:
        """Soft-delete one memory entry."""

    def prune(
        self,
        *,
        workspace_id: str,
        older_than: datetime | None = None,
        kinds: set[MemoryKind] | None = None,
        scope_keys: list[str] | None = None,
        dry_run: bool = False,
    ) -> int:
        """Prune entries by age and optional filters."""

    def prune_expired(self, *, workspace_id: str, dry_run: bool = False) -> int:
        """Prune entries where expires_at is in the past."""

    def stats(self, *, workspace_id: str) -> dict[str, Any]:
        """Return backend memory stats."""

    def reindex(self) -> None:
        """Rebuild full-text index."""

    def get_meta(self, key: str) -> str | None:
        """Get metadata value."""

    def set_meta(self, key: str, value: str) -> None:
        """Set metadata value."""
