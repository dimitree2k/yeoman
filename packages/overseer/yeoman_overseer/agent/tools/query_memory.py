"""query_memory tool — authorized Knowledge search through Gateway."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from yeoman_overseer.agent.tools import ToolContext


def execute(args: dict[str, Any], ctx: ToolContext) -> str:
    import json
    import socket
    chat_id = args.get('chat_id')
    if not isinstance(chat_id, str) or not chat_id:
        return '[query_memory] unavailable: explicit authorized chat scope required'
    gateway = getattr(ctx, 'gateway_socket_path', None) or ctx.yeoman_home / 'run' / 'gateway.sock'
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(30)
            client.connect(str(gateway))
            client.sendall(json.dumps({'cmd': 'knowledge_read', 'args': {
                'chat_id': chat_id, 'query': args['query'], 'limit': args.get('limit', 10)}}).encode() + b'\n')
            with client.makefile('rb') as response:
                result = json.loads(response.readline(1024 * 1024))
        return json.dumps(result)
    except (OSError, ValueError):
        return '[query_memory] unavailable: Gateway authorized Knowledge read failed'
