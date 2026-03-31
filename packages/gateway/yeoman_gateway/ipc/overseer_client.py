"""Client for querying the overseer via its Unix domain socket."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from loguru import logger


class OverseerClient:
    """Gateway-side client for the overseer Unix socket.

    Connects lazily on first use, retries with backoff on failure,
    no persistent connections (connect-per-command).
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

    async def send(self, cmd: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send a command to the overseer and return the response."""
        request = json.dumps({"cmd": cmd, "args": args or {}}).encode() + b"\n"

        for attempt in range(self._max_retries):
            try:
                reader, writer = await asyncio.open_unix_connection(str(self._path))
                try:
                    writer.write(request)
                    await writer.drain()
                    line = await asyncio.wait_for(reader.readline(), timeout=10.0)
                    if not line:
                        return {"status": "error", "message": "Empty response from overseer"}
                    return json.loads(line)
                finally:
                    writer.close()
                    await writer.wait_closed()
            except Exception as e:
                if attempt < self._max_retries - 1:
                    delay = self._base_delay * (2**attempt)
                    logger.debug(
                        "Overseer socket attempt {} failed: {}, retrying in {}s",
                        attempt + 1,
                        e,
                        delay,
                    )
                    await asyncio.sleep(delay)
                else:
                    logger.warning(
                        "Overseer socket unavailable after {} attempts: {}",
                        self._max_retries,
                        e,
                    )
                    return {"status": "error", "message": str(e)}

        return {"status": "error", "message": "Unreachable"}

    async def ping(self) -> str | None:
        """Ping the overseer. Returns 'pong' on success, None on failure."""
        result = await self.send("ping")
        if result.get("status") == "ok":
            return result.get("response")
        return None

    async def get_stats(self) -> dict[str, Any]:
        """Get overseer statistics."""
        return await self.send("get_stats")

    async def get_runbook_status(self, name: str | None = None) -> dict[str, Any]:
        """Get runbook status from the overseer."""
        args = {"name": name} if name else {}
        return await self.send("get_runbook_status", args)
