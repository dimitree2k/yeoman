"""SQLite store for one group-state document per chat.

Every read and write names both ``(channel, chat_id)``. Writes are compare-and-swap
on ``revision`` inside one ``BEGIN IMMEDIATE`` transaction, together with the
matching snapshot, so a racing writer can never leave a half-written document.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path

from .models import SCHEMA_VERSION, GroupState

_DDL = (
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS group_state (
        channel TEXT NOT NULL,
        chat_id TEXT NOT NULL,
        revision INTEGER NOT NULL,
        schema_version INTEGER NOT NULL,
        updated_ms INTEGER NOT NULL,
        document TEXT NOT NULL,
        PRIMARY KEY (channel, chat_id)
    )""",
    """CREATE TABLE IF NOT EXISTS group_state_snapshots (
        channel TEXT NOT NULL,
        chat_id TEXT NOT NULL,
        revision INTEGER NOT NULL,
        updated_ms INTEGER NOT NULL,
        document TEXT NOT NULL,
        PRIMARY KEY (channel, chat_id, revision)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_group_state_snapshots_chat "
    "ON group_state_snapshots(channel, chat_id, revision DESC)",
)


class GroupStateStore:
    """Open ``group-state.db`` lazily, private (0600, WAL, versioned schema)."""

    def __init__(
        self,
        path: Path,
        *,
        snapshots_kept: int = 20,
        readonly: bool = False,
    ) -> None:
        self.path = Path(path)
        self.snapshots_kept = max(1, int(snapshots_kept))
        self._lock = threading.RLock()
        self._conn = self._open(readonly=readonly)

    def _open(self, *, readonly: bool) -> sqlite3.Connection:
        if readonly:
            if not self.path.is_file():
                raise FileNotFoundError(self.path)
            conn = sqlite3.connect(f"{self.path.as_uri()}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            return conn
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Create the file private before SQLite opens it, so it is never world-readable.
        os.close(os.open(self.path, os.O_WRONLY | os.O_CREAT, 0o600))
        conn = sqlite3.connect(self.path, isolation_level=None)
        try:
            os.chmod(self.path, 0o600)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=2000")
            conn.execute("PRAGMA journal_mode=WAL")
            self._migrate(conn)
        except BaseException:
            conn.close()
            raise
        return conn

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in _DDL:
                conn.execute(statement)
            row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
            if row is not None and int(row[0]) > SCHEMA_VERSION:
                raise RuntimeError(f"group-state.db schema {row[0]} is newer than {SCHEMA_VERSION}")
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "GroupStateStore":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def get(self, channel: str, chat_id: str) -> GroupState | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT document FROM group_state WHERE channel = ? AND chat_id = ?",
                (str(channel), str(chat_id)),
            ).fetchone()
        return GroupState.from_json(row["document"]) if row is not None else None

    def put(self, state: GroupState, *, expected_revision: int | None) -> bool:
        """Insert when absent or update the exact revision; snapshot on success."""
        channel = str(state.channel)
        chat_id = str(state.chat_id)
        document = state.to_json()
        with self._lock:
            conn = self._conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                if expected_revision is None:
                    cursor = conn.execute(
                        "INSERT OR IGNORE INTO group_state"
                        "(channel, chat_id, revision, schema_version, updated_ms, document)"
                        " VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            channel,
                            chat_id,
                            int(state.revision),
                            int(state.schema_version),
                            int(state.updated_ms),
                            document,
                        ),
                    )
                    written = cursor.rowcount == 1
                else:
                    cursor = conn.execute(
                        "UPDATE group_state SET revision = ?, schema_version = ?,"
                        " updated_ms = ?, document = ?"
                        " WHERE channel = ? AND chat_id = ? AND revision = ?",
                        (
                            int(state.revision),
                            int(state.schema_version),
                            int(state.updated_ms),
                            document,
                            channel,
                            chat_id,
                            int(expected_revision),
                        ),
                    )
                    written = cursor.rowcount == 1
                if not written:
                    conn.execute("ROLLBACK")
                    return False
                conn.execute(
                    "INSERT OR REPLACE INTO group_state_snapshots"
                    "(channel, chat_id, revision, updated_ms, document) VALUES (?, ?, ?, ?, ?)",
                    (
                        channel,
                        chat_id,
                        int(state.revision),
                        int(state.updated_ms),
                        document,
                    ),
                )
                conn.execute(
                    "DELETE FROM group_state_snapshots WHERE channel = ? AND chat_id = ?"
                    " AND revision NOT IN (SELECT revision FROM group_state_snapshots"
                    " WHERE channel = ? AND chat_id = ? ORDER BY revision DESC LIMIT ?)",
                    (
                        channel,
                        chat_id,
                        channel,
                        chat_id,
                        self.snapshots_kept,
                    ),
                )
                conn.execute("COMMIT")
                return True
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def snapshots(self, channel: str, chat_id: str, limit: int) -> list[GroupState]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT document FROM group_state_snapshots"
                " WHERE channel = ? AND chat_id = ? ORDER BY revision DESC LIMIT ?",
                (str(channel), str(chat_id), max(1, int(limit))),
            ).fetchall()
        return [GroupState.from_json(row["document"]) for row in rows]

    def purge_chat(self, channel: str, chat_id: str) -> int:
        channel = str(channel)
        chat_id = str(chat_id)
        with self._lock:
            conn = self._conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                removed = conn.execute(
                    "DELETE FROM group_state_snapshots WHERE channel = ? AND chat_id = ?",
                    (channel, chat_id),
                ).rowcount
                removed += conn.execute(
                    "DELETE FROM group_state WHERE channel = ? AND chat_id = ?",
                    (channel, chat_id),
                ).rowcount
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        return int(removed)

    def list_chats(self) -> list[tuple[str, str, int, int]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT channel, chat_id, revision, updated_ms FROM group_state"
                " ORDER BY channel, chat_id"
            ).fetchall()
        return [
            (str(row["channel"]), str(row["chat_id"]), int(row["revision"]), int(row["updated_ms"]))
            for row in rows
        ]
