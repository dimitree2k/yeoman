"""Built-in deterministic check functions for trigger conditions."""
from __future__ import annotations

import os
import re
import shutil
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class CheckResult:
    """Result of a check function."""

    value: Any
    detail: str = ""


def _parse_duration(s: str) -> float:
    """Parse duration string like '5m', '1h', '30s' to seconds."""
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([smhd])", s.strip())
    if not match:
        raise ValueError(f"Invalid duration: {s!r}")
    amount = float(match.group(1))
    unit = match.group(2)
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    return amount * multipliers[unit]


def check_process_alive(*, target: str) -> CheckResult:
    """Check if a process is alive. Target is a PID or a path to a PID file."""
    path = Path(target)
    if path.is_file():
        try:
            pid = int(path.read_text().strip())
        except (ValueError, OSError):
            return CheckResult(value=False, detail=f"Cannot read PID from {target}")
    else:
        try:
            pid = int(target)
        except ValueError:
            return CheckResult(value=False, detail=f"Invalid PID target: {target}")

    try:
        os.kill(pid, 0)
        return CheckResult(value=True, detail=f"PID {pid} is alive")
    except ProcessLookupError:
        return CheckResult(value=False, detail=f"PID {pid} not found")
    except PermissionError:
        return CheckResult(value=True, detail=f"PID {pid} exists (no permission to signal)")


def check_file_age_exceeds(*, target: str, threshold: str) -> CheckResult:
    """Check if a file is older than threshold. Missing file = True (infinitely old)."""
    path = Path(target)
    if not path.exists():
        return CheckResult(value=True, detail=f"File {target} does not exist")

    max_age_s = _parse_duration(threshold)
    age_s = time.time() - path.stat().st_mtime
    exceeded = age_s > max_age_s
    return CheckResult(
        value=exceeded,
        detail=f"File age {age_s:.0f}s, threshold {max_age_s:.0f}s",
    )


def check_disk_usage_above(*, target: str, threshold: float) -> CheckResult:
    """Check if disk usage on a partition exceeds a percentage threshold."""
    usage = shutil.disk_usage(target)
    pct = (usage.used / usage.total) * 100
    exceeded = pct > threshold
    return CheckResult(
        value=exceeded,
        detail=f"Disk usage {pct:.1f}%, threshold {threshold}%",
    )


def check_row_count_exceeds(
    *, target: str, query: str, threshold: int
) -> CheckResult:
    """Run a COUNT query on a SQLite DB and check if result exceeds threshold."""
    conn = sqlite3.connect(target)
    try:
        row = conn.execute(query).fetchone()
        count = row[0] if row else 0
    finally:
        conn.close()
    exceeded = count > threshold
    return CheckResult(value=exceeded, detail=f"Row count {count}, threshold {threshold}")


_CHECK_REGISTRY: dict[str, Any] = {
    "process_alive": check_process_alive,
    "file_age_exceeds": check_file_age_exceeds,
    "disk_usage_above": check_disk_usage_above,
    "row_count_exceeds": check_row_count_exceeds,
}


def run_check(name: str, **kwargs: Any) -> CheckResult:
    """Dispatch a check by name."""
    fn = _CHECK_REGISTRY.get(name)
    if fn is None:
        raise ValueError(f"Unknown check: {name!r}. Available: {list(_CHECK_REGISTRY)}")
    return fn(**kwargs)
