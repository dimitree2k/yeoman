"""Built-in deterministic check functions for trigger conditions."""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from yeoman_shared.whatsapp_protocol import PROTOCOL_VERSION


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


def check_gateway_healthy(*, target: str) -> CheckResult:
    """Require both an active user unit and a responsive gateway event loop."""
    active = check_systemd_active(target=target)
    if not active.value:
        return active
    from yeoman_shared.utils.helpers import get_yeoman_home

    home = get_yeoman_home()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(5.0)
            client.connect(str(home / "run" / "gateway.sock"))
            client.sendall(b'{"cmd":"ping","args":{}}\n')
            with client.makefile("rb") as stream:
                response = json.loads(stream.readline(4096))
        healthy = isinstance(response, dict) and response.get("status") == "ok" and response.get("response") == "pong"
        return CheckResult(value=healthy, detail="Gateway pong" if healthy else "Invalid gateway ping response")
    except (OSError, ValueError) as exc:
        return CheckResult(value=False, detail=f"Gateway ping failed: {exc}")


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
        "version": PROTOCOL_VERSION,
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
            if data.get("version") != PROTOCOL_VERSION:
                raise RuntimeError(
                    "Bridge health protocol mismatch: "
                    f"expected {PROTOCOL_VERSION}, got {data.get('version')!r}"
                )
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


def check_whatsapp_bridge_reachable(*, target: str) -> CheckResult:
    """Whether the bridge process answers at all.

    WhatsApp reconnects belong to the bridge and its persisted budget; the Overseer
    restarts the bridge only when the process is dead or hung, never because it is
    disconnected from WhatsApp.
    """
    try:
        _whatsapp_bridge_health(target=target, timeout_s=5.0)
    except Exception as exc:
        return CheckResult(value=False, detail=f"Bridge health check failed: {exc}")
    return CheckResult(value=True, detail="bridge answers")


def check_whatsapp_link_held(*, target: str) -> CheckResult:
    """True while the bridge holds off reconnecting (needs a human, or budget spent)."""
    try:
        health = _whatsapp_bridge_health(target=target, timeout_s=3.0)
    except Exception as exc:
        return CheckResult(value=False, detail=f"Bridge health check failed: {exc}")
    whatsapp = health.get("whatsapp")
    hold = whatsapp.get("hold") if isinstance(whatsapp, dict) else None
    if not isinstance(hold, dict) or whatsapp.get("connected"):
        return CheckResult(value=False, detail="no hold")
    kind = hold.get("kind")
    code = hold.get("code")
    if kind == "needs_human" and code == 440:
        detail = (
            "WhatsApp keeps handing the session to another client (440 twice in 10 min). "
            "No more attempts will be made. Close the other WhatsApp Web/desktop session, "
            "then run `yeoman overseer resume bridge`."
        )
    elif kind == "needs_human":
        detail = (
            f"WhatsApp needs you: the link closed with {code} ({hold.get('reason') or 'unknown'}). "
            "No more login attempts will be made. Relink with `yeoman channels login`."
        )
    else:
        detail = (
            "WhatsApp reconnects used up today's budget. Only one try every 8 h from now; "
            "`yeoman overseer resume bridge` tries now."
        )
    return CheckResult(value=True, detail=detail)


# First time each condition was seen, so a check can require it to last (process-local).
_FIRST_SEEN: dict[str, float] = {}


def _sustained(key: str, active: bool, *, for_s: float) -> float:
    """Seconds the condition has held continuously, or 0.0 when it is not active."""
    if not active:
        _FIRST_SEEN.pop(key, None)
        return 0.0
    first = _FIRST_SEEN.setdefault(key, time.time())
    return time.time() - first


def _unit_active(unit: str) -> bool:
    import subprocess

    try:
        proc = subprocess.run(
            ["systemctl", "--user", "is-active", "--quiet", unit],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def check_whatsapp_gateway_detached(*, target: str, threshold: float = 600.0) -> CheckResult:
    """True once the bridge has had no gateway client for ``threshold`` seconds.

    Only counts while both the gateway and the bridge are desired running; the health
    request itself is one client, so a gateway makes two.
    """
    from yeoman_shared.desired_state import DesiredStateStore

    store = DesiredStateStore()
    if store.is_held("yeoman-bridge") or store.is_held("yeoman-gateway"):
        _sustained("gateway_detached", False, for_s=threshold)
        return CheckResult(value=False, detail="bridge or gateway paused by owner")
    try:
        health = _whatsapp_bridge_health(target=target, timeout_s=3.0)
    except Exception as exc:
        _sustained("gateway_detached", False, for_s=threshold)
        return CheckResult(value=False, detail=f"bridge unreachable ({exc}); health-bridge covers it")
    queue = health.get("queue") if isinstance(health.get("queue"), dict) else {}
    detached = int(queue.get("clients", 0) or 0) < 2
    held_s = _sustained("gateway_detached", detached, for_s=threshold)
    if held_s >= threshold:
        return CheckResult(
            value=True,
            detail=(
                f"The gateway has not been connected to the WhatsApp bridge for {held_s / 60:.0f} min. "
                "Messages are not flowing. Run `yeoman gateway restart`."
            ),
        )
    return CheckResult(value=False, detail="gateway attached" if not detached else f"detached {held_s:.0f}s")


MANAGED_UNITS = ("yeoman-gateway", "yeoman-bridge")


def check_desired_state_drift(*, target: str, threshold: float = 60.0) -> CheckResult:
    """True when a unit the owner paused or stopped has been running for ``threshold`` s."""
    from yeoman_shared.desired_state import DesiredStateStore

    store = DesiredStateStore()
    drifted = [u for u in MANAGED_UNITS if store.is_held(u) and _unit_active(f"{u}.service")]
    held_s = _sustained("desired_drift:" + ",".join(drifted), bool(drifted), for_s=threshold)
    if drifted and held_s >= threshold:
        names = ", ".join(drifted)
        short = drifted[0].removeprefix("yeoman-")
        return CheckResult(
            value=True,
            detail=(
                f"{names} is running although it is marked paused. I am leaving it alone. "
                f"Run `yeoman overseer resume {short}` to hand it back to me, or "
                f"`yeoman overseer pause {short}` to stop it again."
            ),
        )
    return CheckResult(value=False, detail="no drift" if not drifted else f"drift {held_s:.0f}s")


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
    "gateway_healthy": check_gateway_healthy,
    "process_alive": check_process_alive,
    "systemd_active": check_systemd_active,
    "whatsapp_bridge_connected": check_whatsapp_bridge_connected,
    "whatsapp_bridge_reachable": check_whatsapp_bridge_reachable,
    "whatsapp_link_held": check_whatsapp_link_held,
    "whatsapp_gateway_detached": check_whatsapp_gateway_detached,
    "desired_state_drift": check_desired_state_drift,
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
