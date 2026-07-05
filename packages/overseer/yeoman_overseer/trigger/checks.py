"""Built-in deterministic check functions for trigger conditions."""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
import uuid
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


def check_systemd_active(*, target: str) -> CheckResult:
    """Check a local user systemd unit without touching the managed service."""
    proc = subprocess.run(
        ["systemctl", "--user", "is-active", "--quiet", target],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode == 0:
        return CheckResult(value=True, detail=f"{target} is active")
    detail = proc.stderr.strip() or f"{target} is inactive (systemctl rc={proc.returncode})"
    return CheckResult(value=False, detail=detail)


def _whatsapp_bridge_health(target: str, timeout_s: float) -> dict[str, Any]:
    from websockets.sync.client import connect
    from yeoman_shared.config.loader import load_config

    config = load_config()
    wa = config.channels.whatsapp
    token = (wa.bridge_token or "").strip()
    if not token:
        raise RuntimeError("channels.whatsapp.bridgeToken is required for bridge health")

    request_id = uuid.uuid4().hex
    envelope = {
        "version": 2,
        "type": "health",
        "token": token,
        "requestId": request_id,
        "accountId": target or "default",
        "payload": {},
    }
    with connect(
        wa.resolved_bridge_url,
        open_timeout=timeout_s,
        max_size=wa.max_payload_bytes,
    ) as ws:
        ws.send(json.dumps(envelope))
        deadline = time.monotonic() + timeout_s
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError("Bridge health check timed out")
            raw = ws.recv(timeout=left)
            data = json.loads(raw)
            if not isinstance(data, dict):
                continue
            if data.get("type") != "response":
                continue
            if data.get("requestId") != request_id:
                continue
            payload = data.get("payload")
            if not isinstance(payload, dict):
                raise RuntimeError("Bridge health payload malformed")
            if not payload.get("ok"):
                raise RuntimeError(f"Bridge health returned error: {payload.get('error')}")
            result = payload.get("result")
            if not isinstance(result, dict):
                raise RuntimeError("Bridge health result malformed")
            return result


def check_whatsapp_bridge_connected(*, target: str) -> CheckResult:
    """Check whether the bridge process is connected through to WhatsApp."""
    try:
        health = _whatsapp_bridge_health(target=target, timeout_s=3.0)
    except Exception as exc:
        return CheckResult(value=False, detail=f"Bridge health check failed: {exc}")

    whatsapp = health.get("whatsapp")
    if not isinstance(whatsapp, dict):
        return CheckResult(value=False, detail="Bridge health missing whatsapp status")

    connected = bool(whatsapp.get("connected"))
    running = bool(whatsapp.get("running"))
    reconnect_attempts = whatsapp.get("reconnectAttempts")
    last_disconnect_status = whatsapp.get("lastDisconnectStatus")
    last_error = whatsapp.get("lastError")
    parts = [
        f"connected={connected}",
        f"running={running}",
        f"reconnectAttempts={reconnect_attempts}",
    ]
    if last_disconnect_status is not None:
        parts.append(f"lastDisconnectStatus={last_disconnect_status}")
    if last_error:
        parts.append(f"lastError={last_error}")
    return CheckResult(value=connected, detail=", ".join(parts))


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
    "systemd_active": check_systemd_active,
    "whatsapp_bridge_connected": check_whatsapp_bridge_connected,
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
