"""Proof-only sync from history into the directory; owner rows, columns and decisions always win."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass

from .history_source import ObservedPerson
from .identifiers import ObservedId
from .store import PeopleStore

_HISTORY_CONTACT = "history_contact"


@dataclass(slots=True)
class SyncReport:
    persons_created: int = 0
    persons_merged: int = 0
    persons_unmerged: int = 0
    persons_split: int = 0
    ids_added: int = 0
    ids_skipped_hidden: int = 0
    ids_conflicts: int = 0
    names_added: int = 0
    names_skipped_hidden: int = 0
    suggestions_added: int = 0
    owner_decisions_applied: int = 0

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


def sync_from_history(store: PeopleStore, observed: list[ObservedPerson], now_ms: int) -> SyncReport:
    """Apply one pass; the caller owns the transaction (``store.transaction("sync")``)."""
    report = SyncReport()
    _apply_owner_decisions(store, report)
    targets = _targets(store, observed, now_ms, report)
    targets = _resolve_splits(store, observed, targets, now_ms, report)
    owner_taken = store.conn.execute("SELECT 1 FROM persons WHERE is_owner = 1").fetchone() is not None
    for op, target in zip(observed, targets, strict=True):
        _attach(store, op, target, now_ms, report)
        row = store.person_rows(target)
        level = "contact" if "contact" in (op.level, row["level"]) else "seen"
        last_seen = max((v for v in (row["last_seen_ms"], op.last_seen_ms) if v is not None), default=None)
        is_owner = None
        if op.is_owner and not row["is_owner"] and not owner_taken:
            is_owner, owner_taken = True, True
        store.set_sync_fields(target, level=level, last_seen_ms=last_seen, is_owner=is_owner)
    return report


def _latest_decisions(store: PeopleStore) -> dict[tuple[int, int], str]:
    latest: dict[tuple[int, int], str] = {}
    for a, b, decision in store.conn.execute(
            "SELECT person_a, person_b, decision FROM person_decisions ORDER BY decided_ms, id"):
        latest[(min(a, b), max(a, b))] = str(decision)
    return latest


def _chain(store: PeopleStore, person_id: int) -> list[int]:
    """Persons from ``person_id`` up to (excluding) its terminal."""
    terminal = store.terminal(person_id)
    chain, current = [], person_id
    while current != terminal:
        chain.append(current)
        current = int(store.person_rows(current)["merged_into"])
    return chain


def _apply_owner_decisions(store: PeopleStore, report: SyncReport) -> None:
    for (a, b), decision in _latest_decisions(store).items():
        ta, tb = store.terminal(a), store.terminal(b)
        if decision == "same" and ta != tb:
            if store.merge(max(ta, tb), min(ta, tb), by="owner"):
                report.owner_decisions_applied += 1
        elif decision == "not_same" and ta == tb:
            while store.terminal(a) == store.terminal(b):
                sync_links = [p for p in _chain(store, a) + _chain(store, b)
                              if store.person_rows(p)["merged_by"] == "sync"]
                if not sync_links:
                    if store.suggest("merge", a, b, evidence={"conflict": "not_same_vs_owner_merge"}):
                        report.suggestions_added += 1
                    break
                store.unmerge_sync(sync_links[0])
                report.persons_unmerged += 1
                report.owner_decisions_applied += 1


def _active_ids(op: ObservedPerson) -> list[tuple[str, str]]:
    keys = [(_HISTORY_CONTACT, member) for member in op.history_members]
    keys += [(i.kind, i.value) for i in op.ids if i.valid_until_ms is None]
    return keys


def _targets(store: PeopleStore, observed: list[ObservedPerson], now_ms: int,
             report: SyncReport) -> list[int]:
    targets = []
    for op in observed:
        found = {store.terminal(p) for kind, value in _active_ids(op)
                 if (p := store.find_active(kind, value)) is not None}
        if not found:
            targets.append(store.create_person(level=op.level, now_ms=now_ms))
            report.persons_created += 1
            continue
        target = min(found)
        for other in sorted(found - {target}):
            if store.merge(other, target, by="sync"):
                report.persons_merged += 1
            else:
                report.ids_conflicts += 1
                if store.suggest("merge", target, other, evidence={"proof": "history_terminal"}):
                    report.suggestions_added += 1
        targets.append(store.terminal(target))
    return targets


def _home(store: PeopleStore, op: ObservedPerson) -> tuple[int, int] | None:
    """(person_id, row id) of the lowest active history_contact row of this observed person."""
    best = None
    for member in op.history_members:
        row = store.conn.execute(
            "SELECT id, person_id FROM person_ids WHERE kind = ? AND value = ? "
            "AND valid_until_ms IS NULL AND hidden = 0", (_HISTORY_CONTACT, member)).fetchone()
        if row is not None and (best is None or row["id"] < best[1]):
            best = (int(row["person_id"]), int(row["id"]))
    return best


def _owner_linked(store: PeopleStore, person: int, terminal: int) -> bool:
    chain = _chain(store, person)
    if any(store.person_rows(p)["merged_by"] == "owner" for p in chain):
        return True
    return any(store.latest_decision(p, q) == "same" for p in [person, *chain]
               for q in store.members(terminal) if p != q)


def _resolve_splits(store: PeopleStore, observed: list[ObservedPerson], targets: list[int],
                    now_ms: int, report: SyncReport) -> list[int]:
    by_target: dict[int, list[int]] = defaultdict(list)
    for index, target in enumerate(targets):
        by_target[target].append(index)
    result = list(targets)
    for terminal, indexes in by_target.items():
        if len(indexes) < 2:
            continue
        homes = {index: _home(store, observed[index]) for index in indexes}
        on_terminal = []
        for index in indexes:
            home = homes[index]
            if home is None:
                continue
            person = home[0]
            if person == terminal:
                on_terminal.append(index)
                continue
            if store.terminal(person) != terminal or _owner_linked(store, person, terminal):
                continue
            for link in _chain(store, person):
                if store.person_rows(link)["merged_by"] == "sync":
                    store.unmerge_sync(link)
                    report.persons_unmerged += 1
                    break
            result[index] = store.terminal(person)
        # Several observed persons on one person row (merged in history before the first fill).
        if len(on_terminal) >= 2:
            keeper = min(on_terminal, key=lambda i: homes[i][1])
            for index in on_terminal:
                if index == keeper:
                    continue
                result[index] = _split_off(store, observed[index], terminal, now_ms)
                report.persons_split += 1
    return result


def _split_off(store: PeopleStore, op: ObservedPerson, person: int, now_ms: int) -> int:
    fresh = store.create_person(level=op.level, now_ms=now_ms)
    keys = set(_active_ids(op)) | {(i.kind, i.value) for i in op.ids}
    rows = [int(r["id"]) for r in store.conn.execute(
        "SELECT id, kind, value FROM person_ids WHERE person_id = ? AND source = 'observed'", (person,))
        if (r["kind"], r["value"]) in keys]
    store.move_observed_ids(rows, fresh)
    return fresh


def _attach(store: PeopleStore, op: ObservedPerson, target: int, now_ms: int,
            report: SyncReport) -> None:
    idents = [ObservedId(_HISTORY_CONTACT, member) for member in op.history_members] + list(op.ids)
    for ident in idents:
        outcome = store.attach_id(target, ident, now_ms)
        if outcome == "added":
            report.ids_added += 1
        elif outcome == "hidden":
            report.ids_skipped_hidden += 1
        elif outcome == "taken":
            report.ids_conflicts += 1
    for name, kind in op.names:
        outcome = store.add_observed_name(target, name, kind, now_ms)
        if outcome == "added":
            report.names_added += 1
        elif outcome == "hidden":
            report.names_skipped_hidden += 1
