"""Bubblewrap-based exec isolation with batch-session lifecycle."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import platform
import shlex
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

DEFAULT_BLOCKED_HOST_PATTERNS = [
    ".ssh",
    ".aws",
    ".env",
    "id_rsa",
    "id_ed25519",
    "id_dsa",
]

DEFAULT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


class IsolationUnavailableError(RuntimeError):
    """Raised when the configured isolation backend is unavailable."""


class SandboxTimeoutError(RuntimeError):
    """Raised when command execution times out inside sandbox."""


class SandboxPreemptedError(RuntimeError):
    """Raised when sandbox was preempted by capacity policy."""


class SandboxExecutionError(RuntimeError):
    """Raised when sandbox command channel breaks."""


@dataclass(slots=True)
class CommandResult:
    """Single command result from sandbox session."""

    output: str
    exit_code: int


@dataclass(slots=True)
class MountAllowlist:
    """Host-side allowlist rules for mount validation."""

    allowed_roots: list[Path]
    blocked_patterns: list[str]

    @classmethod
    def load(cls, path: Path) -> "MountAllowlist":
        if not path.exists():
            raise IsolationUnavailableError(
                f"Mount allowlist not found at {path}. "
                "Create it or disable exec isolation."
            )

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            raise IsolationUnavailableError(f"Failed to parse allowlist file {path}: {e}") from e

        if not isinstance(data, dict):
            raise IsolationUnavailableError(f"Invalid allowlist format at {path}: expected object")

        raw_roots = data.get("allowedRoots", [])
        if not isinstance(raw_roots, list) or not raw_roots:
            raise IsolationUnavailableError(
                f"Allowlist at {path} must define a non-empty allowedRoots array"
            )

        allowed_roots: list[Path] = []
        for root in raw_roots:
            if not isinstance(root, str) or not root.strip():
                continue
            allowed_roots.append(Path(root).expanduser().resolve())

        if not allowed_roots:
            raise IsolationUnavailableError(
                f"Allowlist at {path} has no valid allowedRoots entries"
            )

        raw_patterns = data.get("blockedHostPatterns", DEFAULT_BLOCKED_HOST_PATTERNS)
        blocked_patterns = (
            [str(p) for p in raw_patterns if isinstance(p, str) and p.strip()]
            if isinstance(raw_patterns, list)
            else list(DEFAULT_BLOCKED_HOST_PATTERNS)
        )

        return cls(allowed_roots=allowed_roots, blocked_patterns=blocked_patterns)

    def validate_workspace(self, workspace: Path) -> None:
        resolved = workspace.expanduser().resolve()

        lower = str(resolved).lower()
        for pattern in self.blocked_patterns:
            if pattern.lower() in lower:
                raise IsolationUnavailableError(
                    f"Workspace path {resolved} blocked by allowlist pattern '{pattern}'"
                )

        if not any(_is_within(resolved, root) for root in self.allowed_roots):
            roots = ", ".join(str(r) for r in self.allowed_roots)
            raise IsolationUnavailableError(
                f"Workspace path {resolved} is outside allowedRoots ({roots})"
            )


class BubblewrapSandboxSession:
    """Persistent bubblewrap shell process for one batch-session."""

    def __init__(self, session_key: str, workspace: Path):
        self.session_key = session_key
        self.workspace = workspace

        self._process: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._buffer = b""
        self._preempt_reason: str | None = None

        self.created_at = time.monotonic()
        self.last_used_at = self.created_at
        self.active_since: float | None = None

    @property
    def active(self) -> bool:
        return self.active_since is not None

    async def start(self) -> None:
        if self._process and self._process.returncode is None:
            return

        cmd = self._build_bwrap_command()
        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

    async def run_command(self, command: str, cwd: str, timeout: int) -> CommandResult:
        if self._preempt_reason:
            raise SandboxPreemptedError(self._preempt_reason)

        if not self._process or self._process.returncode is not None:
            await self.start()

        assert self._process is not None
        assert self._process.stdin is not None
        assert self._process.stdout is not None

        marker = f"__NB_DONE_{uuid.uuid4().hex}__"
        encoded = base64.b64encode(command.encode("utf-8")).decode("ascii")
        quoted_cwd = shlex.quote(cwd)
        script = (
            f"cd {quoted_cwd} || exit 1\n"
            f"__NB_CMD_B64='{encoded}'\n"
            "__NB_CMD=$(printf '%s' \"$__NB_CMD_B64\" | base64 -d)\n"
            "eval \"$__NB_CMD\"\n"
            "__NB_STATUS=$?\n"
            f"printf '\\n{marker}:%s\\n' \"$__NB_STATUS\"\n"
        )

        async with self._lock:
            if self._preempt_reason:
                raise SandboxPreemptedError(self._preempt_reason)

            now = time.monotonic()
            self.active_since = now
            self.last_used_at = now

            try:
                self._process.stdin.write(script.encode("utf-8"))
                await self._process.stdin.drain()
                output, exit_code = await self._read_until_marker(marker, timeout=timeout)
                self.last_used_at = time.monotonic()
                return CommandResult(output=output, exit_code=exit_code)
            except asyncio.TimeoutError as e:
                await self.stop(reason="timed out")
                raise SandboxTimeoutError(
                    f"Command timed out after {timeout} seconds"
                ) from e
            except SandboxPreemptedError:
                raise
            except Exception as e:
                await self.stop(reason="command channel error")
                raise SandboxExecutionError(str(e)) from e
            finally:
                self.active_since = None

    async def preempt(self, reason: str) -> None:
        self._preempt_reason = reason
        await self.stop(reason=reason)

    async def stop(self, reason: str | None = None) -> None:
        process = self._process
        self._process = None
        self._buffer = b""

        if not process or process.returncode is not None:
            return

        process.kill()
        try:
            await asyncio.wait_for(process.wait(), timeout=1.5)
        except Exception:
            pass

        if reason and not self._preempt_reason:
            self._preempt_reason = reason

    def stop_now(self) -> None:
        process = self._process
        self._process = None
        self._buffer = b""
        if process and process.returncode is None:
            process.kill()

    def _build_bwrap_command(self) -> list[str]:
        args = [
            "bwrap",
            "--die-with-parent",
            "--new-session",
            "--unshare-pid",
            "--unshare-ipc",
            "--unshare-uts",
            "--unshare-cgroup",
            "--share-net",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
        ]

        for path in ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc", "/opt"):
            if Path(path).exists():
                args.extend(["--ro-bind", path, path])

        args.extend(["--bind", str(self.workspace), "/workspace"])
        args.extend(["--chdir", "/workspace"])
        args.append("--clearenv")

        path = os.environ.get("PATH") or DEFAULT_PATH
        args.extend(["--setenv", "PATH", path])
        args.extend(["--setenv", "HOME", "/workspace"])
        args.extend(["--setenv", "PWD", "/workspace"])

        for key in ("LANG", "LC_ALL", "LC_CTYPE", "TERM", "TZ"):
            value = os.environ.get(key)
            if value:
                args.extend(["--setenv", key, value])

        args.extend(["/bin/sh"])
        return args

    async def _read_until_marker(self, marker: str, timeout: int) -> tuple[str, int]:
        if not self._process or not self._process.stdout:
            raise SandboxExecutionError("sandbox process stdout unavailable")

        marker_prefix = f"\n{marker}:".encode("utf-8")
        deadline = time.monotonic() + timeout

        while True:
            marker_idx = self._buffer.find(marker_prefix)
            if marker_idx != -1:
                status_start = marker_idx + len(marker_prefix)
                newline_idx = self._buffer.find(b"\n", status_start)
                if newline_idx != -1:
                    raw_status = self._buffer[status_start:newline_idx].decode(
                        "utf-8", errors="replace"
                    )
                    if not raw_status.isdigit():
                        raise SandboxExecutionError(f"invalid exit code marker: {raw_status!r}")

                    raw_output = self._buffer[:marker_idx]
                    self._buffer = self._buffer[newline_idx + 1 :]
                    output = raw_output.decode("utf-8", errors="replace").strip("\n")
                    return output, int(raw_status)

            if self._preempt_reason:
                raise SandboxPreemptedError(self._preempt_reason)

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise asyncio.TimeoutError()

            chunk = await asyncio.wait_for(self._process.stdout.read(4096), timeout=remaining)
            if not chunk:
                if self._preempt_reason:
                    raise SandboxPreemptedError(self._preempt_reason)
                raise SandboxExecutionError("sandbox process exited unexpectedly")
            self._buffer += chunk


class ExecSandboxManager:
    """Global manager for per-session bubblewrap sandboxes."""

    def __init__(
        self,
        workspace: Path,
        max_containers: int,
        idle_seconds: int,
        pressure_policy: str,
        allowlist_path: Path,
    ):
        self.workspace = workspace.expanduser().resolve()
        self.max_containers = max(1, max_containers)
        self.idle_seconds = max(30, idle_seconds)
        self.pressure_policy = pressure_policy
        self.allowlist_path = allowlist_path

        self._sessions: dict[str, BubblewrapSandboxSession] = {}
        self._lock = asyncio.Lock()

        self._check_runtime()
        self._allowlist = MountAllowlist.load(self.allowlist_path)
        self._allowlist.validate_workspace(self.workspace)

    async def execute(
        self,
        session_key: str,
        command: str,
        host_cwd: str,
        timeout: int,
    ) -> CommandResult:
        session = await self._get_or_create_session(session_key)
        cwd = self._to_container_path(host_cwd)

        try:
            return await session.run_command(command=command, cwd=cwd, timeout=timeout)
        except SandboxPreemptedError:
            # Preempted sessions are removed by capacity management.
            raise
        except (SandboxTimeoutError, SandboxExecutionError):
            # Broken sessions are discarded and lazily recreated on next request.
            await self._drop_session(session_key, session)
            raise

    async def aclose(self) -> None:
        async with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()

        for session in sessions:
            await session.stop(reason="manager shutdown")

    def close(self) -> None:
        sessions = list(self._sessions.values())
        self._sessions.clear()
        for session in sessions:
            session.stop_now()

    async def _get_or_create_session(self, session_key: str) -> BubblewrapSandboxSession:
        now = time.monotonic()

        async with self._lock:
            await self._expire_idle_locked(now)

            existing = self._sessions.get(session_key)
            if existing:
                return existing

            await self._ensure_capacity_locked()

            session = BubblewrapSandboxSession(session_key=session_key, workspace=self.workspace)
            await session.start()
            self._sessions[session_key] = session
            logger.debug(
                "exec sandbox created session={} total={}",
                session_key,
                len(self._sessions),
            )
            return session

    async def _expire_idle_locked(self, now: float) -> None:
        to_remove = [
            key
            for key, session in self._sessions.items()
            if (not session.active) and (now - session.last_used_at >= self.idle_seconds)
        ]
        for key in to_remove:
            session = self._sessions.pop(key, None)
            if session:
                await session.stop(reason="idle timeout")

    async def _ensure_capacity_locked(self) -> None:
        if len(self._sessions) < self.max_containers:
            return

        idle_candidates = [
            (session.last_used_at, key, session)
            for key, session in self._sessions.items()
            if not session.active
        ]
        if idle_candidates:
            _, key, victim = min(idle_candidates, key=lambda item: item[0])
            self._sessions.pop(key, None)
            await victim.stop(reason="evicted by capacity")
            return

        if self.pressure_policy != "preempt_oldest_active":
            raise IsolationUnavailableError("Sandbox capacity reached")

        active_candidates = [
            (session.active_since or session.last_used_at, key, session)
            for key, session in self._sessions.items()
            if session.active
        ]
        if not active_candidates:
            raise IsolationUnavailableError("Sandbox capacity reached")

        _, key, victim = min(active_candidates, key=lambda item: item[0])
        self._sessions.pop(key, None)
        await victim.preempt("preempted by capacity policy")

    async def _drop_session(self, session_key: str, session: BubblewrapSandboxSession) -> None:
        async with self._lock:
            current = self._sessions.get(session_key)
            if current is session:
                self._sessions.pop(session_key, None)
        await session.stop(reason="dropped")

    def _to_container_path(self, host_cwd: str) -> str:
        resolved = Path(host_cwd).expanduser().resolve()
        if not _is_within(resolved, self.workspace):
            raise IsolationUnavailableError(
                f"Working directory {resolved} is outside workspace {self.workspace}"
            )

        relative = resolved.relative_to(self.workspace)
        container = Path("/workspace") / relative
        self._validate_container_path(container)
        return container.as_posix()

    @staticmethod
    def _validate_container_path(path: Path) -> None:
        path_str = path.as_posix()
        if not path_str.startswith("/workspace"):
            raise IsolationUnavailableError(f"Invalid container path: {path_str}")
        if ".." in path.parts:
            raise IsolationUnavailableError(f"Path traversal in container path: {path_str}")

    @staticmethod
    def _check_runtime() -> None:
        if platform.system() != "Linux":
            raise IsolationUnavailableError("bubblewrap isolation is only supported on Linux")
        if os.geteuid() == 0:
            raise IsolationUnavailableError("bubblewrap isolation must run as non-root user")
        if shutil.which("bwrap") is None:
            raise IsolationUnavailableError("bubblewrap executable not found in PATH")


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
