"""Read-only temporal person recognition. Reader permissions never enter the shared index."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import unicodedata
from bisect import bisect_left, bisect_right
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal

from .identifiers import identifier_from_history, normalize_phone
from .schema import open_people_db

if TYPE_CHECKING:
    from yeoman_gateway.history.reader import HistorySnapshot


class PeopleUnavailable(RuntimeError):  # noqa: N818 - Public directory error contract.
    def __init__(self, code: str = "people_unavailable") -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ReaderContext:
    directory: PeopleResolver
    channel: str
    chat_id: str
    principal_id: str
    at_ms: int
    kind: Literal["owner_dm", "group", "other_dm"]
    history: HistorySnapshot | None
    national_calling_code: str | None = None


@dataclass(frozen=True, slots=True)
class Candidate:
    person_id: int


@dataclass(frozen=True, slots=True)
class Resolution:
    status: Literal["resolved", "ambiguous", "not_found"]
    person_id: int | None = None
    candidates: tuple[Candidate, ...] = ()


@dataclass(frozen=True, slots=True)
class ReaderScope:
    eligible_ids: frozenset[int]
    member_ids: frozenset[int]
    shared_chats: tuple[str, ...]


def name_tokens(value: str) -> tuple[str, ...]:
    return tuple(re.findall(r"[^\W\d_]+|\d+", unicodedata.normalize("NFKC", value).casefold()))


def _active(row, at_ms: int) -> bool:
    return (row["valid_from_ms"] is None or row["valid_from_ms"] <= at_ms) and (
        row["valid_until_ms"] is None or at_ms < row["valid_until_ms"]
    )


@dataclass(frozen=True, slots=True)
class _Index:
    persons: Mapping[int, sqlite3.Row]
    terminals: Mapping[int, int]
    names: tuple[sqlite3.Row, ...]
    identifiers: tuple[sqlite3.Row, ...]
    aliases: tuple[tuple[int, str, tuple[str, ...], bool], ...]
    identifier_rows: Mapping[tuple[str, str], tuple[sqlite3.Row, ...]]

    identifiers_by_terminal: Mapping[int, tuple[sqlite3.Row, ...]]
    aliases_by_terminal: Mapping[int, tuple]
    exact_names: tuple[Mapping, Mapping]
    token_aliases: Mapping[str, frozenset[int]]
    words: tuple[str, ...]
    alias_width: int
    contact_ids: frozenset[int]
    terminal_ids: frozenset[int]
    channel_people: Mapping[str, frozenset[int]]

    def terminal(self, person: int) -> int | None:
        return self.terminals.get(person)

    def matches(self, kind: str, value: str, at_ms: int) -> set[int]:
        rows = self.identifier_rows.get((kind, value), ())
        hidden = {self.terminal(r["person_id"]) for r in rows if r["hidden"]}
        return {
            self.terminal(r["person_id"]) for r in rows if not r["hidden"] and _active(r, at_ms)
        } - hidden

    def display_name(self, person: int) -> str:
        preferred = self.persons[person]["preferred_name"]
        if preferred and any(
            raw == preferred for raw, _, _ in self.aliases_by_terminal.get(person, ())
        ):
            return preferred
        names = sorted(
            (
                (not owner, raw.casefold(), raw)
                for raw, _, owner in self.aliases_by_terminal.get(person, ())
            )
        )
        return names[0][2] if names else "Unnamed person"


@dataclass(slots=True)
class _State:
    connection: sqlite3.Connection
    inode: tuple[int, int]
    version: int | None = None
    index: _Index | None = None
    memo: OrderedDict = field(default_factory=OrderedDict)


@dataclass(frozen=True, slots=True)
class _HistoryEvidence:
    contacts: Mapping
    identifiers: Mapping
    by_contact: Mapping

    def terminal(self, contact):
        from yeoman_gateway.history.live import HistoryPaused

        visited = set()
        while contact in self.contacts:
            if contact in visited:
                raise HistoryPaused("contact_redirect_cycle")
            visited.add(contact)
            target = self.contacts[contact]["merged_into"]
            if target is None:
                return contact
            contact = target
        return None

    def field(self, contact, key):
        row = self.contacts.get(contact)
        return row[key] if row is not None else None

    def principal_contact(self, principal, at_ms):
        from yeoman_gateway.history.queries import _principal_identifiers

        owners = {
            owner
            for value in _principal_identifiers(principal)
            if (owner := self.owner(value, at_ms)) is not None
        }
        return next(iter(owners)) if len(owners) == 1 else None

    def owner(self, value, at_ms):
        from yeoman_gateway.history.ids import classify

        ident = classify(value)
        if ident is None or not ident.strong:
            return None
        owners = {
            self.terminal(r["contact_id"])
            for r in self.identifiers.get((ident.kind, ident.value), ())
            if _active(r, at_ms)
        }
        return next(iter(owners)) if len(owners) == 1 and None not in owners else None


class PeopleResolver:
    """Each execution thread owns its connection; call close in each owning thread after use."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._local = threading.local()

    def close(self) -> None:
        state = getattr(self._local, "state", None)
        if state is not None:
            state.connection.close()
            del self._local.state
        if hasattr(self._local, "owner_codes"):
            del self._local.owner_codes

    def _index(self) -> _Index:
        try:
            info = self.db_path.stat()
            inode = info.st_dev, info.st_ino
            state = getattr(self._local, "state", None)
            if state is not None and state.inode != inode:
                self.close()
                state = None
            if state is None:
                state = _State(
                    open_people_db(self.db_path, readonly=True, busy_timeout_ms=0), inode
                )
                self._local.state = state
            conn = state.connection
            version = conn.execute("PRAGMA data_version").fetchone()[0]
            if state.index is None or state.version != version:
                conn.execute("BEGIN")
                try:
                    persons = {
                        r["id"]: r
                        for r in conn.execute(
                            "SELECT id,preferred_name,home_city,address,birthday,relation,preferred_language,notes,merged_into,is_owner,effective_level FROM persons_v"
                        )
                    }
                    names = tuple(
                        r
                        for r in conn.execute(
                            "SELECT person_id,name,source,hidden FROM person_names"
                        )
                    )
                    identifiers = tuple(
                        r
                        for r in conn.execute(
                            "SELECT person_id,kind,value,valid_from_ms,valid_until_ms,hidden FROM person_ids"
                        )
                    )
                finally:
                    conn.execute("ROLLBACK")
                terminals = {}
                for person in persons:
                    current, visited = person, set()
                    while True:
                        if current not in persons or current in visited or len(visited) > 64:
                            raise PeopleUnavailable("people_integrity")
                        visited.add(current)
                        target = persons[current]["merged_into"]
                        if target is None:
                            break
                        current = target
                    terminals[person] = current
                hidden = {
                    (terminals[r["person_id"]], name_tokens(r["name"]))
                    for r in names
                    if r["hidden"]
                }
                aliases = []
                for person, row in persons.items():
                    tokens = name_tokens(row["preferred_name"]) if row["preferred_name"] else ()
                    if tokens:
                        aliases.append(
                            (
                                terminals[person],
                                row["preferred_name"],
                                tokens,
                                True,
                            )
                        )
                for row in names:
                    terminal, tokens = terminals[row["person_id"]], name_tokens(row["name"])
                    if (
                        tokens
                        and not row["hidden"]
                        and not (row["source"] == "observed" and (terminal, tokens) in hidden)
                    ):
                        aliases.append((terminal, row["name"], tokens, row["source"] == "owner"))
                identifier_rows = {}
                for row in identifiers:
                    identifier_rows.setdefault((row["kind"], row["value"]), []).append(row)
                by_terminal, alias_by_terminal, token_aliases = {}, {}, {}
                exact_names = ({}, {})
                for row in identifiers:
                    by_terminal.setdefault(terminals[row["person_id"]], []).append(row)
                for position, (terminal, raw, tokens, owner) in enumerate(aliases):
                    alias_by_terminal.setdefault(terminal, []).append((raw, tokens, owner))
                    exact_names[0 if owner else 1].setdefault(tokens, set()).add(terminal)
                    for token in set(tokens):
                        token_aliases.setdefault(token, set()).add(position)
                channel_people = {}
                for (kind, value), rows in identifier_rows.items():
                    if kind in ("phone", "wa_lid"):
                        suffix = "@s.whatsapp.net" if kind == "phone" else "@lid"
                        channel_people[value + suffix] = frozenset(
                            terminals[r["person_id"]] for r in rows
                        )
                state.memo.clear()
                state.index = _Index(
                    MappingProxyType(persons),
                    MappingProxyType(terminals),
                    names,
                    identifiers,
                    tuple(aliases),
                    MappingProxyType({key: tuple(rows) for key, rows in identifier_rows.items()}),
                    MappingProxyType({p: tuple(rows) for p, rows in by_terminal.items()}),
                    MappingProxyType({p: tuple(rows) for p, rows in alias_by_terminal.items()}),
                    tuple(
                        MappingProxyType({k: frozenset(v) for k, v in names.items()})
                        for names in exact_names
                    ),
                    MappingProxyType({k: frozenset(v) for k, v in token_aliases.items()}),
                    tuple(sorted(token_aliases)),
                    max((len(tokens) for _, _, tokens, _ in aliases), default=0),
                    frozenset(
                        p
                        for p in persons
                        if terminals[p] == p
                        and persons[p]["effective_level"] in ("curated", "contact")
                    ),
                    frozenset(terminals.values()),
                    MappingProxyType(channel_people),
                )
                after = conn.execute("PRAGMA data_version").fetchone()[0]
                state.version = version if after == version else None
            return state.index
        except (OSError, sqlite3.Error, PeopleUnavailable) as exc:
            self.close()
            code = exc.code if isinstance(exc, PeopleUnavailable) else "people_unavailable"
            raise PeopleUnavailable(code) from None

    def _memo(self, key, load):
        memo = self._local.state.memo
        if key in memo:
            memo.move_to_end(key)
            return memo[key]
        try:
            value = load()
        except sqlite3.Error:
            raise PeopleUnavailable() from None
        memo[key] = value
        if len(memo) > 128:
            memo.popitem(last=False)
        return value

    @staticmethod
    def _reader_key(reader):
        history = reader.history
        if history is not None:
            history.assert_current(history.generation)
        return (
            history,
            history.generation if history else None,
            reader.channel,
            reader.chat_id,
            reader.principal_id,
            reader.kind,
            reader.at_ms,
            reader.national_calling_code,
        )

    def scope(self, reader: ReaderContext) -> ReaderScope:
        index = self._index()
        scope = self._scope(reader, index)
        return replace(scope, shared_chats=self._shared_chats(reader, index))

    def _scope(self, reader: ReaderContext, index: _Index) -> ReaderScope:
        return self._memo(
            ("scope", self._reader_key(reader)), lambda: self._read_scope(reader, index)
        )

    def _read_scope(self, reader: ReaderContext, index: _Index) -> ReaderScope:
        if reader.kind == "owner_dm":
            return ReaderScope(index.contact_ids, frozenset(), ())
        if reader.history is None or reader.channel != "whatsapp":
            endpoint = self._endpoint(reader.chat_id, reader, index)
            principal = self._endpoint(reader.principal_id, reader, index)
            eligible = endpoint if reader.kind == "other_dm" and endpoint == principal else set()
            return ReaderScope(frozenset(eligible), frozenset(), ())
        if reader.kind == "group":
            members, _ = self._roster(reader.chat_id, reader, index)
            authors = self._authors(reader.chat_id, reader, index)
            return ReaderScope(members | authors, members, ())
        eligible = self._endpoint(reader.chat_id, reader, index)
        shared = self._shared_chats(reader, index)
        for chat in shared:
            members, _ = self._roster(chat, reader, index)
            eligible.update(members | self._authors(chat, reader, index))
        return ReaderScope(frozenset(eligible), frozenset(), shared)

    def _evidence(self, reader):

        snapshot = reader.history
        snapshot.assert_current(snapshot.generation)

        def load():
            cursor = snapshot.connection.cursor()
            cursor.row_factory = sqlite3.Row
            contacts = {
                r["contact_id"]: r
                for r in cursor.execute("SELECT contact_id,merged_into,status,role FROM contacts")
            }
            identifiers, by_contact = {}, {}
            for row in cursor.execute(
                "SELECT kind,value,contact_id,valid_from_ms,valid_until_ms FROM identifier_history WHERE channel='whatsapp' AND strength='strong'"
            ):
                identifiers.setdefault((row["kind"], row["value"]), []).append(row)
                by_contact.setdefault(row["contact_id"], []).append(row)
            cursor.close()
            snapshot.assert_current(snapshot.generation)
            return _HistoryEvidence(contacts, identifiers, by_contact)

        return self._memo(("evidence", snapshot, snapshot.generation), load)

    def _assistant_principals(self, reader):
        def load():
            evidence = self._evidence(reader)
            result = set()
            # All roles and bindings come from two snapshot-local bulk reads, never per-member SQL.
            for rows in evidence.identifiers.values():
                for row in rows:
                    terminal = evidence.terminal(row["contact_id"])
                    if (
                        evidence.field(terminal, "role") == "assistant"
                        and _active(row, reader.at_ms)
                        and evidence.owner(row["value"], reader.at_ms) == terminal
                    ):
                        value = row["value"]
                        if row["kind"] == "pn_jid":
                            result.add("whatsapp:" + value.split("@")[0])
                        result.add("whatsapp:" + value)
            return frozenset(
                principal
                for principal in result
                if evidence.field(evidence.principal_contact(principal, reader.at_ms), "role")
                == "assistant"
            )

        return self._memo(
            ("assistants", reader.history, reader.history.generation, reader.at_ms), load
        )

    def _roster(self, chat, reader, index):
        from yeoman_gateway.history.queries import HistoryQueries

        snapshot = reader.history
        snapshot.assert_current(snapshot.generation)

        def load():
            queries = self._memo(
                ("queries", snapshot, snapshot.generation), lambda: HistoryQueries(snapshot)
            )
            audience = queries.members(chat_id=chat, at_ms=reader.at_ms)
            if audience.status != "known":
                return frozenset(), False
            people = set()
            principals = self._memo(
                ("principals", snapshot, snapshot.generation, reader.at_ms), dict
            )
            for principal in audience.members:
                if principal not in principals:
                    value = principal.removeprefix("whatsapp:")
                    if value.isascii() and value.isdigit() and not value.startswith("0"):
                        found = index.matches("wa_lid", value, reader.at_ms)
                        phone = normalize_phone(value)
                        if phone:
                            found |= index.matches("phone", phone, reader.at_ms)
                    else:
                        ident = identifier_from_history("lid", value)
                        found = (
                            index.matches(ident.kind, ident.value, reader.at_ms)
                            if ident
                            else self._endpoint(principal, reader, index)
                        )
                    principals[principal] = frozenset(found) if len(found) == 1 else frozenset()
                people.update(principals[principal])
            assistant = bool(set(audience.members) & self._assistant_principals(reader))
            return frozenset(people), assistant

        return self._memo(("roster", snapshot, snapshot.generation, chat, reader.at_ms), load)

    def _author_at(self, value, contact, at_ms, reader, index, evidence):
        if value:
            if evidence.owner(value, at_ms) == contact:
                return self._endpoint(value, reader, index, at_ms=at_ms)
            return set()
        linked = index.matches("history_contact", contact, at_ms)
        proof = set()
        for ident in evidence.by_contact.get(contact, ()):
            if evidence.owner(ident["value"], at_ms) == contact:
                proof.update(self._endpoint(ident["value"], reader, index, at_ms=at_ms))
        return proof if len(proof) == 1 and proof == linked else set()

    def _author_boundaries(self, value, contact, index, evidence):
        from yeoman_gateway.history.ids import classify

        rows = []
        values = [value] if value else [r["value"] for r in evidence.by_contact.get(contact, ())]
        if not value:
            rows.extend(index.identifier_rows.get(("history_contact", contact), ()))
        for raw in values:
            ident = classify(raw)
            if ident is None:
                continue
            rows.extend(evidence.identifiers.get((ident.kind, ident.value), ()))
            mapped = identifier_from_history(ident.kind, ident.value)
            if mapped:
                rows.extend(index.identifier_rows.get((mapped.kind, mapped.value), ()))
        return {
            r[key]
            for r in rows
            for key in ("valid_from_ms", "valid_until_ms")
            if r[key] is not None
        }

    def _authors(self, chat, reader, index):
        from yeoman_gateway.history.queries import HistoryQueries

        snapshot = reader.history
        snapshot.assert_current(snapshot.generation)

        known = self._roster(chat, reader, index)[0]
        # Current members are already eligible; only former/unknown authors need proof.
        excluded = self._memo(
            ("known-identifiers", known),
            lambda: json.dumps(
                [value for value, people in index.channel_people.items() if people <= known]
            ),
        )

        def load():
            queries = HistoryQueries(snapshot)
            evidence = self._evidence(reader)
            result = set()
            bindings = self._memo(("author-bindings", snapshot, snapshot.generation), OrderedDict)
            filters = (
                "channel='whatsapp' AND chat_id=? AND sent_ms<=?"
                " AND (sender_identifier IS NULL OR sender_identifier NOT IN (SELECT value FROM json_each(?)))"
                " AND deleted=0 AND sender_basis IN ('native_identifier','owner_attested')"
            )
            rows = queries._rows(
                "SELECT sender_identifier,sender_contact_id,MIN(sent_ms) AS first_ms,MAX(sent_ms) AS last_ms"
                " FROM messages_current WHERE "
                + filters
                + " GROUP BY sender_identifier,sender_contact_id",
                (chat, reader.at_ms, excluded),
            )
            for row in rows:
                contact = evidence.terminal(row["sender_contact_id"])
                if evidence.field(contact, "status") != "confirmed":
                    continue
                value, first, last = row["sender_identifier"], row["first_ms"], row["last_ms"]
                binding_key = (value, contact)
                if binding_key not in bindings:
                    bindings[binding_key] = (
                        tuple(sorted(self._author_boundaries(value, contact, index, evidence))),
                        {},
                    )
                    if len(bindings) > 256:
                        bindings.popitem(last=False)
                bindings.move_to_end(binding_key)
                all_boundaries, proofs = bindings[binding_key]

                def prove(at_ms):
                    bucket = bisect_right(all_boundaries, at_ms)
                    if bucket not in proofs:
                        proofs[bucket] = frozenset(
                            self._author_at(value, contact, at_ms, reader, index, evidence)
                        )
                    return proofs[bucket]

                latest = prove(last)
                result.update(latest)
                boundaries = [b for b in all_boundaries if first < b <= last]
                if not boundaries:
                    continue
                earliest = prove(first)
                segments = [
                    (b, end, prove(b))
                    for b, end in zip(boundaries, [*boundaries[1:], last + 1], strict=True)
                ]
                if earliest == latest and all(found == latest for _, _, found in segments):
                    continue
                result.update(earliest)
                # A third holder can lie between MIN/MAX. Require an actual message in that
                # ownership interval; this remains one aggregate per transition, never per row.
                covered = latest | earliest
                for start, end, found in segments:
                    if not found - covered:
                        continue
                    actual = queries._rows(
                        "SELECT MAX(sent_ms) AS at_ms FROM messages_current WHERE "
                        + filters
                        + " AND sender_identifier IS ? AND sender_contact_id IS ? AND sent_ms>=? AND sent_ms<?",
                        (chat, reader.at_ms, excluded, value, row["sender_contact_id"], start, end),
                    )[0]["at_ms"]
                    if actual is not None:
                        proven = prove(actual)
                        result.update(proven)
                        covered |= proven
            return frozenset(result)

        return self._memo(
            ("authors", snapshot, snapshot.generation, chat, reader.at_ms, known), load
        )

    def _groups(self, reader):
        from yeoman_gateway.history.queries import HistoryQueries

        snapshot = reader.history
        snapshot.assert_current(snapshot.generation)
        return self._memo(
            ("groups", snapshot, snapshot.generation),
            lambda: tuple(
                (r["chat_id"], r["subject"] or "Unnamed group")
                for r in HistoryQueries(snapshot).chats()
                if r["chat_id"].endswith("@g.us")
            ),
        )

    def _shared_chats(self, reader, index):
        key = self._reader_key(reader)
        if reader.channel != "whatsapp" or reader.history is None:
            return ()

        def load():
            person = self._endpoint(
                reader.chat_id if reader.kind == "other_dm" else reader.principal_id, reader, index
            )
            if len(person) != 1:
                return ()
            shared = []
            for chat, _ in self._groups(reader):
                members, assistant = self._roster(chat, reader, index)
                if person <= members and (reader.kind != "other_dm" or assistant):
                    shared.append(chat)
            return tuple(shared)

        return self._memo(("shared", key), load)

    def _shared_chat_names(self, terminal, reader, index):
        key = self._reader_key(reader)
        if reader.channel != "whatsapp" or reader.history is None:
            return ()

        def load():
            shared = set(self._shared_chats(reader, index))
            labels = set()
            for chat, label in self._groups(reader):
                if chat in shared:
                    members, _ = self._roster(chat, reader, index)
                    if terminal in members or terminal in self._authors(chat, reader, index):
                        labels.add(label)
            return tuple(sorted(labels, key=lambda label: (label.casefold(), label)))

        return self._memo(("labels", key, terminal), load)

    def _endpoint(
        self, value: str, reader: ReaderContext, index: _Index, *, at_ms: int | None = None
    ) -> set[int]:
        value = value.removeprefix(reader.channel + ":")
        if reader.channel == "telegram":
            matches = (
                index.matches("telegram_id", value, reader.at_ms if at_ms is None else at_ms)
                if re.fullmatch(r"[0-9]+", value)
                else set()
            )
            return matches if len(matches) == 1 else set()
        if reader.channel != "whatsapp":
            return set()
        _, interpretations = _identifiers(value, reader)
        matches = set()
        for kind, key in interpretations:
            if kind != "person":
                matches.update(index.matches(kind, key, reader.at_ms if at_ms is None else at_ms))
        return matches if len(matches) == 1 else set()


