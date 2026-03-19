"""Deterministic action executor — restart, alert, prune, rotate, noop."""
from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from yeoman_overseer.comms.cascading import CascadingComms

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ActionResult:
    success: bool
    detail: str


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
        try:
            await self.comms.send(message or f"Alert for {target}")
            return ActionResult(success=True, detail=f"Alert sent: {message}")
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


_ACTION_REGISTRY = {
    "restart_service": DeterministicExecutor._restart_service,
    "alert": DeterministicExecutor._alert,
    "rotate_logs": DeterministicExecutor._rotate_logs,
    "prune_files": DeterministicExecutor._prune_files,
    "noop": DeterministicExecutor._noop,
}
