"""Update the natively installed agent CLIs (Claude Code and Codex)."""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_AGENT_CLIS = ("claude", "codex")


@dataclass(frozen=True, slots=True)
class CliUpdateResult:
    name: str
    success: bool
    before: str
    after: str
    error: str = ""

    def describe(self) -> str:
        if not self.success:
            return f"{self.name}: failed ({self.error})"
        if self.before == self.after:
            return f"{self.name}: {self.after} (unchanged)"
        return f"{self.name}: {self.before} -> {self.after}"


async def update_agent_clis(
    names: tuple[str, ...] = DEFAULT_AGENT_CLIS,
    *,
    bin_dir: Path | None = None,
    timeout_s: float = 600.0,
) -> list[CliUpdateResult]:
    """Run each CLI's own `update` command, sequentially, and report versions.

    The native installers live in ~/.local/bin, which the Overseer's systemd
    environment does not have on PATH, so binaries are resolved explicitly.
    """
    directory = bin_dir or Path.home() / ".local" / "bin"
    env = {**os.environ, "PATH": f"{directory}{os.pathsep}{os.environ.get('PATH', '')}"}
    return [await _update_one(name, directory / name, env, timeout_s) for name in names]


async def _update_one(
    name: str,
    binary: Path,
    env: dict[str, str],
    timeout_s: float,
) -> CliUpdateResult:
    if not binary.exists():
        return CliUpdateResult(name, False, "", "", error=f"{binary} not found")
    before = await _version(binary, env)
    returncode, output = await _run(binary, "update", env=env, timeout_s=timeout_s)
    after = await _version(binary, env)
    if returncode != 0:
        tail = output.strip().splitlines()[-1:] or [f"exit {returncode}"]
        return CliUpdateResult(name, False, before, after, error=tail[0][:200])
    return CliUpdateResult(name, True, before, after)


async def _version(binary: Path, env: dict[str, str]) -> str:
    returncode, output = await _run(binary, "--version", env=env, timeout_s=30.0)
    if returncode != 0:
        return "unknown"
    return output.strip().splitlines()[0] if output.strip() else "unknown"


async def _run(
    binary: Path,
    *args: str,
    env: dict[str, str],
    timeout_s: float,
) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        str(binary),
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return -1, f"timed out after {timeout_s:.0f}s"
    return proc.returncode or 0, stdout.decode(errors="replace")
