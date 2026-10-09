"""Unix domain socket server — receives commands from the overseer."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import struct
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

from yeoman_gateway.history.control import MAX_IPC_REQUEST_BYTES, validate_control


@dataclass
class GatewaySocket:
    """Gateway-side IPC socket server.

    Receives commands from the overseer process via a Unix domain socket.
    Protocol: line-delimited JSON-RPC (same as overseer socket).
    """

    path: Path
    send_message_handler: Callable[..., Awaitable[dict]] | None = None
    trigger_agent_turn_handler: Callable[..., Awaitable[dict]] | None = None
    owner_turn_handler: Callable[..., Awaitable[dict]] | None = None
    a2a_delivery_handler: Callable[..., Awaitable[dict]] | None = None
    a2a_invoke_handler: Callable[..., Awaitable[dict[str, Any]]] | None = None
    a2a_capabilities_handler: Callable[[], Awaitable[dict[str, Any]]] | None = None
    publish_event_handler: Callable[..., Awaitable[dict]] | None = None
    get_session_state_handler: Callable[..., Awaitable[dict]] | None = None
    knowledge_statements_handler: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None
    knowledge_accounting_handler: Callable[[], Awaitable[dict[str, Any]]] | None = None
    persona_evolution_read_handler: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None
    history_chats_handler: Callable[[], Awaitable[dict[str, Any]]] | None = None
    knowledge_read_handler: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None
    history_read_handler: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None
    history_control_handler: Callable[[str, Mapping[str, Any]], Awaitable[dict[str, Any]]] | None = None
    rate_limit: int = 10  # commands per second
    _server: asyncio.Server | None = field(default=None, init=False)
    _request_timestamps: list[float] = field(default_factory=list, init=False)

    async def start(self) -> None:
        if self.path.exists():
            self.path.unlink()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._server = await asyncio.start_unix_server(self._handle_client, path=str(self.path), limit=MAX_IPC_REQUEST_BYTES)
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

    def _peer_is_owner(self, writer: asyncio.StreamWriter) -> bool:
        try:
            peer = writer.get_extra_info('socket')
            credentials = peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i'))
            return struct.unpack('3i', credentials)[1] == os.getuid()
        except (AttributeError, OSError, struct.error):
            return False

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            while True:
                oversized = False
                try:
                    line = await reader.readline()
                    if not line:
                        break
                    oversized = len(line) > MAX_IPC_REQUEST_BYTES
                except ValueError:
                    oversized = True
                if oversized:
                    writer.write(b'{"status":"error","code":"REQUEST_TOO_LARGE"}\n')
                    await writer.drain()
                    break
                try:
                    request = json.loads(line)
                    if (not isinstance(request, dict) or not isinstance(request.get('cmd'), str) or
                            not isinstance(request.get('args', {}), dict)):
                        raise ValueError('invalid frame')
                    if request['cmd'] in {'history_control', 'history_read', 'knowledge_read', 'history_chats', 'persona_evolution_read', 'knowledge_accounting', 'knowledge_statements'} and not self._peer_is_owner(writer):
                        response = {'status': 'error', 'code': 'OWNER_REQUIRED'}
                    elif not self._check_rate_limit():
                        response = {"status": "error", "message": "Rate limit exceeded"}
                    else:
                        response = await self._dispatch(request)
                except (ValueError, UnicodeError):
                    response = {"status": "error", "code": "INVALID_REQUEST", "message": "Invalid JSON"}
                writer.write(json.dumps(response).encode() + b"\n")
                await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    async def _dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        cmd = request.get("cmd", "")
        args = request.get("args", {})

        if cmd == 'knowledge_statements':
            fields = {'list': {'action', 'limit'}, 'show': {'action', 'statement_id', 'content'}, 'erase': {'action', 'statement_id'}}
            action = args.get('action')
            if set(request) != {'cmd', 'args'} or not isinstance(action, str) or action not in fields or set(args) != fields[action]:
                return {'status': 'error', 'code': 'INVALID_READ'}
            if action == 'list' and (type(args['limit']) is not int or not 1 <= args['limit'] <= 100) or action != 'list' and (not isinstance(args['statement_id'], str) or not args['statement_id']) or action == 'show' and type(args['content']) is not bool:
                return {'status': 'error', 'code': 'INVALID_READ'}
            if self.knowledge_statements_handler is None:
                return {'status': 'disabled'}
            try:
                return await self.knowledge_statements_handler(args)
            except Exception:
                return {'status': 'paused', 'code': 'KNOWLEDGE_READ_UNAVAILABLE'}

        if cmd == 'knowledge_accounting':
            if set(request) != {'cmd', 'args'} or args:
                return {'status': 'error', 'code': 'INVALID_READ'}
            if self.knowledge_accounting_handler is None:
                return {'status': 'disabled'}
            try:
                return await self.knowledge_accounting_handler()
            except Exception:
                return {'status': 'paused', 'code': 'KNOWLEDGE_READ_UNAVAILABLE'}

        if cmd == 'persona_evolution_read':
            if set(request) != {'cmd', 'args'} or set(args) != {'persona_file', 'window_days', 'limit'} or not isinstance(args['persona_file'], str) or type(args['window_days']) is not int or not 1 <= args['window_days'] <= 90 or type(args['limit']) is not int or not 1 <= args['limit'] <= 100:
                return {'status': 'error', 'code': 'INVALID_READ'}
            if self.persona_evolution_read_handler is None:
                return {'status': 'disabled'}
            try:
                return await self.persona_evolution_read_handler(args)
            except Exception:
                return {'status': 'paused', 'code': 'HISTORY_READ_UNAVAILABLE'}

        if cmd == 'history_chats':
            if set(request) != {'cmd', 'args'} or args:
                return {'status': 'error', 'code': 'INVALID_READ'}
            if self.history_chats_handler is None:
                return {'status': 'disabled'}
            try:
                return await self.history_chats_handler()
            except Exception:
                return {'status': 'paused', 'code': 'HISTORY_READ_UNAVAILABLE'}

        if cmd == 'knowledge_read':
            if set(request) != {'cmd', 'args'} or set(args) != {'chat_id', 'query', 'limit'} or not isinstance(args['chat_id'], str) or not args['chat_id'] or not isinstance(args['query'], str) or type(args['limit']) is not int or not 1 <= args['limit'] <= 500:
                return {'status': 'error', 'code': 'INVALID_READ'}
            if self.knowledge_read_handler is None:
                return {'status': 'disabled'}
            try:
                return await self.knowledge_read_handler(args)
            except Exception:
                return {'status': 'paused', 'code': 'KNOWLEDGE_READ_UNAVAILABLE'}

        if cmd == 'history_read':
            from yeoman_gateway.history.export import validate_read_args
            try:
                if set(request) != {'cmd', 'args'}:
                    raise ValueError('invalid request')
                validate_read_args(args)
                if self.history_read_handler is None:
                    return {'status': 'disabled'}
                return await self.history_read_handler(args)
            except ValueError:
                return {'status': 'error', 'code': 'INVALID_READ'}
            except Exception:
                return {'status': 'paused', 'code': 'HISTORY_READ_UNAVAILABLE'}

        if cmd == 'history_control':
            try:
                if set(request) != {'cmd', 'args'} or 'operation' not in args:
                    raise ValueError('INVALID_OPERATION')
                operation = args['operation']
                arguments = {key: value for key, value in args.items() if key != 'operation'}
                validate_control(operation, arguments)
                if self.history_control_handler is None:
                    return {'status': 'disabled'}
                task: asyncio.Future[dict[str, Any]] = asyncio.ensure_future(self.history_control_handler(operation, arguments))
                return await asyncio.shield(task)
            except ValueError:
                return {'status': 'error', 'code': 'INVALID_OPERATION'}
            except Exception:
                return {'status': 'error', 'code': 'HISTORY_CONTROL_FAILED'}

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

        if cmd == "owner_turn" and self.owner_turn_handler:
            try:
                result = await self.owner_turn_handler(
                    prompt=args.get("prompt", ""),
                    session_key=args.get("session_key"),
                    chat_id=args.get("chat_id", ""),
                    post_to_whatsapp=args.get("post_to_whatsapp") is True,
                    actor_principal=args.get("actor_principal"),
                    peer=args.get("peer"),
                )
                return {"status": "ok", "response": result}
            except Exception as e:
                return {"status": "error", "message": str(e)}

        if cmd == "a2a_send" and self.a2a_delivery_handler:
            try:
                result = await self.a2a_delivery_handler(
                    target=args.get("target", ""),
                    kind=args.get("kind", ""),
                    text=args.get("text", ""),
                    idempotency_key=args.get("idempotency_key", ""),
                    peer=args.get("peer", ""),
                )
                return {"status": "ok", "response": result}
            except Exception as e:
                return {"status": "error", "message": str(e)}

        if cmd == "a2a_invoke" and self.a2a_invoke_handler:
            try:
                result = await self.a2a_invoke_handler(
                    peer=args.get("peer", ""),
                    skill=args.get("skill", ""),
                    input=args.get("input", {}),
                    task_id=args.get("task_id", ""),
                    context_id=args.get("context_id", ""),
                    effect_id=args.get("effect_id", ""),
                    resolved_artifacts=args.get("resolved_artifacts", []),
                )
                return {"status": "ok", "response": result}
            except Exception:
                return {
                    "status": "error",
                    "error": {
                        "code": "IPC_HANDLER_FAILED",
                        "message": "The local runtime could not process the request.",
                        "retryable": False,
                    },
                }

        if cmd == "a2a_capabilities" and self.a2a_capabilities_handler:
            try:
                result = await self.a2a_capabilities_handler()
                return {"status": "ok", "response": result}
            except Exception:
                return {
                    "status": "error",
                    "error": {
                        "code": "IPC_HANDLER_FAILED",
                        "message": "The local runtime could not process the request.",
                        "retryable": False,
                    },
                }

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
