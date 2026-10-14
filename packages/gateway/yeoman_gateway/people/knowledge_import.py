"""One-time import of the owner's curated CRM contacts from Knowledge into the directory."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from yeoman_gateway.knowledge.api import DirectorySeedContact

from .identifiers import ObservedId, identifier_from_history, is_valid_value, normalize_phone
from .store import PeopleStore

_OWNER_ALIAS_SOURCES = frozenset({"manual", "owner_confirmed"})
_DIGITS = re.compile(r"^[0-9]+$")


@dataclass(slots=True)
class ImportReport:
    contacts: int = 0
    mapped_by_history_id: int = 0
    mapped_by_identifier: int = 0
    created: int = 0
    owner_nicknames: int = 0
    observed_names: int = 0
    preferred_names_set: int = 0
    notes_set: int = 0
    telegram_ids: int = 0
    conflicts: int = 0        # identifiers pointing to two different directory persons -> merge suggestion

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


def seed_identifiers(kind: str, value: str) -> list[ObservedId]:
    """Map a Knowledge contact_identifiers row (lid / phone_jid / telegram_id) to directory ids."""
    value = value.strip()
    if kind == "lid":
        ident = identifier_from_history("lid", value if "@" in value else value + "@lid")
        return [ident] if ident else []
    if kind == "phone_jid":
        if "@" in value:
            ident = identifier_from_history("pn_jid", value)
            return [ident] if ident else []
        phone = normalize_phone(value if value.startswith(("+", "00")) else "+" + value)
        return [ObservedId("phone", phone)] if phone else []
    if kind == "telegram_id":
        # The Telegram adapter stores "<user id>|<username>" when the user has a username.
        digits, _, username = value.removeprefix("telegram:").partition("|")
        if not _DIGITS.match(digits):
            return []
        result = [ObservedId("telegram_id", digits)]
        if is_valid_value("telegram_username", username):
            result.append(ObservedId("telegram_username", username))
        return result
    return []


def _map(store: PeopleStore, contact: DirectorySeedContact, idents: list[ObservedId], now_ms: int,
         report: ImportReport) -> int:
    holder = store.find_active("history_contact", contact.knowledge_id)
    if holder is not None:
        report.mapped_by_history_id += 1
        return store.terminal(holder)
    terminals: list[int] = []
    for ident in [ObservedId("knowledge_person", contact.knowledge_id), *idents]:
        holder = store.find_active(ident.kind, ident.value)
        if holder is not None and (term := store.terminal(holder)) not in terminals:
            terminals.append(term)
    if not terminals:
        report.created += 1
        return store.create_person(level="seen", now_ms=now_ms)
    report.mapped_by_identifier += 1
    for other in terminals[1:]:
        report.conflicts += 1
        store.suggest("merge", terminals[0], other, evidence={"source": "knowledge_seed"})
    return terminals[0]


def _add_owner_nickname(store: PeopleStore, person: int, name: str) -> bool:
    name = name.strip()
    if not any(ch.isalnum() for ch in name):
        return False
    group = sorted(store.members(store.terminal(person)))
    marks = ",".join("?" * len(group))
    if store.conn.execute(
            f"SELECT 1 FROM person_names WHERE person_id IN ({marks}) AND name = ? "
            "AND (source = 'owner' OR hidden = 1)", (*group, name)).fetchone():
        return False
    store.conn.execute("INSERT INTO person_names(person_id, name, kind, source) "
                       "VALUES (?, ?, 'nickname', 'owner')", (person, name))
    return True


def import_knowledge_seed(store: PeopleStore, seed: list[DirectorySeedContact],
                          now_ms: int) -> ImportReport:
    """Apply the import; the caller owns the transaction (``store.transaction("import")``)."""
    report = ImportReport()
    for contact in seed:
        report.contacts += 1
        idents = [i for _channel, kind, value in contact.identifiers
                  for i in seed_identifiers(kind, value)]
        person = _map(store, contact, idents, now_ms, report)
        for ident in [ObservedId("knowledge_person", contact.knowledge_id), *idents]:
            outcome = store.attach_id(person, ident, now_ms)
            if outcome == "added" and ident.kind == "telegram_id":
                report.telegram_ids += 1
            elif outcome == "taken":
                # Another directory person already holds this id: likely one person split in two.
                holder = store.terminal(store.find_active(ident.kind, ident.value))
                report.conflicts += store.suggest("merge", store.terminal(person), holder,
                                                  evidence={"source": "knowledge_seed", "kind": ident.kind})
        for alias, source, _status in contact.aliases:
            if source in _OWNER_ALIAS_SOURCES:
                report.owner_nicknames += _add_owner_nickname(store, person, alias)
            elif store.add_observed_name(person, alias, "contact_name", now_ms,
                                         evidence={"source": "knowledge_seed"}) == "added":
                report.observed_names += 1
        # The only owner columns written outside the owner's own edits: this data was
        # curated by the owner in the old CRM, and it never overwrites a value.
        row = store.person_rows(person)
        preferred = (contact.preferred_name or "").strip()
        if preferred and row["preferred_name"] is None:
            store.conn.execute("UPDATE persons SET preferred_name = ? WHERE id = ?", (preferred, person))
            report.preferred_names_set += 1
        notes = row["notes"] or ""
        additions = [v.strip() for v in contact.profile_values if v.strip() and v.strip() not in notes]
        additions = list(dict.fromkeys(additions))
        if additions:
            merged = "\n".join([notes, *additions]) if notes else "\n".join(additions)
            store.conn.execute("UPDATE persons SET notes = ? WHERE id = ?", (merged, person))
            report.notes_set += 1
    return report
