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
from .project import _events, project
from .resolve import resolve

TABLES = ("contacts", "identifier_history", "messages", "message_events")


def _open(db_path: Path) -> sqlite3.Connection:
    return open_ro(db_path)


def table_digest(db_path: Path) -> dict[str, str]:
    digests: dict[str, str] = {}
    with closing(_open(db_path)) as conn:
        for table in TABLES:
            cols = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")
                    if not (table == "identifier_history" and row[1] == "id")]
            digest = hashlib.sha256()
            for row in conn.execute(f"SELECT {', '.join(cols)} FROM {table} ORDER BY {', '.join(cols)}"):
                digest.update(canonical_json(list(row)).encode("utf-8") + b"\n")
            digests[table] = digest.hexdigest()
    return digests


def coverage(db_path: Path) -> dict[str, Any]:
    with closing(_open(db_path)) as conn:
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


def verify(roots: Sequence[Path], db_path: Path, *, scratch: Path | None) -> dict[str, Any]:
    report: dict[str, Any] = {"coverage": coverage(db_path)}
    ex = extract(iter_layer1(roots))
    resolution = resolve(ex.identity)
    with closing(_open(db_path)) as conn:
        message_ids = {row[0] for row in conn.execute("SELECT message_id FROM messages")}
    _events(ex, resolution, resolution.role_contact.get("assistant"), message_ids, resolution.review)
    report["review"] = resolution.review
    accounted: Counter[str] = Counter()
    for (file, _), count in ex.outcomes.items():
        accounted[file] += count
    lines = {rel: sum(1 for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
                      if line.strip()) for rel, path in layer1_files(roots)}
    report["accounting"] = {rel: {"lines": count, "accounted": accounted.get(rel, 0)}
                            for rel, count in lines.items()}
    report["accounting_ok"] = all(item["lines"] == item["accounted"]
                                   for item in report["accounting"].values())
    if scratch is not None:
        scratch.mkdir(parents=True, exist_ok=True, mode=0o700)
        first, second = scratch / "rebuild-a.db", scratch / "rebuild-b.db"
        project(roots, first)
        project(roots, second)
        digests = table_digest(db_path)
        report["deterministic"] = table_digest(first) == table_digest(second) == digests
        report["digests"] = digests
    return report
