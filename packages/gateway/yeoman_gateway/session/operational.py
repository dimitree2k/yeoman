"""Operational routing and boundaries; tool audit is never conversation history."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from yeoman_shared.utils.helpers import get_operational_store_path


class OperationalSessions:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS boundaries (
                channel TEXT NOT NULL, chat_id TEXT NOT NULL, at_ms INTEGER NOT NULL,
                PRIMARY KEY (channel, chat_id));
            CREATE TABLE IF NOT EXISTS routes (
                session_key TEXT PRIMARY KEY, channel TEXT NOT NULL,
                chat_id TEXT NOT NULL, thread_id TEXT);
            CREATE TABLE IF NOT EXISTS tool_traces (
                session_key TEXT NOT NULL, turn_id TEXT NOT NULL,
                tool_call_id TEXT NOT NULL, tool_name TEXT NOT NULL,
                arguments TEXT NOT NULL, result TEXT NOT NULL, at_ms INTEGER NOT NULL,
                PRIMARY KEY (session_key, turn_id, tool_call_id));
            CREATE TRIGGER IF NOT EXISTS tool_trace_no_update BEFORE UPDATE ON tool_traces
                BEGIN SELECT RAISE(ABORT, 'tool audit is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS tool_trace_no_delete BEFORE DELETE ON tool_traces
                BEGIN SELECT RAISE(ABORT, 'tool audit is append-only'); END;
        """)

    def boundary(self, *, channel: str, chat_id: str) -> int | None:
        row = self._db.execute("SELECT at_ms FROM boundaries WHERE channel=? AND chat_id=?",
                               (channel, chat_id)).fetchone()
        return row[0] if row else None

    def set_boundary(self, *, channel: str, chat_id: str, at_ms: int) -> None:
        if not channel or not chat_id or type(at_ms) is not int:
            raise ValueError("boundary requires explicit channel/chat and integer milliseconds")
        with self._db:
            self._db.execute(
                "INSERT INTO boundaries VALUES (?,?,?) ON CONFLICT(channel,chat_id)"
                " DO UPDATE SET at_ms=MAX(at_ms,excluded.at_ms)", (channel, chat_id, at_ms))

    def set_route(self, *, session_key: str, channel: str, chat_id: str,
                  thread_id: str | None) -> None:
        if not session_key or not channel or not chat_id:
            raise ValueError("route requires explicit session/channel/chat")
        values = (session_key, channel, chat_id, thread_id)
        with self._db:
            row = self._db.execute("SELECT * FROM routes WHERE session_key=?",
                                   (session_key,)).fetchone()
            if row is not None and row != values:
                raise ValueError("session route conflicts with existing route")
            self._db.execute("INSERT OR IGNORE INTO routes VALUES (?,?,?,?)", values)

    def record_tool_trace(self, *, session_key: str, turn_id: str, tool_call_id: str,
                          tool_name: str, arguments: str, result: str, at_ms: int) -> None:
        if not all((session_key, turn_id, tool_call_id, tool_name)) or type(at_ms) is not int:
            raise ValueError("tool trace requires session/turn/call/name and integer milliseconds")
        values = (session_key, turn_id, tool_call_id, tool_name, arguments, result, at_ms)
        with self._db:
            row = self._db.execute(
                "SELECT * FROM tool_traces WHERE session_key=? AND turn_id=? AND tool_call_id=?",
                values[:3]).fetchone()
            if row is not None and row != values:
                raise ValueError("tool trace conflicts with existing audit entry")
            self._db.execute("INSERT OR IGNORE INTO tool_traces VALUES (?,?,?,?,?,?,?)", values)

    def close(self) -> None:
        self._db.close()


def import_session_boundaries(source: Path, target: OperationalSessions) -> dict[str, int]:
    """Import final isolated JSONL copies; validate the whole inventory before writing."""
    source = source.resolve(strict=True)
    runtime_data = get_operational_store_path("session_metadata", create=False).parents[1]
    for protected in (runtime_data, Path("/home/dm/.yeoman/data")):
        if source.is_relative_to(protected.resolve()):
            raise ValueError("session boundary import requires an isolated snapshot")
    paths = sorted(source.glob("*.jsonl")) if source.is_dir() else [source]
    inventory: dict[str, int] = {}
    routes: dict[str, tuple[str, str, str | None]] = {}
    for path in paths:
        if path.is_symlink():
            raise ValueError("session snapshot must not contain symlinks")
        rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        markers = [row for row in rows if row.get("role") == "session_boundary"]
        if not markers:
            continue
        metadata = next((row.get("metadata", {}) for row in rows
                         if row.get("_type") == "metadata"), {})
        channel, chat_id = metadata.get("channel"), metadata.get("chat_id")
        thread_id = metadata.get("thread_id")
        if not channel or not chat_id:
            channel, separator, chat_id = path.stem.partition("_")
            # Legacy filenames lose colons; thread routes need an explicit inventory.
            if not separator or "_thread_" in chat_id or "_" in chat_id:
                raise ValueError("ambiguous legacy session route needs explicit metadata")
        if not isinstance(channel, str) or not isinstance(chat_id, str):
            raise ValueError("invalid session route")
        moments = []
        for row in markers:
            moment = datetime.fromisoformat(row["timestamp"])
            # Legacy Session used local naive datetimes; preserve that timezone meaning.
            moments.append(int(moment.timestamp() * 1000))
        key = f"{channel}:{chat_id}"
        inventory[key] = max(inventory.get(key, max(moments)), max(moments))
        routes[key] = (channel, chat_id, thread_id)
    for key, at_ms in inventory.items():
        channel, chat_id, thread_id = routes[key]
        target.set_route(session_key=key, channel=channel, chat_id=chat_id, thread_id=thread_id)
        target.set_boundary(channel=channel, chat_id=chat_id, at_ms=at_ms)
    return inventory
