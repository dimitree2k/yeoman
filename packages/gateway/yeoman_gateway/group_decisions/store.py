"""SQLite store for group decisions: one database, every row scoped by chat_id.

Every query that reads or changes a row names the chat it runs in, so a turn in one
group can never see or change another group's decisions.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path

SCHEMA_VERSION = 1
DONE_RETENTION_MS = 90 * 24 * 60 * 60 * 1000
OPEN_RETENTION_MS = 180 * 24 * 60 * 60 * 1000

_DDL = (
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS group_decisions (
        id TEXT PRIMARY KEY,
        chat_id TEXT NOT NULL,
        text TEXT NOT NULL,
        created_by TEXT NOT NULL,
        due_at_ms INTEGER,
        source_message_ids TEXT NOT NULL DEFAULT '[]',
        created_at_ms INTEGER NOT NULL,
        updated_at_ms INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done', 'cancelled')),
        nudged_at_ms INTEGER,
        nudge_attempts INTEGER NOT NULL DEFAULT 0
    )""",
    "CREATE INDEX IF NOT EXISTS idx_group_decisions_chat "
    "ON group_decisions(chat_id, status, due_at_ms)",
    "CREATE INDEX IF NOT EXISTS idx_group_decisions_creator "
    "ON group_decisions(created_by, created_at_ms)",
    "CREATE INDEX IF NOT EXISTS idx_group_decisions_due "
    "ON group_decisions(status, nudged_at_ms, due_at_ms)",
)


@dataclass(frozen=True, slots=True)
class Decision:
    id: str
    chat_id: str
    text: str
    created_by: str
    due_at_ms: int | None
    created_at_ms: int
    status: str
    nudged_at_ms: int | None


def _row(row: sqlite3.Row) -> Decision:
    return Decision(
        id=row["id"], chat_id=row["chat_id"], text=row["text"], created_by=row["created_by"],
        due_at_ms=row["due_at_ms"], created_at_ms=row["created_at_ms"], status=row["status"],
        nudged_at_ms=row["nudged_at_ms"],
    )


class GroupDecisionStore:
    """Opens ``ops/group-decisions.db`` lazily, private (0600, WAL, versioned schema)."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._conn: sqlite3.Connection | None = None

    def _db(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        if self._path is None:
            from yeoman_shared.utils.helpers import get_operational_store_path

            self._path = get_operational_store_path("group_decisions")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._path, isolation_level=None)
        try:
            os.chmod(self._path, 0o600)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=2000")
            conn.execute("PRAGMA journal_mode=WAL")
            _migrate(conn)
        except BaseException:
            conn.close()
            raise
        self._conn = conn
        return conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def add(
        self,
        *,
        chat_id: str,
        text: str,
        created_by: str,
        due_at_ms: int | None,
        source_message_ids: list[str],
        now_ms: int,
    ) -> Decision:
        decision_id = uuid.uuid4().hex[:8]
        self._db().execute(
            "INSERT INTO group_decisions(id, chat_id, text, created_by, due_at_ms, "
            "source_message_ids, created_at_ms, updated_at_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (decision_id, chat_id, text, created_by, due_at_ms,
             json.dumps(source_message_ids), now_ms, now_ms),
        )
        found = self.get(chat_id, decision_id)
        assert found is not None
        return found

    def get(self, chat_id: str, decision_id: str) -> Decision | None:
        row = self._db().execute(
            "SELECT * FROM group_decisions WHERE chat_id = ? AND id = ?", (chat_id, decision_id)
        ).fetchone()
        return _row(row) if row is not None else None

    def list_open(self, chat_id: str) -> list[Decision]:
        rows = self._db().execute(
            "SELECT * FROM group_decisions WHERE chat_id = ? AND status = 'open' "
            "ORDER BY due_at_ms IS NULL, due_at_ms, created_at_ms",
            (chat_id,),
        ).fetchall()
        return [_row(row) for row in rows]

    def count_open(self, chat_id: str) -> int:
        return int(self._db().execute(
            "SELECT count(*) FROM group_decisions WHERE chat_id = ? AND status = 'open'",
            (chat_id,),
        ).fetchone()[0])

    def count_added_since(self, created_by: str, since_ms: int) -> int:
        """Rows a sender created since a time, closed ones included (they are kept 90 days)."""
        return int(self._db().execute(
            "SELECT count(*) FROM group_decisions WHERE created_by = ? AND created_at_ms > ?",
            (created_by, since_ms),
        ).fetchone()[0])

    def close_decision(self, chat_id: str, decision_id: str, status: str, now_ms: int) -> bool:
        """Mark an open decision of this chat done or cancelled."""
        if status not in {"done", "cancelled"}:
            raise ValueError(f"invalid status {status!r}")
        cursor = self._db().execute(
            "UPDATE group_decisions SET status = ?, updated_at_ms = ? "
            "WHERE chat_id = ? AND id = ? AND status = 'open'",
            (status, now_ms, chat_id, decision_id),
        )
        return cursor.rowcount == 1

    def due_for_nudge(self, now_ms: int, limit: int = 20) -> list[Decision]:
        rows = self._db().execute(
            "SELECT * FROM group_decisions WHERE status = 'open' AND nudged_at_ms IS NULL "
            "AND due_at_ms IS NOT NULL AND due_at_ms <= ? ORDER BY due_at_ms LIMIT ?",
            (now_ms, int(limit)),
        ).fetchall()
        return [_row(row) for row in rows]

    def mark_nudged(self, chat_id: str, decision_id: str, now_ms: int) -> bool:
        cursor = self._db().execute(
            "UPDATE group_decisions SET nudged_at_ms = ?, updated_at_ms = ? "
            "WHERE chat_id = ? AND id = ? AND nudged_at_ms IS NULL",
            (now_ms, now_ms, chat_id, decision_id),
        )
        return cursor.rowcount == 1

    def record_nudge_failure(self, chat_id: str, decision_id: str) -> int:
        """Count a failed nudge; returns the attempts so far."""
        db = self._db()
        db.execute(
            "UPDATE group_decisions SET nudge_attempts = nudge_attempts + 1 "
            "WHERE chat_id = ? AND id = ?",
            (chat_id, decision_id),
        )
        row = db.execute(
            "SELECT nudge_attempts FROM group_decisions WHERE chat_id = ? AND id = ?",
            (chat_id, decision_id),
        ).fetchone()
        return int(row[0]) if row is not None else 0

    def purge_expired(self, now_ms: int) -> int:
        """Drop closed decisions after 90 days and open ones after 180 days."""
        cursor = self._db().execute(
            "DELETE FROM group_decisions WHERE "
            "(status IN ('done', 'cancelled') AND updated_at_ms < ?) "
            "OR (status = 'open' AND created_at_ms < ?)",
            (now_ms - DONE_RETENTION_MS, now_ms - OPEN_RETENTION_MS),
        )
        return int(cursor.rowcount)


def _migrate(conn: sqlite3.Connection) -> None:
    has_meta = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'").fetchone()
    if has_meta:
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        if row is not None:
            version = int(row[0])
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"group-decisions.db schema {version} is newer than {SCHEMA_VERSION}")
            return
    conn.execute("BEGIN IMMEDIATE")
    try:
        for statement in _DDL:
            conn.execute(statement)
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
                     (str(SCHEMA_VERSION),))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
