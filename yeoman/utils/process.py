"""Shared process management utilities for nanobot daemon processes."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path


def pid_alive(pid: int) -> bool:
    """Check if a process with the given PID is alive."""
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def command_for_pid(pid: int) -> str:
    """Get the command line of a process by PID."""
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "command="],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def process_cwd(pid: int) -> Path | None:
    """Get the current working directory of a process by PID (Linux /proc)."""
    proc_cwd = Path(f"/proc/{pid}/cwd")
    try:
        if proc_cwd.exists():
            return proc_cwd.resolve()
    except OSError:
        return None
    return None


def listener_pids_for_port(port: int) -> set[int]:
    """Find PIDs listening on the given TCP port using lsof."""
    listener_pids: set[int] = set()
    if not shutil.which("lsof"):
        return listener_pids
    result = subprocess.run(
        ["lsof", "-nP", f"-tiTCP:{port}", "-sTCP:LISTEN"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return listener_pids
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            listener_pids.add(int(line))
        except ValueError:
            continue
    return listener_pids


def signal_pid(pid: int, sig: int) -> None:
    """Send a signal to a process by PID, ignoring errors."""
    try:
        os.kill(pid, sig)
    except OSError:
        pass


def signal_process_group(pid: int, sig: int, *, fallback: bool = True) -> None:
    """Send a signal to the process group of a PID.

    Falls back to signaling the PID directly if the process group
    matches the current process group (to avoid self-signaling).
    """
    try:
        pgid = os.getpgid(pid)
    except OSError:
        pgid = None
    current_pgid = os.getpgrp()
    if pgid is not None and pgid > 0 and pgid != current_pgid:
        try:
            os.killpg(pgid, sig)
            return
        except OSError:
            pass
    if fallback:
        signal_pid(pid, sig)


def read_pid_file(path: Path) -> int | None:
    """Read an integer PID from a file, returning None on any error."""
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def is_bridge_dir(path: Path) -> bool:
    """Return True if path is a nanobot WhatsApp bridge runtime directory."""
    package_json = path / "package.json"
    if not package_json.exists():
        return False
    try:
        data = json.loads(package_json.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return data.get("name") == "nanobot-whatsapp-bridge"


def is_bridge_process(pid: int) -> bool:
    """Return True if the process appears to be the WhatsApp bridge."""
    cmd = command_for_pid(pid).lower()
    if not cmd:
        return False
    cwd = process_cwd(pid)
    if cwd and is_bridge_dir(cwd):
        return True
    return "nanobot-whatsapp-bridge" in cmd
