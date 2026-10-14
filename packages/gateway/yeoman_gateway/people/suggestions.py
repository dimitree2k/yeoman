"""Owner review of suggestions: list, accept and reject (spec 2026-10-11 §3 ``suggestions``)."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .store import PeopleStore


class SuggestionError(ValueError):
    """The suggestion cannot be decided (unknown, already decided, unsupported or blocked)."""


def _person(conn: sqlite3.Connection, person_id: int | None) -> dict[str, Any] | None:
    if person_id is None:
        return None
    row = conn.execute("SELECT id, merged_into, effective_level, preferred_name, names, ids "
                       "FROM persons_v WHERE id = ?", (person_id,)).fetchone()
    return dict(row) if row is not None else {"id": person_id}


def list_suggestions(conn: sqlite3.Connection, *, include_decided: bool = False) -> list[dict[str, Any]]:
    """Suggestions with both persons described, oldest first."""
    sql = "SELECT * FROM suggestions" + ("" if include_decided else " WHERE status = 'open'") + " ORDER BY id"
    out = []
    for row in conn.execute(sql).fetchall():
        item = dict(row)
        item["evidence"] = json.loads(item["evidence"])
        item["person_a"] = _person(conn, row["person_a"])
        item["person_b"] = _person(conn, row["person_b"])
        out.append(item)
    return out


def decide_suggestion(store: PeopleStore, suggestion_id: int, *, accept: bool, note: str | None,
                      now_ms: int) -> dict[str, Any]:
    """Accept or reject one open suggestion; the caller owns the transaction (writer ``owner``).

    A merge is recorded as an owner decision (``same`` or ``not_same``), so sync keeps honouring
    it; an accepted merge is applied at once. Raises SuggestionError and writes nothing usable
    (the caller's transaction rolls back) when the suggestion cannot be decided.
    """
    row = store.conn.execute("SELECT * FROM suggestions WHERE id = ?", (suggestion_id,)).fetchone()
    if row is None:
        raise SuggestionError(f"no suggestion {suggestion_id}")
    if row["status"] != "open":
        raise SuggestionError(f"suggestion {suggestion_id} is already {row['status']}")
    kind, a, b = row["kind"], row["person_a"], row["person_b"]
    if accept and kind != "merge":
        raise SuggestionError(f"accepting {kind} suggestions is not supported yet")
    merged_into = None
    if kind == "merge":
        store.conn.execute(
            "INSERT INTO person_decisions(person_a, person_b, decision, note, decided_ms) "
            "VALUES (?, ?, ?, ?, ?)", (a, b, "same" if accept else "not_same", note, now_ms))
        if accept:
            ta, tb = store.terminal(a), store.terminal(b)
            if ta != tb and not store.merge(max(ta, tb), min(ta, tb), by="owner"):
                raise SuggestionError(
                    f"suggestion {suggestion_id} is blocked by a not_same decision between the two groups")
            merged_into = store.terminal(a)
    status = "accepted" if accept else "rejected"
    store.conn.execute("UPDATE suggestions SET status = ?, decided_ms = ? WHERE id = ?",
                       (status, now_ms, suggestion_id))
    return {"id": suggestion_id, "kind": kind, "status": status, "merged_into": merged_into}
