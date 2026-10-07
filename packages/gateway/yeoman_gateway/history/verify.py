"""Verify a built history.db: coverage, review list, accounting and rebuild determinism."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path
from typing import Any

from .convert.common import open_ro
from .extract import extract
from .layer1 import canonical_json, iter_layer1, layer1_files
from .project import _authors, _events, project
from .resolve import resolve

TABLES = ("contacts", "identifier_history", "messages", "message_events")


def _open(db_path: Path, *, frozen: bool = False) -> sqlite3.Connection:
    conn = (open_ro(db_path) if frozen else
            sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True))
    conn.execute("BEGIN")
    return conn


def table_digest(db_path: Path, *, frozen: bool = False) -> dict[str, str]:
    with closing(_open(db_path, frozen=frozen)) as conn:
        return _table_digest(conn)


def _table_digest(conn: sqlite3.Connection) -> dict[str, str]:
    digests: dict[str, str] = {}
    for table in TABLES:
        cols = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")
                if not (table == "identifier_history" and row[1] == "id")]
        digest = hashlib.sha256()
        for row in conn.execute(f"SELECT {', '.join(cols)} FROM {table} ORDER BY {', '.join(cols)}"):
            digest.update(canonical_json(list(row)).encode("utf-8") + b"\n")
        digests[table] = digest.hexdigest()
    return digests


def coverage(db_path: Path, *, frozen: bool = False) -> dict[str, Any]:
    with closing(_open(db_path, frozen=frozen)) as conn:
        return _coverage(conn)


def _coverage(conn: sqlite3.Connection) -> dict[str, Any]:
    total, with_contact, confirmed = conn.execute(
        "SELECT count(*), count(m.sender_contact_id), coalesce(sum(c.status = 'confirmed'), 0)"
        " FROM messages m LEFT JOIN contacts c ON c.contact_id = m.sender_contact_id"
        " WHERE m.sender_identifier IS NOT NULL").fetchone()
    provisional = [
        {"contact_id": row[0], "display_name": row[1], "messages": row[2],
         "identifiers": json.loads(row[3])}
        for row in conn.execute(
            "SELECT c.contact_id, c.display_name,"
            " (SELECT count(*) FROM messages m WHERE m.sender_contact_id = c.contact_id),"
            " (SELECT json_group_array(value) FROM (SELECT i.value FROM identifier_history i"
            "   WHERE i.contact_id = c.contact_id AND i.kind != 'push_name' ORDER BY i.value))"
            " FROM contacts c WHERE c.status = 'provisional' AND c.merged_into IS NULL"
            " ORDER BY 3 DESC, c.contact_id")]
    unresolved = [{"message_id": row[0], "sender_identifier": row[1]} for row in conn.execute(
        "SELECT message_id, sender_identifier FROM messages"
        " WHERE sender_identifier IS NOT NULL AND sender_contact_id IS NULL"
        " ORDER BY message_id LIMIT 100")]
    basis = dict(conn.execute("SELECT sender_basis, count(*) FROM messages GROUP BY 1 ORDER BY 1"))
    provenance = dict(conn.execute("SELECT provenance, count(*) FROM messages GROUP BY 1 ORDER BY 1"))
    ratio = confirmed / total if total else 1.0
    return {"messages_with_identifier": total, "with_contact": with_contact,
            "with_confirmed_contact": confirmed, "confirmed_ratio": round(ratio, 4),
            "meets_95_percent": ratio >= 0.95, "provisional_contacts": provisional,
            "unresolved_messages": unresolved, "sender_basis": basis, "provenance": provenance}


def _input_pin(roots: Sequence[Path]) -> dict[str, str]:
    return {rel: hashlib.sha256(path.read_bytes()).hexdigest()
            for rel, path in layer1_files(roots)}


def verify(roots: Sequence[Path], db_path: Path, *, scratch: Path | None,
           frozen: bool = False) -> dict[str, Any]:
    if scratch is not None and not frozen:
        raise ValueError("determinism requires explicitly frozen inputs")
    pinned = _input_pin(roots) if frozen else None
    with closing(_open(db_path, frozen=frozen)) as conn:
        report: dict[str, Any] = {"coverage": _coverage(conn), "digests": _table_digest(conn)}
        message_ids = {row[0] for row in conn.execute("SELECT message_id FROM messages")}
    report["boundary"] = {"mode": "frozen" if frozen else "live",
                          "database": "single_read_transaction", "tail_freshness": False}
    if pinned is not None:
        report["input_digests"] = pinned
    ex = extract(iter_layer1(roots))
    resolution = resolve(ex.identity)
    authors = _authors(ex, resolution)
    _events(ex, resolution, resolution.role_contact.get("assistant"), message_ids, resolution.review, authors)
    report["review"] = resolution.review
    accounted: Counter[str] = Counter()
    for (file, _), count in ex.outcomes.items():
        accounted[file] += count
    lines = {}
    blanks = {}
    for rel, path in layer1_files(roots):
        physical = path.read_bytes().splitlines()
        lines[rel] = len(physical)
        blanks[rel] = sum(not line.strip() for line in physical)
        accounted[rel] += blanks[rel]
    report["blank_lines_skipped"] = blanks
    report["accounting"] = {rel: {"lines": count, "accounted": accounted.get(rel, 0)}
                            for rel, count in lines.items()}
    report["accounting_ok"] = all(item["lines"] == item["accounted"]
                                   for item in report["accounting"].values())

    def check_inputs() -> None:
        if pinned is not None and _input_pin(roots) != pinned:
            raise ValueError("frozen Layer 1 inputs changed during verification")

    check_inputs()
    if scratch is not None:
        if any(scratch.resolve().is_relative_to(root.resolve()) or
               root.resolve().is_relative_to(scratch.resolve()) for root in roots):
            raise ValueError("scratch must be separate from frozen inputs")
        first, second = scratch / "rebuild-a.db", scratch / "rebuild-b.db"
        if db_path.resolve() in (first.resolve(), second.resolve()):
            raise ValueError("scratch rebuild must not replace the original database")
        scratch.mkdir(parents=True, exist_ok=True, mode=0o700)
        project(roots, first)
        check_inputs()
        project(roots, second)
        check_inputs()
        report["deterministic"] = (table_digest(first, frozen=True) ==
                                   table_digest(second, frozen=True) == report["digests"])
    return report
