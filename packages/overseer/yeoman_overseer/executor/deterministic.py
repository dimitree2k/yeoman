"""Deterministic action executor — restart, alert, prune, rotate, noop."""
from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml
from yeoman_shared.raw_archive.paths import (
    ProtectedPathError,
    assert_deletable,
    assert_deletable_tree,
)

from yeoman_overseer.alerts.formatting import format_overseer_alert
from yeoman_overseer.comms.cascading import CascadingComms
from yeoman_overseer.executor.agent_cli_update import update_agent_clis
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
                await asyncio.sleep(2.0)
                active_proc = await asyncio.create_subprocess_exec(
                    "systemctl", "--user", "is-active", "--quiet", target,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                active_stdout, active_stderr = await active_proc.communicate()
                if active_proc.returncode == 0:
                    return ActionResult(success=True, detail=f"Restarted {target}")
                detail = (
                    active_stderr.decode().strip()
                    or active_stdout.decode().strip()
                    or f"systemctl is-active exited {active_proc.returncode}"
                )
                return ActionResult(
                    success=False,
                    detail=f"Restarted {target}, but service is not active: {detail}",
                )
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
        path = Path(target).expanduser()
        if not path.exists():
            return ActionResult(success=True, detail=f"No log file at {target}")
        try:
            assert_deletable_tree(path)
        except ProtectedPathError as exc:
            logger.error("rotate_logs refused: %s", exc)
            return ActionResult(success=False, detail=str(exc))
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        rotated = path.with_name(f"{path.stem}-{ts}{path.suffix}")
        shutil.move(str(path), str(rotated))
        return ActionResult(success=True, detail=f"Rotated {target} → {rotated.name}")

    async def _prune_files(self, *, target: str, max_age_days: str = "7", **_: str) -> ActionResult:
        import time
        directory = Path(target).expanduser()
        if not directory.is_dir():
            return ActionResult(success=True, detail=f"No directory at {target}")
        cutoff = time.time() - float(max_age_days) * 86400
        removed = 0
        refused = 0
        for f in directory.iterdir():
            if f.is_file() and f.stat().st_mtime < cutoff:
                try:
                    assert_deletable(f)
                except ProtectedPathError:
                    refused += 1
                    logger.error("prune_files refused protected raw archive file %s", f)
                    continue
                f.unlink()
                removed += 1
        if refused:
            return ActionResult(
                success=False,
                detail=(
                    f"Pruned {removed} files from {target}; "
                    f"refused {refused} protected raw archive file(s)"
                ),
            )
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

    async def _update_agent_clis(self, *, target: str, **_: str) -> ActionResult:
        names = tuple(name.strip() for name in target.split(",") if name.strip())
        if not names:
            return ActionResult(success=False, detail="update_agent_clis needs a CLI name target")
        try:
            results = await update_agent_clis(names)
        except Exception as exc:
            detail = f"Agent CLI update failed: {exc}"
        else:
            detail = "; ".join(result.describe() for result in results)
            if all(result.success for result in results):
                return ActionResult(success=True, detail=detail)
        await self._alert(target="agent-clis", message=f"Agent CLI update problem: {detail}")
        return ActionResult(success=False, detail=detail)


    async def _log_digest(
        self,
        *,
        target: str,
        processing_db: str = "~/.yeoman/data/ops/processing.db",
        out_dir: str = "~/.yeoman/data/ops/log-review",
        **_: str,
    ) -> ActionResult:
        from yeoman_overseer.executor.log_digest import build_log_digest

        try:
            result = await asyncio.to_thread(
                build_log_digest,
                log_dir=Path(target).expanduser(),
                processing_db=Path(processing_db).expanduser(),
                out_dir=Path(out_dir).expanduser(),
            )
        except Exception as exc:
            message = f"Log digest failed: {type(exc).__name__}: {exc}"
            await self._alert(target="log-digest", message=message)
            return ActionResult(success=False, detail=message)
        return ActionResult(
            success=True,
            detail=(
                f"log digest {result.path.name}: {result.lines_read} lines, "
                f"{result.incidents} incidents ({result.new_incidents} new)"
            ),
        )

    async def _log_review(self, *, target: str, **_: str) -> ActionResult:
        """Start the headless log review as a transient systemd unit.

        The run can deploy, and a deploy restarts this service; a child process
        would die with it, a separate transient unit keeps running.
        """
        script = Path(target).expanduser()
        if not script.is_file():
            return ActionResult(success=False, detail=f"Log review script missing: {script}")
        unit = f"yeoman-log-review-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        proc = await asyncio.create_subprocess_exec(
            "systemd-run", "--user", "--collect", f"--unit={unit}",
            "--property=TimeoutStartSec=2h", "--nice=10", str(script),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            message = f"Log review did not start: {stderr.decode(errors='replace').strip()[:300]}"
            await self._alert(target="log-review", message=message)
            return ActionResult(success=False, detail=message)
        return ActionResult(success=True, detail=f"Log review started as {unit}")

    async def _verify_raw_archive(self, *, target: str, **_: str) -> ActionResult:
        from yeoman_shared.raw_archive.verify import verify_archive

        root = None if target in {"", "default"} else Path(target).expanduser()
        report = await asyncio.to_thread(verify_archive, root)
        if report.ok:
            return ActionResult(
                success=True,
                detail=(
                    f"raw archive ok: {report.files_checked} files, "
                    f"{report.lines_total} lines, closed {len(report.closed)}"
                ),
            )
        message = "Raw archive integrity problem: " + "; ".join(report.problems[:5])
        await self._alert(target="raw-archive", message=message)
        return ActionResult(success=False, detail=message)


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
    "verify_raw_archive": DeterministicExecutor._verify_raw_archive,
    "update_agent_clis": DeterministicExecutor._update_agent_clis,
    "log_digest": DeterministicExecutor._log_digest,
    "log_review": DeterministicExecutor._log_review,
    "noop": DeterministicExecutor._noop,
}
