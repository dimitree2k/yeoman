"""Read-only identifier/validity bridges; names never establish subject identity."""

from __future__ import annotations

from dataclasses import dataclass

from .identifiers import identifier_from_history
from .resolver import PeopleUnavailable, ReaderContext, _active


@dataclass(frozen=True, slots=True)
class PersonIdentifier:
    kind: str
    value: str
    valid_from_ms: int | None
    valid_until_ms: int | None


@dataclass(frozen=True, slots=True)
class HistoryPerson:
    person_id: int
    identifiers: tuple[PersonIdentifier, ...]
    contact_intervals: tuple[PersonIdentifier, ...]
    names: tuple[str, ...]


def _segments(rows, *, start=None, end=None):
    cuts = sorted(
        {
            r[key]
            for r in rows
            for key in ("valid_from_ms", "valid_until_ms")
            if r[key] is not None
            and (start is None or r[key] > start)
            and (end is None or r[key] < end)
        }
    )
    for lo, hi in zip([start, *cuts], [*cuts, end], strict=True):
        if lo is None or hi is None or lo < hi:
            yield lo, hi, lo if lo is not None else (hi - 1 if hi is not None else 0)


def _visible(index, terminal):
    rows = index.identifiers_by_terminal.get(terminal, ())
    hidden = {(r["kind"], r["value"]) for r in rows if r["hidden"]}
    return tuple(r for r in rows if not r["hidden"] and (r["kind"], r["value"]) not in hidden)


def _coalesce(values):
    grouped = {}
    for row in values:
        grouped.setdefault((row.kind, row.value), set()).add(
            (row.valid_from_ms, row.valid_until_ms)
        )
    result = []
    for (kind, value), spans in sorted(grouped.items()):
        ordered = sorted(spans, key=lambda s: (s[0] is not None, s[0] or 0))
        lo, hi = ordered[0]
        for start, end in ordered[1:]:
            if hi is None or start is None or start <= hi:
                hi = None if hi is None or end is None else max(hi, end)
            else:
                result.append(PersonIdentifier(kind, value, lo, hi))
                lo, hi = start, end
        result.append(PersonIdentifier(kind, value, lo, hi))
    return tuple(result)


def _bindings(reader, index, terminal):
    directory = reader.directory
    snapshot = reader.history
    if snapshot is None:
        from yeoman_gateway.history.live import HistoryPaused

        raise HistoryPaused("reader_unavailable")
    snapshot.assert_current(snapshot.generation)

    def load():
        evidence = directory._evidence(reader)
        by_person, owners = {}, {}
        contacts = set()
        for row in _visible(index, terminal):
            if row["kind"] not in ("phone", "wa_lid"):
                continue
            kind = "pn_jid" if row["kind"] == "phone" else "lid"
            value = row["value"] + ("@s.whatsapp.net" if kind == "pn_jid" else "@lid")
            contacts.update(
                evidence.terminal(r["contact_id"])
                for r in evidence.identifiers.get((kind, value), ())
            )
        keys = set()
        for contact, rows in evidence.by_contact.items():
            if evidence.terminal(contact) in contacts:
                keys.update((r["kind"], r["value"]) for r in rows)
        for kind, value in sorted(keys):
            history_rows = evidence.identifiers[(kind, value)]
            mapped = identifier_from_history(kind, value)
            if mapped is None:
                continue
            rows = index.identifier_rows.get((mapped.kind, mapped.value), ())
            if not rows:
                continue
            for lo, hi, at in _segments((*rows, *history_rows)):
                people = index.matches(mapped.kind, mapped.value, at)
                contact = evidence.owner(value, at)
                if len(people) != 1 or contact is None:
                    continue
                person = next(iter(people))
                by_person.setdefault(person, []).append(
                    (contact, PersonIdentifier(kind, value, lo, hi))
                )
                owners.setdefault(contact, set()).add(person)
        return by_person, owners

    return directory._memo(("person-bindings", snapshot, snapshot.generation, terminal), load)


def _selected(person_id, reader):
    directory = reader.directory
    index = directory._index()
    scope = directory._scope(reader, index)
    terminal = index.terminal(person_id)
    if terminal is None or (reader.kind != "owner_dm" and terminal not in scope.eligible_ids):
        raise PeopleUnavailable("people_out_of_scope")
    return index, terminal


def history_person(person_id: int, reader: ReaderContext) -> HistoryPerson:
    index, terminal = _selected(person_id, reader)
    directory = reader.directory
    key = directory._reader_key(reader)

    def load():
        bindings, owners = _bindings(reader, index, terminal)
        evidence = directory._evidence(reader)
        rows = _visible(index, terminal)
        links = tuple(
            r
            for r in rows
            if r["kind"] == "history_contact" and evidence.terminal(r["value"]) is not None
        )
        identifiers, contacts = [], []
        for contact, ident in bindings.get(terminal, ()):
            for lo, hi, at in _segments(links, start=ident.valid_from_ms, end=ident.valid_until_ms):
                invalid = any(
                    _active(link, at)
                    and terminal not in owners.get(evidence.terminal(link["value"]), ())
                    for link in links
                )
                if invalid:
                    directory._local.mapping_unavailable = (
                        getattr(directory._local, "mapping_unavailable", 0) + 1
                    )
                    continue
                matching = [link for link in links if evidence.terminal(link["value"]) == contact]
                if matching and not any(_active(link, at) for link in matching):
                    continue
                identifiers.append(PersonIdentifier(ident.kind, ident.value, lo, hi))
                # A contact shared by directory people cannot attribute identifier-less messages.
                if owners.get(contact) == {terminal}:
                    contacts.append(PersonIdentifier("history_contact", contact, lo, hi))
        canonical = _coalesce(contacts)
        contact_rows = []
        for value in canonical:
            for alias in evidence.contacts:
                if evidence.terminal(alias) == value.value:
                    contact_rows.append(
                        PersonIdentifier(
                            "history_contact", alias, value.valid_from_ms, value.valid_until_ms
                        )
                    )
        names = tuple(
            sorted(
                {raw for raw, _, _ in index.aliases_by_terminal.get(terminal, ())},
                key=lambda n: (n.casefold(), n),
            )
        )
        return HistoryPerson(terminal, _coalesce(identifiers), _coalesce(contact_rows), names)

    return directory._memo(("history-person", key, terminal), load)


def knowledge_person_ids(person_id: int, reader: ReaderContext) -> tuple[str, ...]:
    index, terminal = _selected(person_id, reader)
    directory = reader.directory

    def load():
        result = set()
        for row in _visible(index, terminal):
            if row["kind"] != "knowledge_person":
                continue
            linked = {
                index.terminal(r["person_id"])
                for r in index.identifier_rows.get(("knowledge_person", row["value"]), ())
                if not r["hidden"]
            }
            if linked == {terminal}:
                result.add(row["value"])
            else:
                directory._local.mapping_unavailable = (
                    getattr(directory._local, "mapping_unavailable", 0) + 1
                )
        if reader.history is not None and reader.channel == "whatsapp":
            person = history_person(terminal, reader)
            evidence = directory._evidence(reader)
            _, owners = _bindings(reader, index, terminal)
            if any(
                evidence.terminal(subject) is not None
                and owners.get(evidence.terminal(subject)) != {terminal}
                for subject in result
            ):
                directory._local.mapping_unavailable = (
                    getattr(directory._local, "mapping_unavailable", 0) + 1
                )
                return ()
            result.update(evidence.terminal(r.value) for r in person.contact_intervals)
        return tuple(sorted(result))

    return directory._memo(("knowledge-person", directory._reader_key(reader), terminal), load)
