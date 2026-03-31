"""Persistent state for workflow approval gates."""
from __future__ import annotations

import asyncio
import json
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from loguru import logger


@dataclass
class PendingApproval:
    """A pending workflow approval gate."""

    approval_id: str
    next_job_id: str
    previous_output: str
    channel: str
    chat_id: str
    created_at: float
    expires_at: float
    workflow_id: str | None
    remaining_depth: int


class WorkflowState:
    """Manages pending workflow approvals with atomic JSON persistence."""

    def __init__(self, store_path: Path) -> None:
        self._path = store_path
        self._lock = asyncio.Lock()
        self._approvals: list[PendingApproval] = []
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text())
            self._approvals = [PendingApproval(**item) for item in data.get("approvals", [])]
        except Exception as e:
            logger.warning("Failed to load workflow state: {}", e)
            self._approvals = []

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = {"approvals": [asdict(a) for a in self._approvals]}
        # Atomic write: temp file + rename
        tmp_fd = tempfile.NamedTemporaryFile(
            mode="w", dir=self._path.parent, suffix=".tmp", delete=False
        )
        try:
            json.dump(data, tmp_fd, indent=2)
            tmp_fd.close()
            Path(tmp_fd.name).rename(self._path)
        except Exception:
            Path(tmp_fd.name).unlink(missing_ok=True)
            raise

    async def add(self, approval: PendingApproval) -> None:
        async with self._lock:
            self._approvals.append(approval)
            self._save()

    async def match_and_consume(self, text: str) -> PendingApproval | None:
        async with self._lock:
            for i, a in enumerate(self._approvals):
                if a.approval_id == text:
                    consumed = self._approvals.pop(i)
                    self._save()
                    return consumed
            return None

    async def purge_expired(self) -> list[PendingApproval]:
        async with self._lock:
            now = time.time()
            expired = [a for a in self._approvals if a.expires_at < now]
            if expired:
                self._approvals = [a for a in self._approvals if a.expires_at >= now]
                self._save()
            return expired

    async def list_pending(self) -> list[PendingApproval]:
        async with self._lock:
            return list(self._approvals)
