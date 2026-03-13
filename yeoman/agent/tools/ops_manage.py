"""Service management tool with 4-digit confirmation codes."""

from __future__ import annotations

import secrets
import time
from pathlib import Path
from typing import Any, NamedTuple

from yeoman.agent.tools.base import Tool
from yeoman.utils.process import pid_alive, read_pid_file

_TTL_SECONDS = 120  # 2 minutes


class _PendingConfirmation(NamedTuple):
    action: str
    service: str
    code: str
    expires_at: float


class OpsManageTool(Tool):
    """Manage gateway/bridge services (restart, stop) with confirmation codes."""

    def __init__(self) -> None:
        self._channel: str = ""
        self._chat_id: str = ""
        self._pending: dict[tuple[str, str], _PendingConfirmation] = {}

    def set_context(self, channel: str, chat_id: str) -> None:
        self._channel = channel
        self._chat_id = chat_id

    @property
    def name(self) -> str:
        return "ops_manage"

    @property
    def description(self) -> str:
        return (
            "Manage services (restart/stop gateway or bridge). "
            "Requires a 4-digit confirmation code. "
            "First call with action='restart'/'stop' to get a code, "
            "then call with action='confirm' and the code to execute."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["restart", "stop", "confirm"],
                    "description": "Action: 'restart'/'stop' to request, 'confirm' to execute.",
                },
                "service": {
                    "type": "string",
                    "enum": ["gateway", "bridge"],
                    "description": "Target service (required for restart/stop).",
                },
                "code": {
                    "type": "string",
                    "description": "4-digit confirmation code (required for confirm).",
                },
            },
            "required": ["action"],
        }

    async def execute(self, **kwargs: Any) -> str:
        action = kwargs.get("action", "")
        if action in ("restart", "stop"):
            return self._request_confirmation(action, kwargs.get("service", ""))
        if action == "confirm":
            return await self._confirm(kwargs.get("code", ""))
        return f"Unknown action: {action}"

    def _chat_key(self) -> tuple[str, str]:
        return (self._channel, self._chat_id)

    def _request_confirmation(self, action: str, service: str) -> str:
        if service not in ("gateway", "bridge"):
            return "Error: 'service' must be 'gateway' or 'bridge'."

        # Pre-action state validation
        pid_path = Path("~/.yeoman/run").expanduser() / (
            "gateway.pid" if service == "gateway" else "whatsapp-bridge.pid"
        )
        pid = read_pid_file(pid_path)
        is_running = pid is not None and pid_alive(pid)
        if action == "stop" and not is_running:
            return f"{service.title()} is not running — nothing to stop."
        if action == "restart" and not is_running:
            return f"{service.title()} is not running. Use the CLI to start it."

        code = str(1000 + secrets.randbelow(9000))  # 1000-9999
        self._pending[self._chat_key()] = _PendingConfirmation(
            action=action,
            service=service,
            code=code,
            expires_at=time.time() + _TTL_SECONDS,
        )
        return f"To confirm {action} of {service}, reply with code: {code}. Expires in 2 minutes."

    async def _confirm(self, code: str) -> str:
        key = self._chat_key()
        pending = self._pending.get(key)
        if pending is None:
            return "No pending confirmation for this chat."
        if time.time() > pending.expires_at:
            del self._pending[key]
            return "Confirmation code has expired. Please request the action again."
        if pending.code != code.strip():
            return "Invalid confirmation code."

        action, service = pending.action, pending.service
        del self._pending[key]
        return await self._execute_action(action, service)

    async def _execute_action(self, action: str, service: str) -> str:
        if service == "bridge":
            return await self._manage_bridge(action)
        if service == "gateway":
            return self._manage_gateway(action)
        return f"Unknown service: {service}"

    async def _manage_bridge(self, action: str) -> str:
        try:
            import asyncio

            from yeoman.channels.whatsapp_runtime import WhatsAppRuntimeManager
            from yeoman.config.loader import load_config

            config = load_config()
            runtime = WhatsAppRuntimeManager(config=config)

            if action == "stop":
                stopped = await asyncio.to_thread(runtime.stop_bridge)
                return f"Bridge stopped ({stopped} process(es) terminated)."
            if action == "restart":
                status = await asyncio.to_thread(runtime.restart_bridge)
                state = "running" if status.running else "stopped"
                return f"Bridge restarted. Status: {state} (pid={status.pids})."
        except Exception as exc:
            return f"Bridge {action} failed: {exc}"
        return f"Unknown bridge action: {action}"

    def _manage_gateway(self, action: str) -> str:
        import os
        import subprocess
        import sys

        current_pid = os.getpid()
        yeoman_bin = sys.argv[0] if sys.argv else "yeoman"

        if action == "stop":
            cmd = f"sleep 1; kill {current_pid}; sleep 4; kill -9 {current_pid} 2>/dev/null"
            subprocess.Popen(
                ["bash", "-c", cmd],
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return "Gateway will shut down in ~1 second. You'll need to start it manually."

        if action == "restart":
            try:
                from yeoman.config.loader import load_config

                port = load_config().gateway.port
            except Exception:
                port = 18790
            cmd = (
                f"sleep 1; kill {current_pid}; sleep 4; kill -9 {current_pid} 2>/dev/null; "
                f"sleep 1; {yeoman_bin} gateway start --daemon --port {port}"
            )
            subprocess.Popen(
                ["bash", "-c", cmd],
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return "Gateway restart initiated. I'll be offline for a few seconds while it restarts."

        return f"Unknown gateway action: {action}"
