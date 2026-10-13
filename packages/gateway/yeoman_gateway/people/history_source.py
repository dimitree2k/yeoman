"""Read observed persons from a projected history.db snapshot (read-only, grouped queries)."""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass

from .identifiers import ObservedId, identifier_from_history

_GROUP_SUFFIX = "@g.us"


@dataclass(frozen=True, slots=True)
class ObservedPerson:
    history_terminal: str                  # terminal history contact_id
    history_members: tuple[str, ...]       # contact_ids whose terminal is history_terminal, sorted
    ids: tuple[ObservedId, ...]            # phone / wa_lid with validity, deduplicated
    names: tuple[tuple[str, str], ...]     # (name, kind), kind 'push_name', deduplicated
    is_owner: bool
    level: str                             # 'contact' | 'seen'
    last_seen_ms: int | None


def _terminals(parents: dict[str, str | None]) -> dict[str, str]:
    """Follow merged_into to the terminal contact; cycles and dangling links stop where detected."""
    result: dict[str, str] = {}
    for start in parents:
        path: list[str] = []
        current = start
        while True:
            if current in result:
                terminal = result[current]
                break
            if current in path:
                terminal = current
                break
            path.append(current)
            parent = parents.get(current)
            if parent is None or parent not in parents:
                terminal = current
                break
            current = parent
        for contact in path:
            result[contact] = terminal
    return result


def _max(*values: int | None) -> int | None:
    present = [v for v in values if v is not None]
    return max(present) if present else None


def _min(*values: int | None) -> int | None:
    present = [v for v in values if v is not None]
    return min(present) if present else None


def read_observed_persons(conn: sqlite3.Connection, *,
                          contact_group_max_members: int = 100) -> list[ObservedPerson]:
    contacts = conn.execute(
        "SELECT contact_id, role, display_name, merged_into FROM contacts WHERE kind = 'person'"
    ).fetchall()
    parents = {str(c[0]): (str(c[3]) if c[3] is not None else None) for c in contacts}
    terminal_of = _terminals(parents)
    members: dict[str, list[str]] = defaultdict(list)
    for contact_id, terminal in terminal_of.items():
        members[terminal].append(contact_id)
    roles: dict[str, set[str]] = defaultdict(set)
    names: dict[str, list[str]] = defaultdict(list)
    for contact_id, role, display_name, _ in contacts:
        terminal = terminal_of[str(contact_id)]
        if role:
            roles[terminal].add(str(role))
        if display_name:
            names[terminal].append(str(display_name))

    ids: dict[str, dict[tuple, ObservedId]] = defaultdict(dict)
    raw_values: dict[str, str] = {}
    newsletter: set[str] = set()
    seen_ms: dict[str, int | None] = {}
    for contact_id, kind, value, first, last, ended, v_from, v_until in conn.execute(
            "SELECT contact_id, kind, value, first_seen_ms, last_seen_ms, ended_ms, valid_from_ms, "
            "valid_until_ms FROM identifier_history"):
        terminal = terminal_of.get(str(contact_id))
        if terminal is None:
            continue
        seen_ms[terminal] = _max(seen_ms.get(terminal), last)
        if kind == "push_name":
            names[terminal].append(str(value))
            continue
        if kind == "newsletter":
            newsletter.add(terminal)
            continue
        raw_values[str(value).lower()] = terminal
        ident = identifier_from_history(str(kind), str(value), valid_from_ms=v_from,
                                        valid_until_ms=v_until if v_until is not None else ended,
                                        first_seen_ms=first, last_seen_ms=last)
        if ident is None:
            continue
        key = (ident.kind, ident.value, ident.valid_from_ms, ident.valid_until_ms)
        prior = ids[terminal].get(key)
        if prior is not None:
            ident = ObservedId(ident.kind, ident.value, ident.valid_from_ms, ident.valid_until_ms,
                               _min(prior.first_seen_ms, ident.first_seen_ms),
                               _max(prior.last_seen_ms, ident.last_seen_ms))
        ids[terminal][key] = ident

    group_size: dict[str, int] = {
        str(chat_id): int(size or 0) for chat_id, size in conn.execute("""
            SELECT chat_id, json_array_length(json_extract(payload_json, '$.participants'))
              FROM (SELECT chat_id, payload_json,
                           row_number() OVER (PARTITION BY chat_id
                                              ORDER BY occurred_ms DESC, event_id DESC) AS rank
                      FROM message_events WHERE kind = 'member_snapshot')
             WHERE rank = 1""")
    }
    contact_level: set[str] = set()
    for sender, chat_id, last in conn.execute(
            "SELECT sender_contact_id, chat_id, max(sent_ms) FROM messages "
            "WHERE sender_contact_id IS NOT NULL GROUP BY sender_contact_id, chat_id"):
        terminal = terminal_of.get(str(sender))
        if terminal is None:
            continue
        seen_ms[terminal] = _max(seen_ms.get(terminal), last)
        chat = str(chat_id)
        if not chat.endswith(_GROUP_SUFFIX):
            contact_level.add(terminal)
        elif group_size.get(chat, 0) <= contact_group_max_members:
            contact_level.add(terminal)     # small group, or a group without a member snapshot
    for (chat_id,) in conn.execute("SELECT DISTINCT chat_id FROM messages"):
        terminal = raw_values.get(str(chat_id).lower())
        if terminal is not None:
            contact_level.add(terminal)     # direct-chat counterpart

    persons = []
    for terminal in sorted(members):
        if "assistant" in roles[terminal]:
            continue
        observed = tuple(sorted(ids[terminal].values(),
                                key=lambda i: (i.kind, i.value, i.valid_from_ms or 0, i.valid_until_ms or 0)))
        if terminal in newsletter and not observed:
            continue
        is_owner = "owner" in roles[terminal]
        persons.append(ObservedPerson(
            history_terminal=terminal,
            history_members=tuple(sorted(members[terminal])),
            ids=observed,
            names=tuple((name, "push_name") for name in dict.fromkeys(n.strip() for n in names[terminal])
                        if name),
            is_owner=is_owner,
            level="contact" if is_owner or terminal in contact_level else "seen",
            last_seen_ms=seen_ms.get(terminal),
        ))
    return persons