def _identifiers(text: str, reader: ReaderContext) -> tuple[bool, tuple[tuple[str, str], ...]]:
    text = text.strip()
    low = text.casefold()
    if low.startswith("person:"):
        key = low[7:]
        return True, (("person", key),) if key.isascii() and key.isdigit() else ()
    for prefix, kind in (
        ("wa_lid:", "wa_lid"),
        ("lid:", "wa_lid"),
        ("telegram:", "telegram_id"),
        ("telegram_id:", "telegram_id"),
        ("telegram_username:", "telegram_username"),
    ):
        if low.startswith(prefix):
            key = low[len(prefix) :]
            valid = (
                bool(re.fullmatch(r"[0-9]+", key))
                if kind != "telegram_username"
                else bool(re.fullmatch(r"[a-z0-9_]{5,32}", key))
            )
            if kind.startswith("telegram") and reader.channel != "telegram":
                valid = False
            return True, ((kind, key),) if valid else ()
    if "@" in text and not re.fullmatch(r"@\w+", text):
        value = text.removeprefix("@")
        for kind in ("pn_jid", "lid"):
            ident = identifier_from_history(kind, value)
            if ident:
                return True, ((ident.kind, ident.value),)
        return True, ()
    explicit_phone = low.startswith("phone:")
    value = text[6:] if explicit_phone else text.removeprefix("@")
    numeric = bool(re.fullmatch(r"[+0-9\s/().-]+", value)) and bool(re.search(r"[0-9]", value))
    if explicit_phone or numeric:
        compact = re.sub(r"[\s/().-]", "", value)
        code = reader.national_calling_code
        if (
            compact.startswith("0")
            and not compact.startswith("00")
            and code
            and re.fullmatch(r"[1-9][0-9]{0,2}", code)
        ):
            value = code + compact[1:]
        phone = normalize_phone(value)
        result = [("phone", phone)] if phone else []
        if not explicit_phone and re.fullmatch(r"[0-9]+", compact) and not compact.startswith("0"):
            if reader.channel == "whatsapp":
                result.append(("wa_lid", compact))
            if reader.channel == "telegram":
                result.append(("telegram_id", compact))
        return True, tuple(result)
    if text.startswith("@"):
        if reader.channel == "telegram" and re.fullmatch(r"@[a-z0-9_]{5,32}", low):
            return True, (("telegram_username", low[1:]),)
        return True, ()
    return bool(":" in text or re.search(r"[0-9]{7,}", text)), ()


