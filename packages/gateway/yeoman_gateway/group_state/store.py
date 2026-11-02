"""SQLite store for one group-state document per chat.

Every read and write names both ``(channel, chat_id)``. Writes are compare-and-swap
on ``revision`` inside one ``BEGIN IMMEDIATE`` transaction, together with the
matching snapshot, so a racing writer can never leave a half-written document.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Collection, Mapping
from dataclasses import replace
from pathlib import Path

from .models import SCHEMA_VERSION, GroupState, Section

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
    """CREATE TABLE IF NOT EXISTS group_state_runs (
        channel TEXT NOT NULL,
        chat_id TEXT NOT NULL,
        run_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        started_ms INTEGER NOT NULL,
        input_hash TEXT NOT NULL,
        prompt_version TEXT NOT NULL,
        model TEXT NOT NULL,
        outcome TEXT NOT NULL,
        prompt_tokens INTEGER,
        completion_tokens INTEGER,
        PRIMARY KEY (channel, chat_id, run_id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_group_state_runs_chat "
    "ON group_state_runs(channel, chat_id, started_ms)",
    """CREATE TABLE IF NOT EXISTS group_state_cursors (
        channel TEXT NOT NULL,
        chat_id TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT 'invalidation',
        occurred_ms INTEGER NOT NULL,
        event_id TEXT NOT NULL,
        PRIMARY KEY (channel, chat_id, kind)
    )""",
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
            conn.execute(_DDL[0])
            row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
            stored = int(row[0]) if row is not None else 0
            if stored > SCHEMA_VERSION:
                raise RuntimeError(f"group-state.db schema {row[0]} is newer than {SCHEMA_VERSION}")
            for statement in _DDL[1:]:
                conn.execute(statement)
            GroupStateStore._migrate_cursors(conn)
            if stored < SCHEMA_VERSION:
                GroupStateStore._rewrite_documents(conn)
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    @staticmethod
    def _migrate_cursors(conn: sqlite3.Connection) -> None:
        """Add the ``kind`` discriminator to a pre-summaries cursor table."""
        columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(group_state_cursors)").fetchall()
        }
        if "kind" in columns:
            return
        # The old table held exactly one row per chat: the invalidation cursor.
        conn.execute("ALTER TABLE group_state_cursors RENAME TO group_state_cursors_v1")
        conn.execute(_DDL[-1])
        conn.execute(
            "INSERT INTO group_state_cursors(channel, chat_id, kind, occurred_ms, event_id)"
            " SELECT channel, chat_id, 'invalidation', occurred_ms, event_id"
            " FROM group_state_cursors_v1"
        )
        conn.execute("DROP TABLE group_state_cursors_v1")

    @staticmethod
    def _rewrite_documents(conn: sqlite3.Connection) -> None:
        """Rewrite every stored v1 document through the current value objects."""
        for row in conn.execute(
            "SELECT channel, chat_id, revision, document FROM group_state"
        ).fetchall():
            state = replace(GroupState.from_json(row["document"]), schema_version=SCHEMA_VERSION)
            conn.execute(
                "UPDATE group_state SET schema_version = ?, document = ?"
                " WHERE channel = ? AND chat_id = ? AND revision = ?",
                (SCHEMA_VERSION, state.to_json(), row["channel"], row["chat_id"], row["revision"]),
            )
        for row in conn.execute(
            "SELECT channel, chat_id, revision, document FROM group_state_snapshots"
        ).fetchall():
            state = replace(GroupState.from_json(row["document"]), schema_version=SCHEMA_VERSION)
            conn.execute(
                "UPDATE group_state_snapshots SET document = ?"
                " WHERE channel = ? AND chat_id = ? AND revision = ?",
                (state.to_json(), row["channel"], row["chat_id"], row["revision"]),
            )

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

    def merge_sections(
        self,
        channel: str,
        chat_id: str,
        sections: Mapping[str, Section],
        *,
        remove: Collection[str] = (),
        updated_ms: int,
        require_existing: bool = False,
    ) -> GroupState | None:
        """Replace only these section keys under one compare-and-swap transaction."""
        channel = str(channel)
        chat_id = str(chat_id)
        with self._lock:
            conn = self._conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT document FROM group_state WHERE channel = ? AND chat_id = ?",
                    (channel, chat_id),
                ).fetchone()
                if row is None and require_existing:
                    conn.execute("ROLLBACK")
                    return None
                current = GroupState.from_json(row["document"]) if row is not None else None
                merged = dict(current.sections) if current is not None else {}
                for key in remove:
                    merged.pop(str(key), None)
                merged.update(sections)
                state = GroupState(
                    channel=channel,
                    chat_id=chat_id,
                    schema_version=SCHEMA_VERSION,
                    revision=(current.revision if current is not None else 0) + 1,
                    history_cursor=_newest_cursor(merged),
                    updated_ms=int(updated_ms),
                    sections=merged,
                )
                written = self._write(
                    conn, state, current.revision if current is not None else None
                )
                if not written:
                    conn.execute("ROLLBACK")
                    return None
                self._write_snapshot(conn, state)
                conn.execute("COMMIT")
                return state
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    @staticmethod
    def _write(conn: sqlite3.Connection, state: GroupState, expected_revision: int | None) -> bool:
        if expected_revision is None:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO group_state"
                "(channel, chat_id, revision, schema_version, updated_ms, document)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    state.channel,
                    state.chat_id,
                    int(state.revision),
                    int(state.schema_version),
                    int(state.updated_ms),
                    state.to_json(),
                ),
            )
            return cursor.rowcount == 1
        cursor = conn.execute(
            "UPDATE group_state SET revision = ?, schema_version = ?,"
            " updated_ms = ?, document = ?"
            " WHERE channel = ? AND chat_id = ? AND revision = ?",
            (
                int(state.revision),
                int(state.schema_version),
                int(state.updated_ms),
                state.to_json(),
                state.channel,
                state.chat_id,
                int(expected_revision),
            ),
        )
        return cursor.rowcount == 1

    def _write_snapshot(self, conn: sqlite3.Connection, state: GroupState) -> None:
        conn.execute(
            "INSERT OR REPLACE INTO group_state_snapshots"
            "(channel, chat_id, revision, updated_ms, document) VALUES (?, ?, ?, ?, ?)",
            (
                state.channel,
                state.chat_id,
                int(state.revision),
                int(state.updated_ms),
                state.to_json(),
            ),
        )
        conn.execute(
            "DELETE FROM group_state_snapshots WHERE channel = ? AND chat_id = ?"
            " AND revision NOT IN (SELECT revision FROM group_state_snapshots"
            " WHERE channel = ? AND chat_id = ? ORDER BY revision DESC LIMIT ?)",
            (
                state.channel,
                state.chat_id,
                state.channel,
                state.chat_id,
                self.snapshots_kept,
            ),
        )

    def item_sources(self, channel: str, chat_id: str) -> set[str]:
        """Every message id a derived item depends on, in the document or any snapshot."""
        sources: set[str] = set()
        with self._lock:
            rows = self._conn.execute(
                "SELECT document FROM group_state WHERE channel = ? AND chat_id = ?"
                " UNION ALL SELECT document FROM group_state_snapshots"
                " WHERE channel = ? AND chat_id = ?",
                (str(channel), str(chat_id), str(channel), str(chat_id)),
            ).fetchall()
        for row in rows:
            state = GroupState.from_json(row["document"])
            for section in state.sections.values():
                for item in section.items:
                    sources.update(str(source) for source in item.sources)
        return sources

    def scrub_items(self, channel: str, chat_id: str, message_ids: Collection[str]) -> int:
        """Remove items that depend on these messages from the document and snapshots."""
        channel = str(channel)
        chat_id = str(chat_id)
        wanted = {str(message_id) for message_id in message_ids if str(message_id)}
        if not wanted:
            return 0
        removed: set[str] = set()
        with self._lock:
            conn = self._conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT document FROM group_state WHERE channel = ? AND chat_id = ?",
                    (channel, chat_id),
                ).fetchone()
                if row is not None:
                    state = GroupState.from_json(row["document"])
                    scrubbed = _scrub_state(state, wanted, removed)
                    if scrubbed is not None:
                        scrubbed = replace(scrubbed, revision=state.revision + 1)
                        conn.execute(
                            "UPDATE group_state SET revision = ?, document = ?"
                            " WHERE channel = ? AND chat_id = ? AND revision = ?",
                            (
                                int(scrubbed.revision),
                                scrubbed.to_json(),
                                channel,
                                chat_id,
                                int(state.revision),
                            ),
                        )
                for snapshot in conn.execute(
                    "SELECT revision, document FROM group_state_snapshots"
                    " WHERE channel = ? AND chat_id = ?",
                    (channel, chat_id),
                ).fetchall():
                    state = GroupState.from_json(snapshot["document"])
                    scrubbed = _scrub_state(state, wanted, removed)
                    if scrubbed is not None:
                        conn.execute(
                            "UPDATE group_state_snapshots SET document = ?"
                            " WHERE channel = ? AND chat_id = ? AND revision = ?",
                            (
                                scrubbed.to_json(),
                                channel,
                                chat_id,
                                int(snapshot["revision"]),
                            ),
                        )
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        return len(removed)

    def record_run(
        self,
        channel: str,
        chat_id: str,
        *,
        run_id: str,
        kind: str,
        started_ms: int,
        input_hash: str,
        prompt_version: str,
        model: str,
        outcome: str,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO group_state_runs"
                "(channel, chat_id, run_id, kind, started_ms, input_hash, prompt_version,"
                " model, outcome, prompt_tokens, completion_tokens)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(channel),
                    str(chat_id),
                    str(run_id),
                    str(kind),
                    int(started_ms),
                    str(input_hash),
                    str(prompt_version),
                    str(model),
                    str(outcome),
                    prompt_tokens,
                    completion_tokens,
                ),
            )

    def prune_runs(self, older_than_ms: int) -> int:
        with self._lock:
            removed = self._conn.execute(
                "DELETE FROM group_state_runs WHERE started_ms < ?", (int(older_than_ms),)
            ).rowcount
        return int(removed)

    def label_cache_hit(self, channel: str, chat_id: str, input_hash: str) -> bool:
        """True when this exact input was already written by an earlier attempt."""
        return self.run_cache_hit(channel, chat_id, kind="labels", input_hash=input_hash)

    def run_cache_hit(self, channel: str, chat_id: str, *, kind: str, input_hash: str) -> bool:
        """True when this exact input was already written by an earlier attempt."""
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM group_state_runs WHERE channel = ? AND chat_id = ?"
                " AND kind = ? AND input_hash = ? AND outcome = 'written' LIMIT 1",
                (str(channel), str(chat_id), str(kind), str(input_hash)),
            ).fetchone()
        return row is not None

    def label_usage(
        self, channel: str, chat_id: str, *, start_ms: int, end_ms: int
    ) -> tuple[int, int, int]:
        """Current window's label rows for this chat, globally, and global tokens."""
        return self.run_usage(channel, chat_id, kind="labels", start_ms=start_ms, end_ms=end_ms)

    def run_usage(
        self,
        channel: str,
        chat_id: str,
        *,
        kind: str,
        start_ms: int,
        end_ms: int,
    ) -> tuple[int, int, int]:
        """Rows for this chat, globally, and global tokens for one run kind."""
        with self._lock:
            row = self._conn.execute(
                "SELECT"
                " COALESCE(SUM(CASE WHEN channel = ? AND chat_id = ? THEN 1 ELSE 0 END), 0),"
                " COUNT(*),"
                " COALESCE(SUM(COALESCE(prompt_tokens, 0) + COALESCE(completion_tokens, 0)), 0)"
                " FROM group_state_runs"
                " WHERE kind = ? AND started_ms >= ? AND started_ms < ?",
                (str(channel), str(chat_id), str(kind), int(start_ms), int(end_ms)),
            ).fetchone()
        return int(row[0]), int(row[1]), int(row[2])

    def invalidation_cursor(self, channel: str, chat_id: str) -> tuple[int, str]:
        return self._cursor(channel, chat_id, "invalidation")

    def set_invalidation_cursor(
        self, channel: str, chat_id: str, occurred_ms: int, event_id: str
    ) -> None:
        self._set_cursor(channel, chat_id, "invalidation", occurred_ms, event_id)

    def summary_cursor(self, channel: str, chat_id: str) -> tuple[int, str]:
        """The newest ended conversation this chat has already processed."""
        return self._cursor(channel, chat_id, "summary")

    def set_summary_cursor(
        self, channel: str, chat_id: str, ended_ms: int, conversation_id: str
    ) -> None:
        self._set_cursor(channel, chat_id, "summary", ended_ms, conversation_id)

    def _cursor(self, channel: str, chat_id: str, kind: str) -> tuple[int, str]:
        with self._lock:
            row = self._conn.execute(
                "SELECT occurred_ms, event_id FROM group_state_cursors"
                " WHERE channel = ? AND chat_id = ? AND kind = ?",
                (str(channel), str(chat_id), str(kind)),
            ).fetchone()
        return (int(row["occurred_ms"]), str(row["event_id"])) if row is not None else (0, "")

    def _set_cursor(
        self, channel: str, chat_id: str, kind: str, occurred_ms: int, event_id: str
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO group_state_cursors"
                "(channel, chat_id, kind, occurred_ms, event_id) VALUES (?, ?, ?, ?, ?)",
                (str(channel), str(chat_id), str(kind), int(occurred_ms), str(event_id)),
            )

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
                conn.execute(
                    "DELETE FROM group_state_runs WHERE channel = ? AND chat_id = ?",
                    (channel, chat_id),
                )
                conn.execute(
                    "DELETE FROM group_state_cursors WHERE channel = ? AND chat_id = ?",
                    (channel, chat_id),
                )
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


