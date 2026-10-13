"""Read-only integrity check of people.db; findings never carry names or identifier values."""

from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import date

from .identifiers import is_valid_value

_LANGUAGE = re.compile(r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})*$")
_INTERNAL_KINDS = frozenset({"history_contact", "knowledge_person"})


@dataclass(frozen=True, slots=True)
class Finding:
    severity: str        # 'error' | 'warning' | 'info'
    code: str
    table: str
    row_id: int
    detail: str          # no personal values except the offending identifier kind; never names


def _real_date(value: str) -> bool:
    try:
        if value.startswith("--"):
            date.fromisoformat("2000" + value[1:])      # leap year: --02-29 is valid
        else:
            date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _walk(parents: dict[int, int | None], start: int) -> tuple[int | None, int]:
    """(terminal or None on a cycle, hops)."""
    seen, current, hops = {start}, start, 0
    while (parent := parents.get(current)) is not None:
        if parent in seen or parent not in parents:
            return None, hops
        seen.add(parent)
        current, hops = parent, hops + 1
    return current, hops


def _overlaps(a: tuple[int | None, int | None], b: tuple[int | None, int | None]) -> bool:
    inf = float("inf")
    a_from, a_until = a[0] if a[0] is not None else -inf, a[1] if a[1] is not None else inf
    b_from, b_until = b[0] if b[0] is not None else -inf, b[1] if b[1] is not None else inf
    return a_from < b_until and b_from < a_until


def check_people_db(conn: sqlite3.Connection) -> list[Finding]:
    findings: list[Finding] = []
    for table, rowid, parent, _fkid in conn.execute("PRAGMA foreign_key_check"):
        findings.append(Finding("error", "foreign_key", str(table), int(rowid or 0),
                                f"references a missing {parent} row"))

    parents = {int(r[0]): (int(r[1]) if r[1] is not None else None)
               for r in conn.execute("SELECT id, merged_into FROM persons")}
    terminal: dict[int, int | None] = {}
    for person in parents:
        term, hops = _walk(parents, person)
        terminal[person] = term
        if term is None:
            findings.append(Finding("error", "merge_cycle", "persons", person,
                                    "merged_into chain revisits a person or ends at a missing one"))
        elif hops > 1:
            findings.append(Finding("info", "merge_chain", "persons", person,
                                    f"merged_into chain of {hops} hops"))

    rows = conn.execute("SELECT id, person_id, kind, value, valid_from_ms, valid_until_ms "
                        "FROM person_ids WHERE hidden = 0").fetchall()
    by_value: dict[tuple[str, str], list] = defaultdict(list)
    for row_id, person, kind, value, v_from, v_until in rows:
        by_value[(kind, value)].append((row_id, terminal.get(person, person), (v_from, v_until)))
        if kind not in _INTERNAL_KINDS and not is_valid_value(kind, value):
            findings.append(Finding("warning", "invalid_value", "person_ids", row_id,
                                    f"{kind} value is not normalized"))
    for (kind, _value), entries in by_value.items():
        for i, (row_a, term_a, span_a) in enumerate(entries):
            for row_b, term_b, span_b in entries[i + 1:]:
                if term_a != term_b and _overlaps(span_a, span_b):
                    findings.append(Finding("error", "validity_overlap", "person_ids", row_b,
                                            f"{kind} also held by another person (row {row_a}) in the same period"))

    for person, birthday, language in conn.execute(
            "SELECT id, birthday, preferred_language FROM persons"):
        if birthday is not None and not _real_date(str(birthday)):
            findings.append(Finding("error", "invalid_birthday", "persons", person, "birthday is not a real date"))
        if language is not None and not _LANGUAGE.match(str(language)):
            findings.append(Finding("warning", "invalid_language", "persons", person,
                                    "preferred_language is not a BCP 47 tag"))

    nicknames: dict[str, set[int | None]] = defaultdict(set)
    first_row: dict[str, int] = {}
    for row_id, person, name in conn.execute(
            "SELECT id, person_id, name FROM person_names WHERE source = 'owner' AND hidden = 0 ORDER BY id"):
        key = str(name).casefold()
        nicknames[key].add(terminal.get(person, person))
        first_row.setdefault(key, row_id)
    for key, terminals in nicknames.items():
        if len(terminals) > 1:
            findings.append(Finding("info", "ambiguous_owner_nickname", "person_names", first_row[key],
                                    f"owner nickname used for {len(terminals)} persons"))

    row = conn.execute("SELECT name FROM writer_context WHERE id = 1").fetchone()
    if row is not None and row[0] is not None and not conn.in_transaction:
        findings.append(Finding("error", "writer_context_stuck", "writer_context", 1,
                                "writer label left set outside a transaction"))
    return findings