def _resolution(matches: set[int], index: _Index) -> Resolution:
    if len(matches) == 1:
        return Resolution("resolved", next(iter(matches)))
    if not matches:
        return Resolution("not_found")
    ordered = sorted(matches, key=lambda p: (index.display_name(p).casefold(), p))
    return Resolution("ambiguous", candidates=tuple(Candidate(p) for p in ordered))


def resolve_person(text_or_id: str, reader: ReaderContext) -> Resolution:
    directory = reader.directory
    index = directory._index()
    scope = directory._scope(reader, index)
    return _resolve(text_or_id, reader, index, scope)


def _resolve(
    text_or_id: str, reader: ReaderContext, index: _Index, scope: ReaderScope
) -> Resolution:
    exact_eligible = index.terminal_ids if reader.kind == "owner_dm" else scope.eligible_ids
    shaped, interpretations = _identifiers(text_or_id, reader)
    if shaped:
        matches = set()
        for kind, value in interpretations:
            if kind == "person":
                terminal = index.terminal(int(value))
                if terminal is not None:
                    matches.add(terminal)
            else:
                matches.update(index.matches(kind, value, reader.at_ms))
        return _resolution(matches & exact_eligible, index)
    tokens = name_tokens(text_or_id)
    if not tokens:
        return Resolution("not_found")
    for tier in index.exact_names:
        matches = tier.get(tokens, frozenset()) & exact_eligible
        if matches:
            return _resolution(matches, index)
    if not all(word.isalpha() and len(word) >= 3 for word in tokens):
        return Resolution("not_found")
    eligible = scope.eligible_ids & index.contact_ids
    for prefix in (False, True):
        choices = None
        for word in tokens:
            positions = index.token_aliases.get(word, frozenset())
            if prefix:
                positions = set(positions)
                offset = bisect_left(index.words, word)
                while offset < len(index.words) and index.words[offset].startswith(word):
                    positions.update(index.token_aliases[index.words[offset]])
                    offset += 1
            choices = positions if choices is None else choices & positions
            if not choices:
                break
        matches = {index.aliases[position][0] for position in choices or ()} & eligible
        if matches:
            return _resolution(matches, index)
    return Resolution("not_found")


def resolve_people_in_text(content: str, reader: ReaderContext) -> tuple[tuple[str, int], ...]:
    index = reader.directory._index()
    scope = reader.directory._scope(reader, index)
    width = index.alias_width
    words = list(re.finditer(r"[^\W_]+", content, re.UNICODE))
    found, seen, offset = [], set(), 0
    while offset < len(words):
        for length in range(min(width, len(words) - offset), 0, -1):
            span = content[words[offset].start() : words[offset + length - 1].end()]
            result = _resolve(span, reader, index, scope)
            if result.status == "ambiguous":
                return ()
            if result.status == "resolved":
                if result.person_id not in seen:
                    found.append((span, result.person_id))
                    seen.add(result.person_id)
                offset += length
                break
        else:
            offset += 1
    return tuple(found)
