"""Shared runtime manager for the WhatsApp Node bridge."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from loguru import logger

from nanobot.config.loader import get_config_path, get_data_dir, load_config, save_config

PROTOCOL_VERSION = 2
MANIFEST_FILENAME = "bridge.manifest.json"
DEFAULT_BUILD_ID = "dev"


@dataclass(slots=True)
class BridgeManifest:
    bridge_version: str
    protocol_version: int
    build_id: str


@dataclass(slots=True)
class BridgeStatus:
    running: bool
    port: int
    pids: list[int]
    log_path: Path


@dataclass(slots=True)
class BridgeReadyReport:
    ready: bool
    repaired: bool
    started: bool
    runtime_dir: Path
    status: BridgeStatus
    health: dict[str, Any]


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _command_for_pid(pid: int) -> str:
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "command="],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _process_cwd(pid: int) -> Path | None:
    proc_cwd = Path(f"/proc/{pid}/cwd")
    try:
        if proc_cwd.exists():
            return proc_cwd.resolve()
    except OSError:
        return None
    return None


def _is_bridge_dir(path: Path) -> bool:
    package_json = path / "package.json"
    if not package_json.exists():
        return False
    try:
        data = json.loads(package_json.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return data.get("name") == "nanobot-whatsapp-bridge"


def _is_bridge_process(pid: int) -> bool:
    cmd = _command_for_pid(pid).lower()
    if not cmd:
        return False
    cwd = _process_cwd(pid)
    if cwd and _is_bridge_dir(cwd):
        return True
    return "nanobot-whatsapp-bridge" in cmd


def _listener_pids_for_port(port: int) -> set[int]:
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


class WhatsAppRuntimeManager:
    """Owns WhatsApp bridge runtime lifecycle and health orchestration."""

    def __init__(
        self,
        config=None,
        *,
        source_bridge_dir: Path | None = None,
        user_bridge_dir: Path | None = None,
    ):
        self.config = config or load_config()
        self._source_bridge_dir = source_bridge_dir
        self._user_bridge_dir = user_bridge_dir or (get_data_dir() / "bridge")
        self._runtime_refreshed = False

    @property
    def user_bridge_dir(self) -> Path:
        return self._user_bridge_dir

    @property
    def bridge_log_path(self) -> Path:
        logs_dir = get_data_dir() / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        return logs_dir / "whatsapp-bridge.log"

    @property
    def bridge_pid_path(self) -> Path:
        run_dir = get_data_dir() / "run"
        run_dir.mkdir(parents=True, exist_ok=True)
        return run_dir / "whatsapp-bridge.pid"

    def _resolve_bridge_port(self) -> int:
        wa = self.config.channels.whatsapp
        if wa.bridge_port:
            return wa.bridge_port
        parsed = urlparse(wa.bridge_url)
        if parsed.port is not None:
            return parsed.port
        if parsed.scheme == "wss":
            return 443
        if parsed.scheme == "ws":
            return 80
        return 3001

    def _resolve_bridge_url(self) -> str:
        wa = self.config.channels.whatsapp
        host = (wa.bridge_host or "").strip() or "127.0.0.1"
        port = self._resolve_bridge_port()
        return f"ws://{host}:{port}"

    def _resolve_source_bridge_dir(self) -> Path:
        if self._source_bridge_dir:
            return self._source_bridge_dir

        package_bridge = Path(__file__).resolve().parents[1] / "bridge"
        repo_bridge = Path(__file__).resolve().parents[2] / "bridge"
        for candidate in (package_bridge, repo_bridge):
            if (candidate / "package.json").exists() and (candidate / MANIFEST_FILENAME).exists():
                return candidate
        raise RuntimeError("Bridge source not found. Install nanobot with packaged bridge artifacts.")

    def _read_manifest(self, path: Path) -> BridgeManifest:
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            raise RuntimeError(f"Invalid bridge manifest {path}: {e}") from e

        bridge_version = str(data.get("bridgeVersion") or "").strip()
        build_id = str(data.get("buildId") or "").strip() or DEFAULT_BUILD_ID
        protocol_version = int(data.get("protocolVersion") or 0)
        if not bridge_version:
            raise RuntimeError(f"Bridge manifest missing bridgeVersion: {path}")
        if protocol_version != PROTOCOL_VERSION:
            raise RuntimeError(
                f"Bridge manifest protocol mismatch: expected {PROTOCOL_VERSION}, got {protocol_version}"
            )
        return BridgeManifest(
            bridge_version=bridge_version,
            protocol_version=protocol_version,
            build_id=build_id,
        )

    def _validate_bridge_artifacts(self, root: Path) -> BridgeManifest:
        manifest_path = root / MANIFEST_FILENAME
        if not manifest_path.exists():
            raise RuntimeError(f"Missing bridge manifest: {manifest_path}")
        manifest = self._read_manifest(manifest_path)
        required = [
            root / "dist" / "index.js",
            root / "dist" / "server.js",
            root / "dist" / "protocol.js",
            root / "dist" / "whatsapp.js",
            root / "package.json",
        ]
        missing = [str(p) for p in required if not p.exists()]
        if missing:
            raise RuntimeError(f"Incomplete bridge runtime artifacts: {', '.join(missing)}")
        return manifest

    def _runtime_fingerprint(self, root: Path, manifest: BridgeManifest) -> str:
        """Content fingerprint for bridge runtime artifact parity checks."""
        hasher = hashlib.sha256()
        hasher.update(f"bridgeVersion={manifest.bridge_version}\n".encode())
        hasher.update(f"protocolVersion={manifest.protocol_version}\n".encode())
        hasher.update(f"buildId={manifest.build_id}\n".encode())

        files = [
            root / "bridge.manifest.json",
            root / "package.json",
            root / "dist" / "index.js",
            root / "dist" / "server.js",
            root / "dist" / "protocol.js",
            root / "dist" / "whatsapp.js",
        ]
        for file_path in files:
            if not file_path.exists():
                hasher.update(f"missing:{file_path.name}\n".encode())
                continue
            hasher.update(f"name:{file_path.name}\n".encode())
            hasher.update(file_path.read_bytes())
            hasher.update(b"\n")
        return hasher.hexdigest()

    def _ensure_runtime_dependencies(self, root: Path) -> None:
        node_modules = root / "node_modules"
        if node_modules.exists():
            return
        if not shutil.which("npm"):
            raise RuntimeError("npm is required on first bridge runtime install (dependencies only)")
        result = subprocess.run(
            ["npm", "install", "--omit=dev", "--no-fund", "--no-audit"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            raise RuntimeError(f"Bridge dependency install failed: {stderr[:800]}")

    def ensure_runtime(self) -> Path:
        source = self._resolve_source_bridge_dir()
        source_manifest = self._validate_bridge_artifacts(source)
        source_fingerprint = self._runtime_fingerprint(source, source_manifest)
        self._runtime_refreshed = False

        target = self.user_bridge_dir
        if target.exists():
            try:
                target_manifest = self._validate_bridge_artifacts(target)
            except RuntimeError:
                target_manifest = None
            if (
                target_manifest
                and target_manifest.build_id == source_manifest.build_id
                and target_manifest.protocol_version == source_manifest.protocol_version
            ):
                target_fingerprint = self._runtime_fingerprint(target, target_manifest)
                if target_fingerprint == source_fingerprint:
                    self._ensure_runtime_dependencies(target)
                    return target
                logger.info(
                    "Bridge runtime fingerprint mismatch; refreshing artifacts "
                    "(buildId={}, protocol={})",
                    source_manifest.build_id,
                    source_manifest.protocol_version,
                )

        target.parent.mkdir(parents=True, exist_ok=True)
        tmp_dir = target.parent / f"{target.name}.tmp-{uuid.uuid4().hex[:8]}"
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir)

        shutil.copytree(
            source,
            tmp_dir,
            ignore=shutil.ignore_patterns("node_modules", "src", "*.ts", "*.d.ts"),
        )

        self._validate_bridge_artifacts(tmp_dir)

        backup_dir = target.parent / f"{target.name}.bak-{int(time.time())}"
        if target.exists():
            if backup_dir.exists():
                shutil.rmtree(backup_dir)
            target.rename(backup_dir)
        tmp_dir.rename(target)
        self._runtime_refreshed = True
        self._ensure_runtime_dependencies(target)
        return target

    def ensure_bridge_token(self, *, quiet: bool = True) -> str:
        token = (self.config.channels.whatsapp.bridge_token or "").strip()
        if token:
            return token

        token = uuid.uuid4().hex + uuid.uuid4().hex
        self.config.channels.whatsapp.bridge_token = token
        save_config(self.config)
        try:
            get_config_path().chmod(0o600)
        except OSError:
            # Best effort; platform/permissions may disallow chmod.
            pass
        if not quiet:
            logger.info(f"Generated channels.whatsapp.bridgeToken at {get_config_path()}")
        return token

    def rotate_bridge_token(self) -> tuple[str, str]:
        old_token = (self.config.channels.whatsapp.bridge_token or "").strip()
        new_token = uuid.uuid4().hex + uuid.uuid4().hex
        self.config.channels.whatsapp.bridge_token = new_token
        save_config(self.config)
        try:
            get_config_path().chmod(0o600)
        except OSError:
            pass
        return old_token, new_token

    def _find_bridge_pids(self, port: int) -> list[int]:
        pids: set[int] = set(_listener_pids_for_port(port))

        pid_file = self.bridge_pid_path
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text().strip())
                if _pid_alive(pid) and (pid in pids or _is_bridge_process(pid)):
                    pids.add(pid)
            except ValueError:
                pass
        return sorted(pid for pid in pids if _pid_alive(pid))

    def status_bridge(self, port: int | None = None) -> BridgeStatus:
        resolved_port = self._resolve_bridge_port() if port is None else port
        pids = self._find_bridge_pids(resolved_port)
        return BridgeStatus(
            running=bool(pids),
            port=resolved_port,
            pids=pids,
            log_path=self.bridge_log_path,
        )

    def _signal_bridge_pid(self, pid: int, sig: int) -> None:
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
        try:
            os.kill(pid, sig)
        except OSError:
            pass

    def stop_bridge(self, port: int | None = None, timeout_s: float = 8.0) -> int:
        resolved_port = self._resolve_bridge_port() if port is None else port
        pids = self._find_bridge_pids(resolved_port)
        if not pids:
            self.bridge_pid_path.unlink(missing_ok=True)
            return 0

        for pid in pids:
            self._signal_bridge_pid(pid, signal.SIGTERM)

        deadline = time.time() + timeout_s
        while time.time() < deadline:
            remaining = [pid for pid in pids if _pid_alive(pid)]
            listeners = self._find_bridge_pids(resolved_port)
            if not remaining and not listeners:
                self.bridge_pid_path.unlink(missing_ok=True)
                return len(pids)
            time.sleep(0.2)

        for pid in [pid for pid in pids if _pid_alive(pid)]:
            self._signal_bridge_pid(pid, signal.SIGKILL)
        for pid in self._find_bridge_pids(resolved_port):
            self._signal_bridge_pid(pid, signal.SIGKILL)
        self.bridge_pid_path.unlink(missing_ok=True)
        return len(pids)

    def start_bridge(self, port: int | None = None) -> BridgeStatus:
        if not shutil.which("node"):
            raise RuntimeError("node not found. Install Node.js >= 20.")

        bridge_dir = self.ensure_runtime()
        token = self.ensure_bridge_token(quiet=True)
        wa = self.config.channels.whatsapp
        resolved_port = self._resolve_bridge_port() if port is None else port
        status = self.status_bridge(resolved_port)
        if status.running:
            return status

        with open(self.bridge_log_path, "a") as log_file:
            env = dict(os.environ)
            env["BRIDGE_PORT"] = str(resolved_port)
            env["BRIDGE_HOST"] = wa.bridge_host
            env["BRIDGE_TOKEN"] = token
            env["AUTH_DIR"] = str(Path(wa.auth_dir).expanduser())
            env["WHATSAPP_READ_RECEIPTS"] = "1" if wa.read_receipts else "0"
            env["WHATSAPP_ACCEPT_FROM_ME"] = "1" if wa.accept_from_me else "0"
            env["WHATSAPP_PERSIST_INBOUND_AUDIO"] = "1" if wa.media.persist_incoming_audio else "0"
            incoming_media = wa.media.incoming_path.expanduser().resolve()
            outgoing_media = wa.media.outgoing_path.expanduser().resolve()
            media_root = Path(
                os.path.commonpath([str(incoming_media), str(outgoing_media)])
            )
            env["MEDIA_DIR"] = str(media_root)
            env["MEDIA_INCOMING_DIR"] = str(incoming_media)
            env["MEDIA_OUTGOING_DIR"] = str(outgoing_media)
            proc = subprocess.Popen(
                ["node", "dist/index.js"],
                cwd=bridge_dir,
                env=env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )

        time.sleep(0.6)
        if proc.poll() is not None:
            raise RuntimeError(f"Bridge failed to start. Check log: {self.bridge_log_path}")

        self.bridge_pid_path.write_text(str(proc.pid))
        return self.status_bridge(resolved_port)

    def restart_bridge(self, port: int | None = None) -> BridgeStatus:
        resolved_port = self._resolve_bridge_port() if port is None else port
        self.stop_bridge(resolved_port)
        time.sleep(0.2)
        return self.start_bridge(resolved_port)

    async def _health_check_async(self, timeout_s: float) -> dict[str, Any]:
        import websockets

        token = self.ensure_bridge_token(quiet=True)
        request_id = uuid.uuid4().hex
        envelope = {
            "version": PROTOCOL_VERSION,
            "type": "health",
            "token": token,
            "requestId": request_id,
            "accountId": "default",
            "payload": {},
        }
        async with websockets.connect(
            self._resolve_bridge_url(),
            max_size=self.config.channels.whatsapp.max_payload_bytes,
        ) as ws:
            await ws.send(json.dumps(envelope))
            deadline = time.monotonic() + timeout_s
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError("Bridge health check timed out")
                raw = await asyncio.wait_for(ws.recv(), timeout=left)
                data = json.loads(raw)
                if not isinstance(data, dict):
                    continue
                if data.get("version") != PROTOCOL_VERSION:
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
                protocol_version = result.get("protocolVersion", result.get("version"))
                if protocol_version != PROTOCOL_VERSION:
                    raise RuntimeError(
                        f"Bridge protocol mismatch: expected {PROTOCOL_VERSION}, got {protocol_version!r}"
                    )
                return result

    def health_check(self, timeout_s: float) -> dict[str, Any]:
        return asyncio.run(self._health_check_async(timeout_s))

    def repair_once(self, port: int | None = None) -> BridgeStatus:
        resolved_port = self._resolve_bridge_port() if port is None else port
        self.ensure_runtime()
        self.stop_bridge(resolved_port)
        time.sleep(0.2)
        return self.start_bridge(resolved_port)

    def ensure_ready(
        self,
        *,
        auto_repair: bool | None = None,
        start_if_needed: bool = True,
        timeout_s: float | None = None,
    ) -> BridgeReadyReport:
        wa = self.config.channels.whatsapp
        repair_enabled = wa.bridge_auto_repair if auto_repair is None else auto_repair
        timeout = timeout_s if timeout_s is not None else max(1.0, wa.bridge_startup_timeout_ms / 1000.0)

        runtime_dir = self.ensure_runtime()
        self.ensure_bridge_token(quiet=True)
        status = self.status_bridge()
        started = False

        if not status.running:
            if not start_if_needed:
                return BridgeReadyReport(
                    ready=False,
                    repaired=False,
                    started=False,
                    runtime_dir=runtime_dir,
                    status=status,
                    health={},
                )
            status = self.start_bridge()
            started = True
        elif self._runtime_refreshed:
            logger.info("Bridge runtime was refreshed; restarting running bridge process")
            status = self.repair_once(status.port)
            started = True

        repaired = False
        try:
            health = self.health_check(timeout)
        except Exception as e:
            if not repair_enabled:
                raise RuntimeError(f"Bridge health check failed: {e}") from e
            repaired = True
            status = self.repair_once(status.port)
            health = self.health_check(timeout)

        return BridgeReadyReport(
            ready=True,
            repaired=repaired,
            started=started,
            runtime_dir=runtime_dir,
            status=status,
            health=health,
        )
