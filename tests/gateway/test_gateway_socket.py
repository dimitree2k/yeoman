# tests/gateway/test_gateway_socket.py
"""Tests for the gateway IPC socket server."""

import asyncio
import json
import tempfile
from pathlib import Path

import pytest
from yeoman_gateway.ipc.gateway_socket import GatewaySocket


@pytest.mark.asyncio
async def test_send_message_command() -> None:
    sent: list[dict] = []

    async def mock_send(channel: str, chat_id: str, content: str) -> dict:
        sent.append({"channel": channel, "chat_id": chat_id, "content": content})
        return {"status": "ok"}

    with tempfile.TemporaryDirectory() as tmpdir:
        sock_path = Path(tmpdir) / "gateway.sock"
        server = GatewaySocket(
            path=sock_path,
            send_message_handler=mock_send,
        )
        await server.start()
        try:
            reader, writer = await asyncio.open_unix_connection(str(sock_path))
            request = {
                "cmd": "send_message",
                "args": {"channel": "whatsapp", "chat_id": "123", "content": "hello"},
            }
            writer.write(json.dumps(request).encode() + b"\n")
            await writer.drain()
            line = await reader.readline()
            response = json.loads(line)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    assert response["status"] == "ok"
    assert len(sent) == 1
    assert sent[0]["content"] == "hello"


@pytest.mark.asyncio
async def test_a2a_send_command_forwards_logical_target() -> None:
    received: list[dict] = []

    async def mock_a2a_send(**kwargs: str) -> dict:
        received.append(kwargs)
        return {"target": kwargs["target"], "kind": kwargs["kind"], "response": "sent"}

    with tempfile.TemporaryDirectory() as tmpdir:
        sock_path = Path(tmpdir) / "gateway.sock"
        server = GatewaySocket(path=sock_path, a2a_delivery_handler=mock_a2a_send)
        await server.start()
        try:
            reader, writer = await asyncio.open_unix_connection(str(sock_path))
            request = {
                "cmd": "a2a_send",
                "args": {
                    "target": "Molty Python",
                    "kind": "voice",
                    "text": "Hallo Gruppe.",
                    "idempotency_key": "hermes-1",
                    "peer": "hermes",
                },
            }
            writer.write(json.dumps(request).encode() + b"\n")
            await writer.drain()
            line = await reader.readline()
            response = json.loads(line)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    assert response == {
        "status": "ok",
        "response": {"target": "Molty Python", "kind": "voice", "response": "sent"},
    }
    assert received == [
        {
            "target": "Molty Python",
            "kind": "voice",
            "text": "Hallo Gruppe.",
            "idempotency_key": "hermes-1",
            "peer": "hermes",
        }
    ]


@pytest.mark.asyncio
async def test_unknown_command_returns_error() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        sock_path = Path(tmpdir) / "gateway.sock"
        server = GatewaySocket(path=sock_path)
        await server.start()
        try:
            reader, writer = await asyncio.open_unix_connection(str(sock_path))
            writer.write(json.dumps({"cmd": "bogus"}).encode() + b"\n")
            await writer.drain()
            line = await reader.readline()
            response = json.loads(line)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    assert response["status"] == "error"
    assert "Unknown command" in response["message"]


@pytest.mark.asyncio
async def test_rate_limiting() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        sock_path = Path(tmpdir) / "gateway.sock"
        server = GatewaySocket(path=sock_path, rate_limit=2)
        await server.start()
        try:
            responses = []
            for _ in range(4):
                reader, writer = await asyncio.open_unix_connection(str(sock_path))
                writer.write(json.dumps({"cmd": "ping"}).encode() + b"\n")
                await writer.drain()
                line = await reader.readline()
                responses.append(json.loads(line))
                writer.close()
                await writer.wait_closed()
        finally:
            await server.stop()

    ok_count = sum(1 for r in responses if r["status"] == "ok")
    limited_count = sum(
        1 for r in responses if r["status"] == "error" and "rate" in r.get("message", "").lower()
    )
    assert ok_count == 2
    assert limited_count == 2
