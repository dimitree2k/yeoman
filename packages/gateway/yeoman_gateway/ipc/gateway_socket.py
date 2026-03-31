"""Unix domain socket server — receives commands from the overseer."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger


@dataclass
class GatewaySocket:
    """Gateway-side IPC socket server.

    Receives commands from the overseer process via a Unix domain socket.
    Protocol: line-delimited JSON-RPC (same as overseer socket).
    """

    path: Path
    send_message_handler: Callable[..., Awaitable[dict]] | None = None
    trigger_agent_turn_handler: Callable[..., Awaitable[dict]] | None = None
    publish_event_handler: Callable[..., Awaitable[dict]] | None = None
    get_session_state_handler: Callable[..., Awaitable[dict]] | None = None
    rate_limit: int = 10  # commands per second
    _server: asyncio.Server | None = field(default=None, init=False)
    _request_timestamps: list[float] = field(default_factory=list, init=False)

    async def start(self) -> None:
        if self.path.exists():
            self.path.unlink()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._server = await asyncio.start_unix_server(self._handle_client, path=str(self.path))
        self.path.chmod(0o600)
        logger.info("Gateway IPC socket listening on {}", self.path)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        if self.path.exists():
            self.path.unlink()

    def _check_rate_limit(self) -> bool:
        now = time.monotonic()
        cutoff = now - 1.0
        self._request_timestamps = [t for t in self._request_timestamps if t > cutoff]
        if len(self._request_timestamps) >= self.rate_limit:
            return False
        self._request_timestamps.append(now)
        return True

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
                    if not self._check_rate_limit():
                        response = {"status": "error", "message": "Rate limit exceeded"}
                    else:
                        response = await self._dispatch(request)
                except json.JSONDecodeError:
                    response = {"status": "error", "message": "Invalid JSON"}
                writer.write(json.dumps(response).encode() + b"\n")
                await writer.drain()
        except ConnectionResetError:
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    async def _dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        cmd = request.get("cmd", "")
        args = request.get("args", {})

        if cmd == "ping":
            return {"status": "ok", "response": "pong"}

        if cmd == "send_message" and self.send_message_handler:
            try:
                result = await self.send_message_handler(
                    channel=args.get("channel", ""),
                    chat_id=args.get("chat_id", ""),
                    content=args.get("content", ""),
                )
                return {"status": "ok", "response": result}
            except Exception as e:
                return {"status": "error", "message": str(e)}

        if cmd == "trigger_agent_turn" and self.trigger_agent_turn_handler:
            try:
                result = await self.trigger_agent_turn_handler(
                    prompt=args.get("prompt", ""),
                    session_key=args.get("session_key", "overseer:direct"),
                    channel=args.get("channel", "cli"),
                    chat_id=args.get("chat_id", "direct"),
                    model_profile=args.get("model_profile"),
                )
                return {"status": "ok", "response": result}
            except Exception as e:
                return {"status": "error", "message": str(e)}

        if cmd == "publish_event" and self.publish_event_handler:
            try:
                result = await self.publish_event_handler(
                    kind=args.get("kind", ""),
                    detail=args.get("detail", {}),
                )
                return {"status": "ok", "response": result}
            except Exception as e:
                return {"status": "error", "message": str(e)}

        if cmd == "get_session_state" and self.get_session_state_handler:
            try:
                result = await self.get_session_state_handler(
                    session_key=args.get("session_key", ""),
                )
                return {"status": "ok", "response": result}
            except Exception as e:
                return {"status": "error", "message": str(e)}

        return {"status": "error", "message": f"Unknown command: {cmd}"}
