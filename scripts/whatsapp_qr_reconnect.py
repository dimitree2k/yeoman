#!/usr/bin/env python3
"""Reconnect Yeoman WhatsApp bridge with a local auto-refreshing QR SVG.

This script is intentionally repository-bound so it is available from any
checkout on the Yeoman host, including remote sessions into moltypython.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

try:
    import websockets
    from yeoman_gateway.channels.whatsapp_runtime import (
        PROTOCOL_VERSION,
        WhatsAppRuntimeManager,
    )
except Exception as exc:  # pragma: no cover - wrong interpreter path
    print(
        "Run this script with the Yeoman tool Python, usually:\n"
        "  /home/dm/.local/share/uv/tools/yeoman-gateway/bin/python3 "
        "scripts/whatsapp_qr_reconnect.py ...\n"
        f"Import failed: {type(exc).__name__}: {exc}",
        file=sys.stderr,
    )
    raise SystemExit(2)


def repo_root(path: Path | None = None) -> Path:
    """Return the Yeoman checkout root for this script."""
    return (path or Path(__file__)).resolve().parents[1]


DEFAULT_SOURCE_DIR = repo_root()
RUN_DIR = Path.home() / ".yeoman" / "var" / "run"
SECRETS_DIR = Path.home() / ".yeoman" / "secrets"
AUTH_DIR = SECRETS_DIR / "whatsapp-auth"
DEFAULT_SVG = RUN_DIR / "whatsapp-login-qr.svg"


def run(cmd: list[str], *, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, check=check)


def systemctl(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run(["systemctl", "--user", *args], check=check)


def ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)


def active_units() -> list[str]:
    result = systemctl(
        "is-active",
        "yeoman-bridge.service",
        "yeoman-gateway.service",
        "yeoman-overseer.service",
        check=False,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def refresh_bridge_runtime() -> None:
    runtime = WhatsAppRuntimeManager()
    path = runtime.ensure_runtime()
    print(f"runtime={path}")
    print(f"runtime_refreshed={runtime._runtime_refreshed}")


def backup_auth() -> Path | None:
    ensure_private_dir(SECRETS_DIR)
    if not AUTH_DIR.exists():
        ensure_private_dir(AUTH_DIR)
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = SECRETS_DIR / f"whatsapp-auth.bak-{stamp}"
    AUTH_DIR.rename(backup)
    for root, dirs, files in os.walk(backup):
        Path(root).chmod(0o700)
        for name in dirs:
            (Path(root) / name).chmod(0o700)
        for name in files:
            (Path(root) / name).chmod(0o600)
    ensure_private_dir(AUTH_DIR)
    return backup


def latest_backup() -> Path | None:
    backups = sorted(SECRETS_DIR.glob("whatsapp-auth.bak-*"), key=lambda p: p.stat().st_mtime, reverse=True)
    return backups[0] if backups else None


def cleanup_qr(svg_path: Path = DEFAULT_SVG) -> None:
    svg_path.unlink(missing_ok=True)
    for raw in RUN_DIR.glob("whatsapp-login-qr.raw.*"):
        raw.unlink(missing_ok=True)


async def bridge_command(command_type: str, payload: dict[str, Any], timeout_s: float) -> dict[str, Any]:
    runtime = WhatsAppRuntimeManager()
    token = runtime.ensure_bridge_token(quiet=True)
    request_id = uuid.uuid4().hex
    envelope = {
        "version": PROTOCOL_VERSION,
        "type": command_type,
        "token": token,
        "requestId": request_id,
        "accountId": "default",
        "payload": payload,
    }
    async with websockets.connect(
        runtime._resolve_bridge_url(),
        max_size=runtime.config.channels.whatsapp.max_payload_bytes,
    ) as ws:
        await ws.send(json.dumps(envelope))
        deadline = time.monotonic() + timeout_s
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError(f"Timed out waiting for {command_type} response")
            raw = await asyncio.wait_for(ws.recv(), timeout=left)
            data = json.loads(raw)
            if data.get("type") != "response" or data.get("requestId") != request_id:
                continue
            response = data.get("payload") or {}
            if not response.get("ok"):
                raise RuntimeError(response.get("error"))
            result = response.get("result")
            if not isinstance(result, dict):
                raise RuntimeError(f"{command_type} returned malformed result")
            return result


def render_qr_svg(qr_text: str, svg_path: Path, *, reload_seconds: int) -> None:
    ensure_private_dir(svg_path.parent)
    with tempfile.NamedTemporaryFile("w", dir=svg_path.parent, prefix="whatsapp-login-qr.raw.", delete=False) as tmp:
        raw_path = Path(tmp.name)
        tmp.write(qr_text)
    raw_path.chmod(0o600)

    node_code = r"""
