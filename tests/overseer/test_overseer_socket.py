"""Tests for the Unix domain socket server."""
from __future__ import annotations
import asyncio
import json
from pathlib import Path
import pytest
from yeoman_overseer.socket.server import OverseerSocket

@pytest.mark.asyncio
async def test_ping_pong(tmp_path: Path) -> None:
    sock_path = tmp_path / "test.sock"
    server = OverseerSocket(sock_path, stats_callback=lambda: {"uptime": 42})
    await server.start()
    try:
        reader, writer = await asyncio.open_unix_connection(str(sock_path))
        writer.write(json.dumps({"cmd": "ping"}).encode() + b"\n")
        await writer.drain()
        data = await reader.readline()
        resp = json.loads(data)
        assert resp["status"] == "ok"
        assert resp["response"] == "pong"
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()

@pytest.mark.asyncio
async def test_get_stats(tmp_path: Path) -> None:
    sock_path = tmp_path / "test.sock"
    server = OverseerSocket(sock_path, stats_callback=lambda: {"messages": 100})
    await server.start()
    try:
        reader, writer = await asyncio.open_unix_connection(str(sock_path))
        writer.write(json.dumps({"cmd": "get_stats"}).encode() + b"\n")
        await writer.drain()
        data = await reader.readline()
        resp = json.loads(data)
        assert resp["status"] == "ok"
        assert resp["response"]["messages"] == 100
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()

@pytest.mark.asyncio
async def test_unknown_command(tmp_path: Path) -> None:
    sock_path = tmp_path / "test.sock"
    server = OverseerSocket(sock_path, stats_callback=lambda: {})
    await server.start()
    try:
        reader, writer = await asyncio.open_unix_connection(str(sock_path))
        writer.write(json.dumps({"cmd": "invalid"}).encode() + b"\n")
        await writer.drain()
        data = await reader.readline()
        resp = json.loads(data)
        assert resp["status"] == "error"
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()

@pytest.mark.asyncio
async def test_stale_socket_cleanup(tmp_path: Path) -> None:
    sock_path = tmp_path / "test.sock"
    sock_path.touch()
    server = OverseerSocket(sock_path, stats_callback=lambda: {})
    await server.start()
    await server.stop()
