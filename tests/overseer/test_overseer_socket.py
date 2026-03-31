"""Tests for expanded overseer socket commands."""

import asyncio
import json
import tempfile
from pathlib import Path

import pytest
from yeoman_overseer.socket.server import OverseerSocket


@pytest.mark.asyncio
async def test_get_runbook_status() -> None:
    def mock_stats() -> dict:
        return {"uptime": 100}

    with tempfile.TemporaryDirectory() as tmpdir:
        sock_path = Path(tmpdir) / "overseer.sock"
        server = OverseerSocket(
            path=sock_path,
            stats_callback=mock_stats,
            runbook_status_callback=lambda name: {
                "runbooks": [{"name": "test", "last_status": "ok"}]
            },
        )
        await server.start()
        try:
            reader, writer = await asyncio.open_unix_connection(str(sock_path))
            writer.write(json.dumps({"cmd": "get_runbook_status", "args": {}}).encode() + b"\n")
            await writer.drain()
            line = await reader.readline()
            response = json.loads(line)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    assert response["status"] == "ok"
    assert "runbooks" in response["response"]
