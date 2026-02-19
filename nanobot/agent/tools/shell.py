"""Shell execution tool."""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.file_access import grants_are_active
from nanobot.config.schema import ExecIsolationConfig

if TYPE_CHECKING:
    from nanobot.agent.tools.exec_isolation import ExecSandboxManager, SandboxMount


class ExecTool(Tool):
    """Tool to execute shell commands."""

    def __init__(
        self,
        timeout: int = 60,
        working_dir: str | None = None,
        deny_patterns: list[str] | None = None,
        allow_patterns: list[str] | None = None,
        restrict_to_workspace: bool = False,
        allow_host_execution: bool = False,
        isolation_config: "ExecIsolationConfig | None" = None,
        *,
        extra_mounts: list["SandboxMount"] | None = None,
        grant_container_prefixes: list[str] | None = None,
    ):
        self.timeout = timeout
        self.working_dir = working_dir
        self.deny_patterns = deny_patterns or [
            r"\brm\s+-[rf]{1,2}\b",  # rm -r, rm -rf, rm -fr
            r"\bdel\s+/[fq]\b",  # del /f, del /q
            r"\brmdir\s+/s\b",  # rmdir /s
            r"\b(format|mkfs|diskpart)\b",  # disk operations
            r"\bdd\s+if=",  # dd
            r">\s*/dev/sd",  # write to disk
            r"\b(shutdown|reboot|poweroff)\b",  # system power
            r":\(\)\s*\{.*\};\s*:",  # fork bomb
        ]
        self.allow_patterns = allow_patterns or []
        self.restrict_to_workspace = restrict_to_workspace
        self.allow_host_execution = allow_host_execution
        self.isolation_config = isolation_config or ExecIsolationConfig()
        self._extra_mounts = list(extra_mounts or [])
        self._grant_container_prefixes = [
            p.rstrip("/") for p in (grant_container_prefixes or []) if p and p.startswith("/")
        ]

        self._session_key = "cli:default"
        self._sandbox_manager: "ExecSandboxManager | None" = None
        self._isolation_error: str | None = None
        self._init_isolation()

    @property
    def name(self) -> str:
        return "exec"

    @property
    def description(self) -> str:
        return "Execute a shell command and return its output. Use with caution."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The shell command to execute"},
                "working_dir": {
                    "type": "string",
                    "description": "Optional working directory for the command",
                },
            },
            "required": ["command"],
        }

    async def execute(self, command: str, working_dir: str | None = None, **kwargs: Any) -> str:
        cwd = working_dir or self.working_dir or os.getcwd()
        guard_error = self._guard_command(command, cwd)
        if guard_error:
            return guard_error

        if self._sandbox_manager:
            return await self._execute_isolated(command=command, cwd=cwd)

        if self.isolation_config.enabled:
            reason = self._isolation_error or "sandbox manager unavailable"
            return f"Error: Exec isolation unavailable: {reason}"

        if not self.allow_host_execution:
            return (
                "Error: Host exec is disabled by configuration "
                "(set tools.exec.allowHostExecution=true to enable unsafe host execution)"
            )

        return await self._execute_local(command=command, cwd=cwd)

    def set_session_context(self, session_key: str) -> None:
        """Bind exec calls to a session key for batch-session isolation."""
        self._session_key = session_key or "cli:default"

    def close(self) -> None:
        """Close isolation resources synchronously."""
        if self._sandbox_manager:
            self._sandbox_manager.close()

    async def aclose(self) -> None:
        """Close isolation resources asynchronously."""
        if self._sandbox_manager:
            await self._sandbox_manager.aclose()

    def _is_allowed_grant_path(self, raw_path: str) -> bool:
        if not raw_path.startswith("/"):
            return False
        if not self._grant_container_prefixes:
            return False
        if not grants_are_active():
            return False
        for prefix in self._grant_container_prefixes:
            if raw_path == prefix or raw_path.startswith(prefix + "/"):
                return True
        return False

    def _guard_command(self, command: str, cwd: str) -> str | None:
        """Best-effort safety guard for potentially destructive commands."""
        cmd = command.strip()
        lower = cmd.lower()

        for pattern in self.deny_patterns:
            if re.search(pattern, lower):
                return "Error: Command blocked by safety guard (dangerous pattern detected)"

        if self.allow_patterns:
            if not any(re.search(p, lower) for p in self.allow_patterns):
                return "Error: Command blocked by safety guard (not in allowlist)"

        if self.restrict_to_workspace:
            if "..\\" in cmd or "../" in cmd:
                return "Error: Command blocked by safety guard (path traversal detected)"

            cwd_path = Path(cwd).resolve()

            win_paths = re.findall(r"[A-Za-z]:\\[^\\\"']+", cmd)
            posix_paths = re.findall(r"/[^\s\"']+", cmd)

            for raw in win_paths + posix_paths:
                if self._is_allowed_grant_path(raw):
                    continue
                try:
                    p = Path(raw).resolve()
                except Exception:
                    continue
                if cwd_path not in p.parents and p != cwd_path:
                    return "Error: Command blocked by safety guard (path outside working dir)"

        return None

    def _init_isolation(self) -> None:
        """Initialize optional isolation manager based on config."""
        if not self.isolation_config.enabled:
            return

        if not self.working_dir:
            self._isolation_error = "working_dir is required when exec isolation is enabled"
            return

        from nanobot.agent.tools.exec_isolation import ExecSandboxManager

        allowlist_path = Path(self.isolation_config.allowlist_path).expanduser()
        try:
            self._sandbox_manager = ExecSandboxManager(
                workspace=Path(self.working_dir).expanduser().resolve(),
                max_containers=self.isolation_config.max_containers,
                idle_seconds=self.isolation_config.batch_session_idle_seconds,
                pressure_policy=self.isolation_config.pressure_policy,
                allowlist_path=allowlist_path,
                extra_mounts=self._extra_mounts,
            )
        except Exception as e:
            self._isolation_error = str(e)
            self._sandbox_manager = None
            if self.allow_host_execution:
                logger.warning(
                    "Exec isolation unavailable; host exec remains enabled via allow_host_execution=true: {}",
                    self._isolation_error,
                )
            else:
                logger.error(
                    "Exec isolation unavailable (host exec disabled): {}", self._isolation_error
                )

    async def _execute_local(self, command: str, cwd: str) -> str:
        try:
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )

            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=self.timeout)
            except asyncio.TimeoutError:
                process.kill()
                return f"Error: Command timed out after {self.timeout} seconds"

            output_parts = []

            if stdout:
                output_parts.append(stdout.decode("utf-8", errors="replace"))

            if stderr:
                stderr_text = stderr.decode("utf-8", errors="replace")
                if stderr_text.strip():
                    output_parts.append(f"STDERR:\n{stderr_text}")

            result = "\n".join(output_parts) if output_parts else "(no output)"
            if process.returncode != 0:
                result = f"{result}\n\nExit code: {process.returncode}"
            return self._truncate_result(result)

        except Exception as e:
            return f"Error executing command: {str(e)}"

    async def _execute_isolated(self, command: str, cwd: str) -> str:
        from nanobot.agent.tools.exec_isolation import (
            IsolationUnavailableError,
            SandboxExecutionError,
            SandboxPreemptedError,
            SandboxTimeoutError,
        )

        if not self._sandbox_manager:
            return f"Error: Exec isolation unavailable: {self._isolation_error or 'unknown error'}"

        try:
            result = await self._sandbox_manager.execute(
                session_key=self._session_key,
                command=command,
                host_cwd=cwd,
                timeout=self.timeout,
            )
        except SandboxTimeoutError:
            return f"Error: Command timed out after {self.timeout} seconds"
        except SandboxPreemptedError as e:
            return f"Error: Command interrupted due to sandbox preemption ({e})"
        except (IsolationUnavailableError, SandboxExecutionError) as e:
            return f"Error: Exec isolation unavailable: {str(e)}"
        except Exception as e:
            return f"Error executing command: {str(e)}"

        output = result.output if result.output else "(no output)"
        if result.exit_code != 0:
            output = f"{output}\n\nExit code: {result.exit_code}"
        return self._truncate_result(output)

    @staticmethod
    def _truncate_result(result: str) -> str:
        """Truncate very long outputs to stay within context budget."""
        max_len = 10000
        if len(result) > max_len:
            return result[:max_len] + f"\n... (truncated, {len(result) - max_len} more chars)"
        return result
