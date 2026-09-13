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


@pytest.mark.asyncio
async def test_a2a_invoke_round_trips_exact_arguments_over_real_socket(
    tmp_path: Path,
) -> None:
    received: dict[str, object] = {}

    async def handler(**kwargs: object) -> dict[str, object]:
        received.update(kwargs)
        return {"accepted": True}

    server = GatewaySocket(path=tmp_path / "gateway.sock")
    server.a2a_invoke_handler = handler
    await server.start()
    try:
        reader, writer = await asyncio.open_unix_connection(server.path)
        request = {
            "cmd": "a2a_invoke",
            "args": {
                "peer": "hermes",
                "skill": "whatsapp.send",
                "input": {"recipient": {"type": "group", "alias": "molty.python"}},
                "task_id": "task-1",
                "context_id": "context-1",
                "effect_id": "a2a-effect-1",
                "resolved_artifacts": [
                    {
                        "peer": "hermes",
                        "uri": "https://relay.example.test/artifacts/opaque",
                        "path": "/private/artifact.ogg",
                    }
                ],
            },
        }
        writer.write(json.dumps(request).encode() + b"\n")
        await writer.drain()
        response = json.loads(await reader.readline())
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()

    assert response == {"status": "ok", "response": {"accepted": True}}
    assert received == request["args"]


@pytest.mark.asyncio
async def test_a2a_invoke_sanitizes_handler_failures(tmp_path: Path) -> None:
    async def handler(**_kwargs: object) -> dict[str, object]:
        raise RuntimeError("private-contact@lid")

    server = GatewaySocket(
        path=tmp_path / "gateway.sock",
        a2a_invoke_handler=handler,
    )
    await server.start()
    try:
        reader, writer = await asyncio.open_unix_connection(server.path)
        writer.write(json.dumps({"cmd": "a2a_invoke", "args": {}}).encode() + b"\n")
        await writer.drain()
        response = json.loads(await reader.readline())
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()

    assert response == {
        "status": "error",
        "error": {
            "code": "IPC_HANDLER_FAILED",
            "message": "The local runtime could not process the request.",
            "retryable": False,
        },
    }
    assert "private-contact@lid" not in str(response)


@pytest.mark.asyncio
async def test_a2a_capabilities_round_trips_available_text_skill_over_real_socket(
    tmp_path: Path,
) -> None:
    async def handler() -> dict[str, object]:
        return {
            "skills": ["whatsapp.send"],
            "content_types": ["text"],
        }

    server = GatewaySocket(path=tmp_path / "gateway.sock")
    server.a2a_capabilities_handler = handler
    await server.start()
    try:
        reader, writer = await asyncio.open_unix_connection(server.path)
        writer.write(json.dumps({"cmd": "a2a_capabilities", "args": {}}).encode() + b"\n")
        await writer.drain()
        response = json.loads(await reader.readline())
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()

    assert response == {
        "status": "ok",
        "response": {
            "skills": ["whatsapp.send"],
            "content_types": ["text"],
        },
    }
    assert "voice" not in str(response)
