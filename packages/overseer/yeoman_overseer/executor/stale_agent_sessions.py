"""Find and clean stale interactive agent sessions."""
from __future__ import annotations

import asyncio
import signal
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ProcessInfo:
    pid: int
    ppid: int
    command: str
    etimes: int
    args: str


@dataclass(frozen=True, slots=True)
class CleanupResult:
    killed_pids: list[int]
    skipped_young: int
    skipped_non_agent: int


_AGENT_COMMANDS = {"claude", "codex"}


async def cleanup_stale_agent_sessions(
    *,
    min_age_seconds: int = 3600,
    dry_run: bool = False,
) -> CleanupResult:
    processes = await collect_processes()
    roots = find_stale_agent_session_roots(processes, min_age_seconds=min_age_seconds)
    skipped_young = count_young_agent_session_roots(processes, min_age_seconds=min_age_seconds)
    skipped_non_agent = count_non_agent_mosh_roots(processes)

    killed_pids: list[int] = []
    for root in roots:
        killed_pids.append(root.pid)
        if not dry_run:
            await terminate_process(root.pid)

    return CleanupResult(
        killed_pids=killed_pids,
        skipped_young=skipped_young,
        skipped_non_agent=skipped_non_agent,
    )


async def collect_processes() -> list[ProcessInfo]:
    proc = await asyncio.create_subprocess_exec(
        "ps",
        "-eo",
        "pid=,ppid=,comm=,etimes=,args=",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        detail = stderr.decode(errors="replace").strip()
        raise RuntimeError(f"ps failed: {detail}")
    return parse_process_snapshot(stdout.decode(errors="replace"))


def parse_process_snapshot(output: str) -> list[ProcessInfo]:
    processes: list[ProcessInfo] = []
    for line in output.splitlines():
        parts = line.strip().split(maxsplit=4)
        if len(parts) < 5:
            continue
        pid, ppid, command, etimes, args = parts
        try:
            processes.append(
                ProcessInfo(
                    pid=int(pid),
                    ppid=int(ppid),
                    command=command,
                    etimes=int(etimes),
                    args=args,
                )
            )
        except ValueError:
            continue
    return processes


async def terminate_process(pid: int) -> None:
    proc = await asyncio.create_subprocess_exec(
        "kill",
        f"-{signal.SIGTERM.value}",
        str(pid),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        detail = stderr.decode(errors="replace").strip()
        raise RuntimeError(f"kill {pid} failed: {detail}")


def find_stale_agent_session_roots(
    processes: list[ProcessInfo],
    *,
    min_age_seconds: int,
) -> list[ProcessInfo]:
    """Return mosh-server roots older than min_age that contain agent children."""
    children_by_parent = _children_by_parent(processes)

    stale_roots: list[ProcessInfo] = []
    for process in processes:
        if process.command != "mosh-server":
            continue
        if process.etimes < min_age_seconds:
            continue
        if _has_agent_descendant(process.pid, children_by_parent):
            stale_roots.append(process)
    return stale_roots


def count_young_agent_session_roots(
    processes: list[ProcessInfo],
    *,
    min_age_seconds: int,
) -> int:
    children_by_parent = _children_by_parent(processes)
    return sum(
        1
        for process in processes
        if process.command == "mosh-server"
        and process.etimes < min_age_seconds
        and _has_agent_descendant(process.pid, children_by_parent)
    )


def count_non_agent_mosh_roots(processes: list[ProcessInfo]) -> int:
    children_by_parent = _children_by_parent(processes)
    return sum(
        1
        for process in processes
        if process.command == "mosh-server"
        and not _has_agent_descendant(process.pid, children_by_parent)
    )


def _children_by_parent(processes: list[ProcessInfo]) -> dict[int, list[ProcessInfo]]:
    children_by_parent: dict[int, list[ProcessInfo]] = {}
    for process in processes:
        children_by_parent.setdefault(process.ppid, []).append(process)
    return children_by_parent


def _has_agent_descendant(
    root_pid: int,
    children_by_parent: dict[int, list[ProcessInfo]],
) -> bool:
    stack = list(children_by_parent.get(root_pid, []))
    while stack:
        process = stack.pop()
        if process.command in _AGENT_COMMANDS:
            return True
        stack.extend(children_by_parent.get(process.pid, []))
    return False
