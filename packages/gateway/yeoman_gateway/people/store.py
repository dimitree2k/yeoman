"""PeopleStore: the only write API used by sync and import; owner rows and columns stay unreachable."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from .identifiers import ObservedId
from .schema import writer

_SYNC_FIELDS = frozenset({"level", "last_seen_ms", "is_owner"})
_MAX_HOPS = 64


class PeopleIntegrityError(RuntimeError):
    """The directory violates an invariant sync relies on (e.g. a merged_into cycle)."""


class PeopleStore:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    @contextmanager
    def transaction(self, writer_name: str) -> Iterator[None]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            with writer(self.conn, writer_name):
                yield
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

    # -- merge graph -----------------------------------------------------------------------

    def terminal(self, person_id: int) -> int:
        seen: set[int] = set()
        current = person_id
        for _ in range(_MAX_HOPS):
            if current in seen:
                raise PeopleIntegrityError("merged_into cycle")
            seen.add(current)
            row = self.conn.execute("SELECT merged_into FROM persons WHERE id = ?", (current,)).fetchone()
            if row is None:
                raise PeopleIntegrityError("unknown person")
            if row[0] is None:
                return current
            current = int(row[0])
        raise PeopleIntegrityError("merged_into chain too long")

    def members(self, terminal: int) -> set[int]:
        """All persons whose terminal is ``terminal`` (including itself)."""
        found = {terminal}
        frontier = [terminal]
        while frontier:
            marks = ",".join("?" * len(frontier))
            rows = self.conn.execute(
                f"SELECT id FROM persons WHERE merged_into IN ({marks})", frontier).fetchall()
            frontier = [int(r[0]) for r in rows if int(r[0]) not in found]
            found.update(frontier)
            if len(found) > 100_000:
                raise PeopleIntegrityError("merge graph too large")
        return found

    # -- identifiers -----------------------------------------------------------------------

    def find_active(self, kind: str, value: str, at_ms: int | None = None) -> int | None:
        if at_ms is None:
            row = self.conn.execute(
                "SELECT person_id FROM person_ids WHERE kind = ? AND value = ? "
                "AND valid_until_ms IS NULL AND hidden = 0", (kind, value)).fetchone()
        else:
            row = self.conn.execute(
                "SELECT person_id FROM person_ids WHERE kind = ? AND value = ? AND hidden = 0 "
                "AND (valid_from_ms IS NULL OR valid_from_ms <= ?) "
                "AND (valid_until_ms IS NULL OR valid_until_ms > ?) "
                "ORDER BY valid_until_ms IS NULL, valid_from_ms DESC LIMIT 1",
                (kind, value, at_ms, at_ms)).fetchone()
        return int(row[0]) if row is not None else None

    def is_hidden_for(self, person_terminal: int, kind: str, value: str) -> bool:
        rows = self.conn.execute(
            "SELECT person_id FROM person_ids WHERE kind = ? AND value = ? AND hidden = 1",
            (kind, value)).fetchall()
        return any(self.terminal(int(r[0])) == person_terminal for r in rows)

    def create_person(self, *, level: str, now_ms: int) -> int:
        cursor = self.conn.execute(
            "INSERT INTO persons(level, created_ms) VALUES (?, ?)", (level, now_ms))
        return int(cursor.lastrowid)

    def attach_id(self, person_id: int, ident: ObservedId, now_ms: int) -> str:
        """Return 'added' | 'touched' | 'hidden' | 'taken'; only last_seen of observed rows changes."""
        term = self.terminal(person_id)
        if self.is_hidden_for(term, ident.kind, ident.value):
            return "hidden"
        if ident.valid_until_ms is not None:
            rows = self.conn.execute(
                "SELECT person_id FROM person_ids WHERE kind = ? AND value = ? AND valid_until_ms = ? "
                "AND valid_from_ms IS ?", (ident.kind, ident.value, ident.valid_until_ms,
                                           ident.valid_from_ms)).fetchall()
            if any(self.terminal(int(r[0])) == term for r in rows):
                return "touched"
            self._insert_id(person_id, ident, now_ms)
            return "added"
        row = self.conn.execute(
            "SELECT id, person_id, source, last_seen_ms FROM person_ids WHERE kind = ? AND value = ? "
            "AND valid_until_ms IS NULL AND hidden = 0", (ident.kind, ident.value)).fetchone()
        if row is None:
            self._insert_id(person_id, ident, now_ms)
            return "added"
        if self.terminal(int(row["person_id"])) != term:
            return "taken"
        if (row["source"] == "observed" and ident.last_seen_ms is not None
                and (row["last_seen_ms"] is None or ident.last_seen_ms > row["last_seen_ms"])):
            self.conn.execute("UPDATE person_ids SET last_seen_ms = ? WHERE id = ?",
                              (ident.last_seen_ms, row["id"]))
        return "touched"

    def _insert_id(self, person_id: int, ident: ObservedId, now_ms: int) -> None:
        self.conn.execute(
            "INSERT INTO person_ids(person_id, kind, value, source, valid_from_ms, valid_until_ms, "
            "first_seen_ms, last_seen_ms) VALUES (?, ?, ?, 'observed', ?, ?, ?, ?)",
            (person_id, ident.kind, ident.value, ident.valid_from_ms, ident.valid_until_ms,
             ident.first_seen_ms if ident.first_seen_ms is not None else now_ms, ident.last_seen_ms))

    def move_observed_ids(self, ids: list[int], to_person: int) -> int:
        if not ids:
            return 0
        marks = ",".join("?" * len(ids))
        cursor = self.conn.execute(
            f"UPDATE person_ids SET person_id = ? WHERE id IN ({marks}) AND source = 'observed' "
            "AND person_id <> ?", (to_person, *ids, to_person))
        return cursor.rowcount

    # -- names -----------------------------------------------------------------------------

    def add_observed_name(self, person_id: int, name: str, kind: str, now_ms: int,
                          evidence: dict[str, Any] | None = None) -> str:
        """Return 'added' | 'touched' | 'hidden' | 'empty'."""
        name = name.strip() if isinstance(name, str) else ""
        if not any(ch.isalnum() for ch in name):
            return "empty"
        group = sorted(self.members(self.terminal(person_id)))
        marks = ",".join("?" * len(group))
        if self.conn.execute(
                f"SELECT 1 FROM person_names WHERE person_id IN ({marks}) AND name = ? AND hidden = 1",
                (*group, name)).fetchone():
            return "hidden"
        if self.conn.execute(
                f"SELECT 1 FROM person_names WHERE person_id IN ({marks}) AND name = ? AND kind = ? "
                "AND source = 'observed'", (*group, name, kind)).fetchone():
            return "touched"
        self.conn.execute(
            "INSERT INTO person_names(person_id, name, kind, source, evidence, first_seen_ms, last_seen_ms) "
            "VALUES (?, ?, ?, 'observed', ?, ?, ?)",
            (person_id, name, kind, json.dumps(evidence or {}, sort_keys=True), now_ms, now_ms))
        return "added"

    # -- person fields ---------------------------------------------------------------------

    def set_sync_fields(self, person_id: int, **fields: Any) -> bool:
        """Set level / last_seen_ms / is_owner; anything else is unreachable by construction."""
        unknown = set(fields) - _SYNC_FIELDS
        if unknown:
            raise ValueError(f"sync may not write {sorted(unknown)}")
        row = self.person_rows(person_id)
        changes = {}
        for key, value in fields.items():
            if value is None:
                continue
            if key == "is_owner":
                value = int(bool(value))
            if row[key] != value:
                changes[key] = value
        if not changes:
            return False
        assignments = ", ".join(f"{key} = ?" for key in changes)
        self.conn.execute(f"UPDATE persons SET {assignments} WHERE id = ?", (*changes.values(), person_id))
        return True

    def person_rows(self, person_id: int) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM persons WHERE id = ?", (person_id,)).fetchone()
        if row is None:
            raise PeopleIntegrityError("unknown person")
        return row

    # -- merges and decisions --------------------------------------------------------------

    def latest_decision(self, a: int, b: int) -> str | None:
        row = self.conn.execute(
            "SELECT decision FROM person_decisions WHERE (person_a = ? AND person_b = ?) "
            "OR (person_a = ? AND person_b = ?) ORDER BY decided_ms DESC, id DESC LIMIT 1",
            (a, b, b, a)).fetchone()
        return str(row[0]) if row is not None else None

    def blocked(self, a: int, b: int) -> bool:
        """True when any pair across the two merge groups has a latest 'not_same' decision."""
        left, right = self.members(self.terminal(a)), self.members(self.terminal(b))
        return any(self.latest_decision(x, y) == "not_same" for x in left for y in right)

    def merge(self, loser: int, winner: int, *, by: str) -> bool:
        loser, winner = self.terminal(loser), self.terminal(winner)
        if loser == winner or self.blocked(loser, winner):
            return False
        self.conn.execute("UPDATE persons SET merged_into = ?, merged_by = ? WHERE id = ?",
                          (winner, by, loser))
        return True

    def unmerge_sync(self, person_id: int) -> bool:
        cursor = self.conn.execute(
            "UPDATE persons SET merged_into = NULL, merged_by = NULL "
            "WHERE id = ? AND merged_into IS NOT NULL AND merged_by = 'sync'", (person_id,))
        return cursor.rowcount == 1

    def suggest(self, kind: str, person_a: int, person_b: int | None, *, field: str | None = None,
                value: str | None = None, evidence: dict[str, Any] | None = None) -> bool:
        if kind == "merge" and person_b is not None and person_b < person_a:
            person_a, person_b = person_b, person_a
        cursor = self.conn.execute(
            "INSERT OR IGNORE INTO suggestions(kind, person_a, person_b, field, value, evidence) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (kind, person_a, person_b, field, value, json.dumps(evidence or {}, sort_keys=True)))
        return cursor.rowcount == 1
