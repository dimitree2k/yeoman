"""Unix domain socket server — command channel to gateway."""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class OverseerSocket:
    path: Path
    stats_callback: Callable[[], dict[str, Any]]
    _server: asyncio.Server | None = field(default=None, init=False)

    async def start(self) -> None:
        if self.path.exists():
            self.path.unlink()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._server = await asyncio.start_unix_server(
            self._handle_client, path=str(self.path)
        )
        logger.info("Overseer socket listening on %s", self.path)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        if self.path.exists():
            self.path.unlink()

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    request = json.loads(line)
                    response = self._dispatch(request)
                except json.JSONDecodeError:
                    response = {"status": "error", "message": "Invalid JSON"}
                writer.write(json.dumps(response).encode() + b"\n")
                await writer.drain()
        except ConnectionResetError:
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    def _dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        cmd = request.get("cmd", "")
        if cmd == "ping":
            return {"status": "ok", "response": "pong"}
        elif cmd == "get_stats":
            return {"status": "ok", "response": self.stats_callback()}
        else:
            return {"status": "error", "message": f"Unknown command: {cmd}"}
