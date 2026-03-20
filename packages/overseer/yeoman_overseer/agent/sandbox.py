"""Bubblewrap sandbox wrapper — per-call UUID tmpdir, no network, sensitive paths masked."""
from __future__ import annotations

import shutil
import subprocess
import uuid
from pathlib import Path


class SandboxUnavailableError(RuntimeError):
    """Raised when bwrap is not available on PATH."""


class Sandbox:
    _bwrap: str | None = None

    @classmethod
    def _find_bwrap(cls) -> str:
        if cls._bwrap is None:
            found = shutil.which("bwrap")
            if not found:
                raise SandboxUnavailableError("bwrap not found on PATH")
            cls._bwrap = found
        return cls._bwrap

    def run(
        self,
        cmd: list[str],
        *,
        timeout: int = 60,
        source_root: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> dict[str, object]:
        """Run cmd inside bubblewrap. Returns {stdout, stderr, exit_code}."""
        bwrap = self._find_bwrap()

        yeoman_home = Path.home() / ".yeoman"
        source_dir = source_root or (Path.home() / "Documents" / "yeoman")

        tmpdir = Path(f"/tmp/overseer-{uuid.uuid4().hex}")
        tmpdir.mkdir(mode=0o700)

        bwrap_args: list[str] = [
            bwrap,
            # System read-only
            "--ro-bind", "/usr", "/usr",
            "--ro-bind", "/lib", "/lib",
            "--ro-bind-try", "/lib64", "/lib64",
            "--ro-bind", "/bin", "/bin",
            "--ro-bind", "/sbin", "/sbin",
            "--ro-bind", "/etc/ld.so.cache", "/etc/ld.so.cache",
            # Application read-only
            "--ro-bind", str(yeoman_home), str(yeoman_home),
            "--ro-bind", str(source_dir), str(source_dir),
            # Sensitive path masking
            "--tmpfs", str(yeoman_home / "secrets"),
            "--ro-bind", "/dev/null", str(yeoman_home / ".env"),
            # Ephemeral writable tmp — unique per call
            "--bind", str(tmpdir), "/tmp",
            "--proc", "/proc",
            "--dev", "/dev",
            "--unshare-net",
            "--unshare-pid",
            "--die-with-parent",
            "--",
            *cmd,
        ]

        kwargs: dict = dict(capture_output=True, text=True, timeout=timeout)
        if env is not None:
            import os
            kwargs["env"] = {**os.environ, **env}

        try:
            result = subprocess.run(bwrap_args, **kwargs)
            return {
                "stdout": result.stdout,
                "stderr": result.stderr,
                "exit_code": result.returncode,
            }
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
