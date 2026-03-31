"""Tests for the gateway-side overseer client."""

import asyncio
import json
import tempfile
from pathlib import Path

import pytest
from yeoman_gateway.ipc.overseer_client import OverseerClient


@pytest.mark.asyncio
async def test_ping_succeeds() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        sock_path = Path(tmpdir) / "overseer.sock"

        # Spin up a mock overseer socket
        async def mock_handler(reader, writer):
            line = await reader.readline()
            req = json.loads(line)
            if req.get("cmd") == "ping":
                writer.write(json.dumps({"status": "ok", "response": "pong"}).encode() + b"\n")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_unix_server(mock_handler, path=str(sock_path))
        try:
            client = OverseerClient(socket_path=sock_path)
            result = await client.ping()
            assert result == "pong"
        finally:
            server.close()
            await server.wait_closed()


@pytest.mark.asyncio
async def test_connection_failure_returns_error() -> None:
    client = OverseerClient(
        socket_path=Path("/tmp/nonexistent.sock"), max_retries=1, base_delay=0.01
    )
    result = await client.send("ping")
    assert result["status"] == "error"
    assert "connect" in result["message"].lower() or "no such file" in result["message"].lower()