def _scrub_state(state: GroupState, wanted: set[str], removed: set[str]) -> GroupState | None:
    """Drop items depending on ``wanted`` and summaries depending on dropped ones.

    A week summary has no message sources of its own (it is built only from
    conversation summaries); its ``depends_on`` list names the conversations, so a
    removed conversation cascades to the week instead of relying on a 500-source union.
    """
    sections: dict[str, Section] = {}
    changed = False
    removed_conversations: set[str] = set()
    for key, section in state.sections.items():
        kept = []
        for item in section.items:
            if wanted.intersection(item.sources):
                removed.add(item.id)
                if item.conversation_id:
                    removed_conversations.add(item.conversation_id)
            else:
                kept.append(item)
        if len(kept) != len(section.items):
            changed = True
            section = replace(section, items=tuple(kept))
        sections[key] = section
    if removed_conversations:
        for key, section in sections.items():
            kept = []
            for item in section.items:
                depends_on = item.data.get("depends_on")
                dependencies = (
                    {str(value) for value in depends_on}
                    if isinstance(depends_on, (list, tuple))
                    else set()
                )
                if dependencies.intersection(removed_conversations):
                    removed.add(item.id)
                    changed = True
                else:
                    kept.append(item)
            if len(kept) != len(section.items):
                sections[key] = replace(section, items=tuple(kept))
    return replace(state, sections=sections) if changed else None


def _newest_cursor(sections: Mapping[str, Section]) -> str:
    """The newest known message id, preferring the widest observation window."""
    for key in ("presence", "conversations", "people_activity", "arvid_actions", "budgets"):
        section = sections.get(key)
        if section is not None and section.source_cursor:
            return section.source_cursor
    return next(
        (section.source_cursor for section in sections.values() if section.source_cursor),
        "",
    )
