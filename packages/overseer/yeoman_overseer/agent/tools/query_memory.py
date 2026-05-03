"""query_memory tool — FTS search on memory.db."""
from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from yeoman_overseer.agent.tools import ToolContext


def execute(args: dict[str, Any], ctx: ToolContext) -> str:
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