const fs = require('fs');
const QRCode = require('/home/dm/.yeoman/var/cache/bridge/node_modules/qrcode-terminal/vendor/QRCode');
const QRErrorCorrectLevel = require('/home/dm/.yeoman/var/cache/bridge/node_modules/qrcode-terminal/vendor/QRCode/QRErrorCorrectLevel');
const input = fs.readFileSync(process.env.QR_RAW, 'utf8');
const qrcode = new QRCode(-1, QRErrorCorrectLevel.L);
qrcode.addData(input);
qrcode.make();
const count = qrcode.getModuleCount();
const margin = 4;
const scale = 10;
const size = (count + margin * 2) * scale;
let rects = [];
for (let r = 0; r < count; r++) {
  for (let c = 0; c < count; c++) {
    if (qrcode.isDark(r, c)) {
      rects.push(`<rect x="${(c + margin) * scale}" y="${(r + margin) * scale}" width="${scale}" height="${scale}"/>`);
    }
  }
}
const reloadMs = Math.max(5, Number(process.env.RELOAD_SECONDS || '15')) * 1000;
const svg = `<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="${size}" height="${size}" viewBox="0 0 ${size} ${size}">
<script><![CDATA[setTimeout(function(){ window.location.reload(); }, ${reloadMs});]]></script>
<rect width="100%" height="100%" fill="#fff"/>
<g fill="#000">
${rects.join('\n')}
</g>
</svg>
`;
fs.writeFileSync(process.env.OUT_SVG, svg, {mode: 0o600});
"""
    env = os.environ.copy()
    env["QR_RAW"] = str(raw_path)
    env["OUT_SVG"] = str(svg_path)
    env["RELOAD_SECONDS"] = str(reload_seconds)
    try:
        if not shutil.which("node"):
            raise RuntimeError("node is required to render QR SVG")
        subprocess.run(["node", "-e", node_code], env=env, cwd=DEFAULT_SOURCE_DIR, check=True)
        svg_path.chmod(0o600)
    finally:
        raw_path.unlink(missing_ok=True)


def health() -> dict[str, Any]:
    return WhatsAppRuntimeManager().health_check(timeout_s=10)


def cmd_status(_: argparse.Namespace) -> int:
    print(f"repo={DEFAULT_SOURCE_DIR}")
    print(f"units={active_units()}")
    print(json.dumps(health(), indent=2, sort_keys=True))
    return 0


def cmd_cleanup(args: argparse.Namespace) -> int:
    cleanup_qr(Path(args.svg_path))
    print(f"removed={args.svg_path}")
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    backup = Path(args.backup) if args.backup else latest_backup()
    if backup is None or not backup.exists():
        print("No whatsapp-auth.bak-* backup found", file=sys.stderr)
        return 1

    cleanup_qr(Path(args.svg_path))
    systemctl("stop", "yeoman-bridge.service", check=False)
    if AUTH_DIR.exists():
        stamp = time.strftime("%Y%m%d-%H%M%S")
        incomplete = SECRETS_DIR / f"whatsapp-auth.incomplete-{stamp}"
        AUTH_DIR.rename(incomplete)
        print(f"current_auth_moved={incomplete}")
    backup.rename(AUTH_DIR)
    AUTH_DIR.chmod(0o700)
    systemctl("start", "yeoman-bridge.service")
    time.sleep(5)
    systemctl("restart", "yeoman-gateway.service", check=False)
    print(f"restored={AUTH_DIR}")
    print(f"units={active_units()}")
    return 0


async def start_loop(args: argparse.Namespace) -> int:
    svg_path = Path(args.svg_path)
    cleanup_qr(svg_path)
    refresh_bridge_runtime()

    if args.fresh_auth:
        systemctl("stop", "yeoman-bridge.service", check=False)
        backup = backup_auth()
        print(f"auth_backup={backup or 'none'}")

    systemctl("restart", "yeoman-bridge.service")
    time.sleep(args.startup_wait_seconds)
    if args.restart_gateway_first:
        systemctl("restart", "yeoman-gateway.service", check=False)

    deadline = time.monotonic() + args.timeout_minutes * 60
    next_qr_at = 0.0
    while time.monotonic() < deadline:
        current = health()
        print(json.dumps(current, sort_keys=True))
        if current.get("whatsapp", {}).get("connected"):
            cleanup_qr(svg_path)
            systemctl("restart", "yeoman-gateway.service", check=False)
            print("connected=true")
            return 0

        now = time.monotonic()
        if now >= next_qr_at:
            result = await bridge_command(
                "login_start",
                {"force": True, "timeoutMs": args.login_timeout_seconds * 1000},
                timeout_s=args.login_timeout_seconds + 5,
            )
            login = result.get("login") or {}
            qr = login.get("qr")
            if not qr:
                raise RuntimeError(f"login_start returned no qr: {login}")
            render_qr_svg(qr, svg_path, reload_seconds=args.svg_reload_seconds)
            print(f"qr_svg={svg_path}")
            next_qr_at = now + args.refresh_seconds

        await asyncio.sleep(args.poll_seconds)

    print("connected=false timeout=true", file=sys.stderr)
    return 1


def cmd_start(args: argparse.Namespace) -> int:
    return asyncio.run(start_loop(args))


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    start = sub.add_parser("start", help="Start QR reconnect flow and keep SVG refreshed")
    start.add_argument("--svg-path", default=str(DEFAULT_SVG))
    start.add_argument("--fresh-auth", action=argparse.BooleanOptionalAction, default=True)
    start.add_argument("--timeout-minutes", type=int, default=8)
    start.add_argument("--refresh-seconds", type=int, default=75)
    start.add_argument("--poll-seconds", type=int, default=10)
    start.add_argument("--login-timeout-seconds", type=int, default=90)
    start.add_argument("--svg-reload-seconds", type=int, default=15)
    start.add_argument("--startup-wait-seconds", type=int, default=4)
    start.add_argument("--restart-gateway-first", action="store_true")
    start.set_defaults(func=cmd_start)

    status = sub.add_parser("status", help="Print service and bridge health")
    status.set_defaults(func=cmd_status)

    restore = sub.add_parser("restore", help="Restore latest or specified auth backup")
    restore.add_argument("--backup")
    restore.add_argument("--svg-path", default=str(DEFAULT_SVG))
    restore.set_defaults(func=cmd_restore)

    cleanup = sub.add_parser("cleanup", help="Remove QR artifacts only")
    cleanup.add_argument("--svg-path", default=str(DEFAULT_SVG))
    cleanup.set_defaults(func=cmd_cleanup)
    return p


def main() -> int:
    args = parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
