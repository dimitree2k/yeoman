"""Audit log + backup support for policy admin mutations."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from yeoman.policy.loader import load_policy
from yeoman.policy.schema import PolicyConfig


@dataclass(frozen=True, slots=True)
class PolicyAuditEntry:
    id: str
    timestamp: str
    actor_source: str
    actor_id: str
    channel: str
    chat_id: str
    command_raw: str
    dry_run: bool
    result: str
    before_hash: str | None
    after_hash: str | None
    backup_ref: str | None
    error: str | None = None


class PolicyAuditStore:
    """Stores append-only audit rows and policy backup snapshots."""

    def __init__(self, policy_path: Path) -> None:
        self._policy_path = policy_path
        self._root = policy_path.parent / "policy" / "audit"
        self._history_path = self._root / "policy_changes.jsonl"
        self._backup_dir = self._root / "backups"

    @property
    def history_path(self) -> Path:
        return self._history_path

    def ensure_dirs(self) -> None:
        self._backup_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def policy_hash(policy: PolicyConfig) -> str:
        payload = json.dumps(
            policy.model_dump(by_alias=True, exclude_none=True),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def write_backup(self, change_id: str, before_policy: PolicyConfig) -> str:
        self.ensure_dirs()
        rel = f"backups/{change_id}.json"
        path = self._root / rel
        with open(path, "w", encoding="utf-8") as f:
            json.dump(before_policy.model_dump(by_alias=True, exclude_none=True), f, indent=2, ensure_ascii=False)
        return rel

    def load_backup(self, backup_ref: str) -> PolicyConfig:
        path = self._root / backup_ref
        return load_policy(path)

    def append(self, entry: PolicyAuditEntry) -> None:
        self.ensure_dirs()
        row = {
            "id": entry.id,
            "timestamp": entry.timestamp,
            "actor_source": entry.actor_source,
            "actor_id": entry.actor_id,
            "channel": entry.channel,
            "chat_id": entry.chat_id,
            "command_raw": entry.command_raw,
            "dry_run": entry.dry_run,
            "result": entry.result,
            "before_hash": entry.before_hash,
            "after_hash": entry.after_hash,
            "backup_ref": entry.backup_ref,
            "error": entry.error,
        }
        with open(self._history_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")

    def read_recent(self, limit: int) -> list[PolicyAuditEntry]:
        if limit <= 0:
            return []
        if not self._history_path.exists():
            return []
        rows: list[PolicyAuditEntry] = []
        with open(self._history_path, encoding="utf-8", errors="ignore") as f:
            for line in f:
                compact = line.strip()
                if not compact:
                    continue
                try:
                    data = json.loads(compact)
                except json.JSONDecodeError:
                    continue
                rows.append(
                    PolicyAuditEntry(
                        id=str(data.get("id", "")),
                        timestamp=str(data.get("timestamp", "")),
                        actor_source=str(data.get("actor_source", "")),
                        actor_id=str(data.get("actor_id", "")),
                        channel=str(data.get("channel", "")),
                        chat_id=str(data.get("chat_id", "")),
                        command_raw=str(data.get("command_raw", "")),
                        dry_run=bool(data.get("dry_run", False)),
                        result=str(data.get("result", "")),
                        before_hash=self._none_if_blank(data.get("before_hash")),
                        after_hash=self._none_if_blank(data.get("after_hash")),
                        backup_ref=self._none_if_blank(data.get("backup_ref")),
                        error=self._none_if_blank(data.get("error")),
                    )
                )
        if not rows:
            return []
        return rows[-limit:][::-1]

    def find(self, change_id: str) -> PolicyAuditEntry | None:
        if not self._history_path.exists():
            return None
        with open(self._history_path, encoding="utf-8", errors="ignore") as f:
            for line in f:
                compact = line.strip()
                if not compact:
                    continue
                try:
                    data = json.loads(compact)
                except json.JSONDecodeError:
                    continue
                if str(data.get("id", "")).strip() != change_id:
                    continue
                return PolicyAuditEntry(
                    id=str(data.get("id", "")),
                    timestamp=str(data.get("timestamp", "")),
                    actor_source=str(data.get("actor_source", "")),
                    actor_id=str(data.get("actor_id", "")),
                    channel=str(data.get("channel", "")),
                    chat_id=str(data.get("chat_id", "")),
                    command_raw=str(data.get("command_raw", "")),
                    dry_run=bool(data.get("dry_run", False)),
                    result=str(data.get("result", "")),
                    before_hash=self._none_if_blank(data.get("before_hash")),
                    after_hash=self._none_if_blank(data.get("after_hash")),
                    backup_ref=self._none_if_blank(data.get("backup_ref")),
                    error=self._none_if_blank(data.get("error")),
                )
        return None

    @staticmethod
    def now_iso() -> str:
        return datetime.now(UTC).isoformat()

    @staticmethod
    def _none_if_blank(value: object) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None
