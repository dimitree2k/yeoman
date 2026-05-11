"""Deterministic action executor — restart, alert, prune, rotate, noop."""
from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml

from yeoman_overseer.alerts.formatting import format_overseer_alert
from yeoman_overseer.comms.cascading import CascadingComms
from yeoman_overseer.executor.stale_agent_sessions import cleanup_stale_agent_sessions

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ActionResult:
    success: bool
    detail: str


@dataclass(frozen=True, slots=True)
class DeterministicAction:
    action: str
    target: str
    kwargs: dict[str, str]


@dataclass
class DeterministicExecutor:
    comms: CascadingComms

    async def execute(self, action: str, *, target: str, **kwargs: str) -> ActionResult:
        handler = _ACTION_REGISTRY.get(action)
        if handler is None:
            return ActionResult(success=False, detail=f"Unknown action: {action!r}")
        return await handler(self, target=target, **kwargs)

    async def _restart_service(self, *, target: str, **_: str) -> ActionResult:
        try:
            proc = await asyncio.create_subprocess_exec(
                "systemctl", "--user", "restart", target,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode == 0:
                return ActionResult(success=True, detail=f"Restarted {target}")
            return ActionResult(success=False, detail=f"Failed to restart {target}: {stderr.decode().strip()}")
        except Exception as exc:
            return ActionResult(success=False, detail=f"Error restarting {target}: {exc}")

    async def _alert(self, *, target: str, message: str = "", **_: str) -> ActionResult:
        formatted_message = format_overseer_alert(message or f"Alert for {target}")
        try:
            await self.comms.send(formatted_message)
            return ActionResult(success=True, detail=f"Alert sent: {formatted_message}")
        except Exception as exc:
            return ActionResult(success=False, detail=f"Alert failed: {exc}")

    async def _rotate_logs(self, *, target: str, **_: str) -> ActionResult:
        path = Path(target)
        if not path.exists():
            return ActionResult(success=True, detail=f"No log file at {target}")
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        rotated = path.with_name(f"{path.stem}-{ts}{path.suffix}")
        shutil.move(str(path), str(rotated))
        return ActionResult(success=True, detail=f"Rotated {target} → {rotated.name}")

    async def _prune_files(self, *, target: str, max_age_days: str = "7", **_: str) -> ActionResult:
        import time
        directory = Path(target)
        if not directory.is_dir():
            return ActionResult(success=True, detail=f"No directory at {target}")
        cutoff = time.time() - float(max_age_days) * 86400
        removed = 0
        for f in directory.iterdir():
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
                removed += 1
        return ActionResult(success=True, detail=f"Pruned {removed} files from {target}")

    async def _noop(self, **_: str) -> ActionResult:
        return ActionResult(success=True, detail="No-op executed")

    async def _cleanup_stale_agent_sessions(
        self,
        *,
        min_age_seconds: str = "3600",
        dry_run: str = "false",
        **_: str,
    ) -> ActionResult:
        try:
            min_age = int(min_age_seconds)
            is_dry_run = dry_run.lower() in {"1", "true", "yes"}
            result = await cleanup_stale_agent_sessions(
                min_age_seconds=min_age,
                dry_run=is_dry_run,
            )
        except Exception as exc:
            return ActionResult(success=False, detail=f"Stale agent session cleanup failed: {exc}")

        verb = "would kill" if is_dry_run else "killed"
        return ActionResult(
            success=True,
            detail=(
                f"{verb} {len(result.killed_pids)} stale agent session(s): {result.killed_pids}; "
                f"skipped_young={result.skipped_young}; "
                f"skipped_non_agent={result.skipped_non_agent}"
            ),
        )


def parse_deterministic_actions(body: str) -> list[DeterministicAction]:
    actions_text = _extract_actions_block(body)
    if not actions_text:
        return []
    try:
        raw = yaml.safe_load(actions_text)
    except yaml.YAMLError:
        return []
    if not isinstance(raw, list):
        return []

    actions: list[DeterministicAction] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        action = str(item.get("action") or "").strip()
        target = str(item.get("target") or "").strip()
        if not action or not target:
            continue
        kwargs = {
            str(key): str(value)
            for key, value in item.items()
            if key not in {"action", "target"} and value is not None
        }
        actions.append(DeterministicAction(action=action, target=target, kwargs=kwargs))
    return actions


def _extract_actions_block(body: str) -> str:
    lines = body.splitlines()
    start: int | None = None
    for index, line in enumerate(lines):
        if line.strip().lower() == "## actions":
            start = index + 1
            break
    if start is None:
        return ""

    block: list[str] = []
    for line in lines[start:]:
        if line.startswith("## "):
            break
        block.append(line)
    return "\n".join(block).strip()


_ACTION_REGISTRY = {
    "restart_service": DeterministicExecutor._restart_service,
    "alert": DeterministicExecutor._alert,
    "rotate_logs": DeterministicExecutor._rotate_logs,
    "prune_files": DeterministicExecutor._prune_files,
    "cleanup_stale_agent_sessions": DeterministicExecutor._cleanup_stale_agent_sessions,
    "noop": DeterministicExecutor._noop,
}
