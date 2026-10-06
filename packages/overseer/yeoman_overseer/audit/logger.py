"""JSONL audit logger with tombstone support."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class AuditEntry:
    runbook: str
    trigger: str
    action: str
    target: str
    result: str
    duration_ms: int
    escalated_to_llm: bool
    domain: str = ""
    budget_remaining: dict[str, int] | None = None
    llm_tokens_used: int | None = None
    llm_tool_calls: int | None = None
    llm_profile: str | None = None
    reasoning_summary: str | None = None


@dataclass
class TombstoneEntry:
    entry_type: str
    name: str
    action: str
    reason: str
    runbook: str
    origin: str = "manual"
    domain: str = ""


class AuditLogger:
    def __init__(self, audit_dir: Path) -> None:
        self._dir = audit_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._tombstone_path = self._dir / "tombstones.jsonl"

    def _month_log(self) -> Path:
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        return self._dir / f"{month}.jsonl"

    def append(self, entry: AuditEntry) -> dict[str, Any] | None:
        if (
            entry.trigger == "cron"
            and entry.action == "triggered"
            and entry.result == "success"
            and not entry.escalated_to_llm
            and entry.llm_tokens_used is None
            and entry.llm_tool_calls is None
        ):
            return None
        record = asdict(entry)
        record["ts"] = datetime.now(timezone.utc).isoformat()
        path = self._month_log()
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
        return record

    def read_recent(self, *, limit: int = 20, domain: str | None = None) -> list[dict[str, Any]]:
        all_entries: list[dict[str, Any]] = []
        for log_file in self._dir.glob("????-??*.jsonl"):
            for line in log_file.read_text(encoding="utf-8").splitlines():
                if line:
                    entry = json.loads(line)
                    if not domain or entry.get("domain") == domain:
                        all_entries.append(entry)
        all_entries.sort(key=lambda entry: entry.get("ts", ""), reverse=True)
        return all_entries[:limit]

    def write_tombstone(self, entry: TombstoneEntry) -> None:
        record = asdict(entry)
        record["ts"] = datetime.now(timezone.utc).isoformat()
        with self._tombstone_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

    def query_tombstones(self, *, name: str | None = None, domain: str | None = None) -> list[dict[str, Any]]:
        if not self._tombstone_path.exists():
            return []
        results: list[dict[str, Any]] = []
        for line in self._tombstone_path.read_text(encoding="utf-8").strip().splitlines():
            if not line:
                continue
            entry = json.loads(line)
            if name and entry.get("name") != name:
                continue
            if domain and entry.get("domain") and entry["domain"] != domain:
                continue
            results.append(entry)
        return results
