"""Tests for the overseer-side gateway client."""

import asyncio
import json
import tempfile
from pathlib import Path

import pytest
from yeoman_overseer.gateway.client import GatewayClient


@pytest.mark.asyncio
async def test_send_message() -> None:
    received_cmds: list[dict] = []

    with tempfile.TemporaryDirectory() as tmpdir:
        sock_path = Path(tmpdir) / "gateway.sock"

        async def mock_handler(reader, writer):
            line = await reader.readline()
            req = json.loads(line)
            received_cmds.append(req)
            writer.write(json.dumps({"status": "ok", "response": {}}).encode() + b"\n")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_unix_server(mock_handler, path=str(sock_path))
        try:
            client = GatewayClient(socket_path=sock_path)
            result = await client.send_message(
                channel="whatsapp", chat_id="owner", content="Alert!"
            )
            assert result["status"] == "ok"
            assert received_cmds[0]["cmd"] == "send_message"
            assert received_cmds[0]["args"]["content"] == "Alert!"
        finally:
            server.close()
            await server.wait_closed()


@pytest.mark.asyncio
async def test_trigger_agent_turn() -> None:
    received_cmds: list[dict] = []

    with tempfile.TemporaryDirectory() as tmpdir:
        sock_path = Path(tmpdir) / "gateway.sock"

        async def mock_handler(reader, writer):
            line = await reader.readline()
            received_cmds.append(json.loads(line))
            writer.write(json.dumps({"status": "ok", "response": "done"}).encode() + b"\n")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_unix_server(mock_handler, path=str(sock_path))
        try:
            client = GatewayClient(socket_path=sock_path)
            result = await client.trigger_agent_turn(
                prompt="Check disk", session_key="overseer:ops"
            )
            assert result["status"] == "ok"
            assert received_cmds[0]["cmd"] == "trigger_agent_turn"
        finally:
            server.close()
            await server.wait_closed()
