"""query_db tool — read-only SQLite access via mode=ro URI."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from yeoman_overseer.agent.tools import ToolContext


def execute(args: dict[str, Any], ctx: ToolContext) -> str:
    db_path = Path(args["db_path"]).expanduser()
    query = args["query"]

    if not db_path.exists():
        return f"[query_db] ERROR: database not found: {db_path}"

    uri = f"file:{db_path}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(query).fetchall()
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        return f"[query_db] ERROR: {exc}"

    if not rows:
        return "[query_db] (no rows)"
    headers = rows[0].keys()
    lines = [" | ".join(str(r[h]) for h in headers) for r in rows]
    return "\n".join([" | ".join(headers), *lines])
