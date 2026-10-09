"""query_memory tool — FTS search on memory.db."""
from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from yeoman_overseer.agent.tools import ToolContext


def execute(args: dict[str, Any], ctx: ToolContext) -> str:
    if getattr(ctx, 'history_selected', False) or getattr(ctx, 'legacy_history_disabled', False):
        import json
        import socket
        chat_id = args.get('chat_id')
        if not isinstance(chat_id, str) or not chat_id:
            return '[query_memory] unavailable: explicit authorized chat scope required'
        gateway = getattr(ctx, 'gateway_socket_path', ctx.yeoman_home / 'run' / 'gateway.sock')
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
    query = args["query"]
    limit = int(args.get("limit", 10))
    db_path = ctx.memory_db or (ctx.yeoman_home / "data" / "memory" / "memory.db")

    if not db_path.exists():
        return "[query_memory] ERROR: memory.db not found"

    uri = f"file:{db_path}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        try:
            # FTS search via memory2_nodes_fts if available, else fallback
            rows = conn.execute(
                """
                SELECT n.id, n.content, n.salience, n.created_at
                FROM memory2_nodes n
                JOIN memory2_nodes_fts fts ON n.id = fts.rowid
                WHERE memory2_nodes_fts MATCH ?
                ORDER BY rank LIMIT ?
                """,
                (query, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            # FTS table not available — fallback to LIKE
            rows = conn.execute(
                "SELECT id, content, salience, created_at FROM memory2_nodes WHERE content LIKE ? LIMIT ?",
                (f"%{query}%", limit),
            ).fetchall()
        finally:
            conn.close()
    except Exception as exc:
        return f"[query_memory] ERROR: {exc}"

    if not rows:
        return "[query_memory] (no results)"
    return "\n".join(
        f"[{r['created_at']}] salience={r['salience']:.2f}: {r['content'][:200]}"
        for r in rows
    )
