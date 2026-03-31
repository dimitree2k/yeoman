"""Client for sending commands to the gateway via its Unix domain socket."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class GatewayClient:
    """Overseer-side client for the gateway Unix socket.

    Connects lazily on first use, retries with backoff, no persistent connections.
    """

    def __init__(
        self,
        socket_path: Path,
        max_retries: int = 3,
        base_delay: float = 1.0,
    ):
        self._path = socket_path
        self._max_retries = max_retries
        self._base_delay = base_delay

    async def _send(self, cmd: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        request = json.dumps({"cmd": cmd, "args": args or {}}).encode() + b"\n"
        for attempt in range(self._max_retries):
            try:
                reader, writer = await asyncio.open_unix_connection(str(self._path))
                try:
                    writer.write(request)
                    await writer.drain()
                    line = await asyncio.wait_for(reader.readline(), timeout=10.0)
                    if not line:
                        return {"status": "error", "message": "Empty response"}
                    return json.loads(line)
                finally:
                    writer.close()
                    await writer.wait_closed()
            except Exception as e:
                if attempt < self._max_retries - 1:
                    delay = self._base_delay * (2**attempt)
                    logger.debug(
                        "Gateway socket attempt %d failed: %s, retrying in %ss",
                        attempt + 1,
                        e,
                        delay,
                    )
                    await asyncio.sleep(delay)
                else:
                    logger.warning(
                        "Gateway socket unavailable after %d attempts: %s", self._max_retries, e
                    )
                    return {"status": "error", "message": str(e)}
        return {"status": "error", "message": "Unreachable"}

    async def send_message(self, *, channel: str, chat_id: str, content: str) -> dict[str, Any]:
        return await self._send(
            "send_message", {"channel": channel, "chat_id": chat_id, "content": content}
        )

    async def trigger_agent_turn(
        self,
        *,
        prompt: str,
        session_key: str = "overseer:direct",
        channel: str = "cli",
        chat_id: str = "direct",
        model_profile: str | None = None,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {
            "prompt": prompt,
            "session_key": session_key,
            "channel": channel,
            "chat_id": chat_id,
        }
        if model_profile:
            args["model_profile"] = model_profile
        return await self._send("trigger_agent_turn", args)

    async def publish_event(self, *, kind: str, detail: dict[str, Any]) -> dict[str, Any]:
        return await self._send("publish_event", {"kind": kind, "detail": detail})

    async def notify_runbook_result(
        self,
        *,
        runbook_name: str,
        status: str,
        summary: str,
        deliver_to: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {"runbook_name": runbook_name, "status": status, "summary": summary}
        if deliver_to:
            args["deliver_to"] = deliver_to
        return await self._send("notify_runbook_result", args)
