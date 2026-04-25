"""Persistent approval storage for proactive speakup previews."""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from loguru import logger

SpeakupApprovalAction = Literal["approve", "deny"]


@dataclass(slots=True)
class PendingSpeakupApproval:
    """A pending owner approval for one proactive speakup proposal."""

    proposal_id: str
    target_channel: str
    target_chat_id: str
    owner_channel: str
    owner_chat_id: str
    message: str
    action_type: str
    profile: str
    created_at: float
    expires_at: float
    context_snapshot: dict[str, object]

    @property
    def approve_code(self) -> str:
        return f"spk-approve-{self.proposal_id}"

    @property
    def deny_code(self) -> str:
        return f"spk-deny-{self.proposal_id}"


class SpeakupApprovalStore:
    """Manages pending speakup approvals with atomic JSON persistence."""

    def __init__(self, store_path: Path) -> None:
        self._path = store_path
        self._lock = asyncio.Lock()
        self._approvals: list[PendingSpeakupApproval] = []
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text())
            self._approvals = [
                PendingSpeakupApproval(**item) for item in data.get("approvals", [])
            ]
        except Exception as exc:
            logger.warning("Failed to load speakup approval state: {}", exc)
            self._approvals = []

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = {"approvals": [asdict(approval) for approval in self._approvals]}
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

    @staticmethod
    def _parse_code(text: str) -> tuple[SpeakupApprovalAction, str] | None:
        stripped = str(text or "").strip()
        if stripped.startswith("spk-approve-"):
            proposal_id = stripped.removeprefix("spk-approve-").strip()
            return ("approve", proposal_id) if proposal_id else None
        if stripped.startswith("spk-deny-"):
            proposal_id = stripped.removeprefix("spk-deny-").strip()
            return ("deny", proposal_id) if proposal_id else None
        return None

    async def add(self, approval: PendingSpeakupApproval) -> None:
        async with self._lock:
            self._approvals = [
                item for item in self._approvals if item.proposal_id != approval.proposal_id
            ]
            self._approvals.append(approval)
            self._save()

    async def match_and_consume(
        self, text: str
    ) -> tuple[SpeakupApprovalAction, PendingSpeakupApproval] | None:
        parsed = self._parse_code(text)
        if parsed is None:
            return None
        action, proposal_id = parsed
        async with self._lock:
            now = time.time()
            for index, approval in enumerate(self._approvals):
                if approval.proposal_id == proposal_id:
                    consumed = self._approvals.pop(index)
                    self._save()
                    if consumed.expires_at < now:
                        return None
                    return action, consumed
            return None

    async def purge_expired(self) -> list[PendingSpeakupApproval]:
        async with self._lock:
            now = time.time()
            expired = [approval for approval in self._approvals if approval.expires_at < now]
            if expired:
                self._approvals = [
                    approval for approval in self._approvals if approval.expires_at >= now
                ]
                self._save()
            return expired

    async def list_pending(self) -> list[PendingSpeakupApproval]:
        async with self._lock:
            return list(self._approvals)

