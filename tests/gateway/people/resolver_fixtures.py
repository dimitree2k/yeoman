"""Explicit synthetic directory and native-shaped history evidence, never runtime paths."""

import json
import sqlite3
from dataclasses import replace

from yeoman_gateway.history.live import HistoryBoundary
from yeoman_gateway.history.reader import HistoryReader
from yeoman_gateway.history.schema import create
from yeoman_gateway.people.schema import open_people_db


class DirectoryCase:
    def __init__(self, root, *, identities=True):
        from yeoman_gateway.people.resolver import PeopleResolver

        self.path = root / "people.db"
        self.writer = open_people_db(self.path)
        self.directory = PeopleResolver(self.path)
        self.hdb = sqlite3.connect(root / "history.db", isolation_level=None)
        self.hdb.execute("PRAGMA journal_mode=WAL")
        create(self.hdb)
        self.hdb.execute(
            "INSERT INTO projector_state VALUES ('@runtime',0,0,'',4,?)", ('{"generation":1}',)
        )
        self.history_reader = HistoryReader(root / "history.db")
        self.sequence = 0
        if not identities:
            return
        self.owner = self.add_person("Synthetic Owner", ids=(("phone", "4915000000099"),))
        self.writer.execute("UPDATE persons SET is_owner=1 WHERE id=?", (self.owner,))
        self.counterpart = self.add_person(
            "Synthetic Counterpart", ids=(("phone", "4915000000098"),)
        )
        self.assistant = self.add_person("Synthetic Assistant", ids=(("phone", "4915000000097"),))
        self.hdb.execute("UPDATE contacts SET role='owner' WHERE contact_id=?", (str(self.owner),))
        self.hdb.execute(
            "UPDATE contacts SET role='assistant' WHERE contact_id=?", (str(self.assistant),)
        )

    def add_person(self, name, *, source="owner", level="contact", ids=(), merged_into=None):
        person = self.writer.execute(
            "INSERT INTO persons(preferred_name,level,merged_into) VALUES (?,?,?)",
            (name if source == "owner" else None, level, merged_into),
        ).lastrowid
        if source != "owner":
            self.add_name(person, name, source=source)
        self.hdb.execute(
            "INSERT INTO contacts VALUES (?,'person',NULL,?,'confirmed',NULL,'[]')",
            (str(person), name),
        )
        for kind, value in ids:
            self.add_id(person, kind, value)
        return person

    def add_name(self, person, name, *, source="observed", hidden=False):
        self.writer.execute(
            "INSERT INTO person_names(person_id,name,source,hidden) VALUES (?,?,?,?)",
            (person, name, source, hidden),
        )

    def add_id(self, person, kind, value, *, start=None, end=None, hidden=False):
        self.writer.execute(
            "INSERT INTO person_ids(person_id,kind,value,valid_from_ms,valid_until_ms,hidden,source) VALUES (?,?,?,?,?,?,'observed')",
            (person, kind, value, start, end, hidden),
        )
        if kind in ("phone", "wa_lid"):
            hkind = "pn_jid" if kind == "phone" else "lid"
            jid = value + ("@s.whatsapp.net" if kind == "phone" else "@lid")
            self.hdb.execute(
                "INSERT INTO identifier_history(contact_id,channel,kind,value,strength,evidence,valid_from_ms,valid_until_ms,source_refs) VALUES (?,'whatsapp',?,?,'strong','observed',?,?,'[]')",
                (str(person), hkind, jid, start, end),
            )

    def endpoint(self, person):
        row = self.writer.execute(
            "SELECT kind,value FROM person_ids WHERE person_id=? AND kind IN ('phone','wa_lid') AND hidden=0 ORDER BY id LIMIT 1",
            (person,),
        ).fetchone()
        if row is None:
            self.add_id(person, "wa_lid", str(300000000000 + person))
            return self.endpoint(person)
        return row["value"] + ("@s.whatsapp.net" if row["kind"] == "phone" else "@lid")

    def set_roster(self, chat, persons, *, at_ms=1, label=None):
        self.sequence += 1
        payload = {"complete": True, "participants": [[self.endpoint(p)] for p in persons]}
        self.hdb.execute(
            "INSERT INTO message_events(event_id,kind,channel,chat_id,actor_basis,occurred_ms,time_certainty,payload_json,provenance,source_refs) VALUES (?,'member_snapshot','whatsapp',?,'unknown',?,'native',?,'native','[]')",
            (str(self.sequence), chat, at_ms, json.dumps(payload)),
        )
        if label:
            self.hdb.execute(
                "INSERT INTO message_events(event_id,kind,channel,chat_id,actor_basis,occurred_ms,time_certainty,payload_json,provenance,source_refs) VALUES (?,'group_subject','whatsapp',?,'unknown',?,'native',?,'native','[]')",
                ("label" + str(self.sequence), chat, at_ms, json.dumps({"value": label})),
            )

    def add_author(self, chat, person, *, at_ms, basis="native_identifier"):
        self.sequence += 1
        self.hdb.execute(
            "INSERT INTO messages(message_id,channel,chat_id,sender_contact_id,sender_identifier,sender_basis,direction,sent_ms,time_certainty,text,provenance,source_refs) VALUES (?,'whatsapp',?,?,?,?, 'in',?,'native','note','native','[]')",
            (str(self.sequence), chat, str(person), self.endpoint(person), basis, at_ms),
        )

    @property
    def snapshot(self):
        return self.history_reader.open_snapshot(HistoryBoundary(1, ()))

    def reader(self, kind="owner_dm", at_ms=300, national_calling_code=None):
        from yeoman_gateway.people.resolver import ReaderContext

        principal = self.owner if kind == "owner_dm" else self.counterpart
        chat = "g@g.us" if kind == "group" else self.endpoint(principal)
        return ReaderContext(
            self.directory,
            "whatsapp",
            chat,
            "whatsapp:" + self.endpoint(principal),
            at_ms,
            kind,
            self.snapshot,
            national_calling_code,
        )

    def close(self):
        self.directory.close()
        self.writer.close()
        self.history_reader.close()
        self.hdb.close()

    def changed_reader(self, **kwargs):
        return replace(self.reader(), **kwargs)

    def add_message(
        self,
        message_id,
        *,
        sender_person,
        identifier,
        at_ms,
        basis="native_identifier",
        text="note",
        chat="g@g.us",
        mentions=(),
        time_basis=None,
    ):
        from yeoman_gateway.history.schema import FTS_ROWS

        self.hdb.execute(
            "INSERT INTO messages(message_id,channel,chat_id,native_message_id,sender_contact_id,sender_identifier,sender_basis,direction,sent_ms,time_certainty,text,mentions_json,provenance,source_refs) VALUES (?,'whatsapp',?,?,?,?,?,'in',?,?,?,?,'native','[]')",
            (
                message_id,
                chat,
                message_id,
                str(sender_person) if sender_person is not None else None,
                identifier,
                basis,
                at_ms,
                time_basis or ("native" if at_ms is not None else "unknown"),
                text,
                json.dumps(mentions),
            ),
        )
        self.hdb.execute("DELETE FROM messages_fts")
        self.hdb.execute("INSERT INTO messages_fts(message_id,chat_id,text) " + FTS_ROWS)


def synced_directory(snapshot, root):
    """Seed P1 from synthetic projected history; caller owns the returned directory."""
    from yeoman_gateway.people.history_source import read_observed_persons
    from yeoman_gateway.people.resolver import PeopleResolver
    from yeoman_gateway.people.store import PeopleStore
    from yeoman_gateway.people.sync import sync_from_history

    path = root / "people.db"
    writer = open_people_db(path)
    store = PeopleStore(writer)
    with store.transaction("sync"):
        observed = read_observed_persons(snapshot.connection)
        sync_from_history(store, observed, 1700000000100)
    # Test owner contacts are explicit contacts even when observed only as former authors.
    writer.execute("UPDATE persons SET level='contact'")
    for person in observed:
        row = writer.execute(
            "SELECT person_id FROM person_ids WHERE kind='history_contact' AND value=?",
            (person.history_terminal,),
        ).fetchone()
        if row is None:
            continue
        display = snapshot.connection.execute(
            "SELECT display_name FROM contacts WHERE contact_id=?", (person.history_terminal,)
        ).fetchone()[0]
        writer.execute("UPDATE persons SET preferred_name=? WHERE id=?", (display, row[0]))
        for name, kind in person.names:
            writer.execute(
                "INSERT OR IGNORE INTO person_names(person_id,name,kind,source) VALUES (?,?,?,'owner')",
                (row[0], name, kind),
            )
    writer.close()
    return PeopleResolver(path)


def gold_directory(root):
    """Build exactly the plan's invented gold graph; fixed ids are part of this fixture."""
    from pathlib import Path
    from types import SimpleNamespace

    c = DirectoryCase(root, identities=False)
    labels = (
        "Vincent", "New Holder", "Alex Müller", "Alex Weber", "Al", "Quiet Person",
        "Secret Person", "Merged Alias", "Sam Twins", "Sam Twins", "Telegram Person",
        "Synthetic Owner", "Past Author", "Synthetic Assistant",
    )
    for person, label in enumerate(labels, 1):
        actual = c.add_person(
            label, source="observed" if person == 6 else "owner",
            level="seen" if person == 6 else "contact", merged_into=1 if person == 8 else None,
        )
        assert actual == person
    c.owner, c.counterpart, c.assistant = 12, 2, 14
    c.writer.execute("UPDATE persons SET is_owner=1 WHERE id=12")
    c.writer.execute("UPDATE persons SET notes='PRIVATE_gold_note' WHERE id=1")
    c.hdb.execute("UPDATE contacts SET role='owner' WHERE contact_id='12'")
    c.hdb.execute("UPDATE contacts SET role='assistant' WHERE contact_id='14'")
    c.add_id(1, "phone", "4915000000012")
    c.add_id(1, "phone", "4915000000011", end=200)
    c.add_id(1, "wa_lid", "300000000001")
    c.add_id(2, "phone", "4915000000011", start=200)
    c.add_id(11, "telegram_id", "1234567")
    c.add_id(11, "telegram_username", "synthetic_user")
    c.add_id(12, "phone", "4915000000099")
    c.add_id(14, "phone", "4915000000097")
    for person in (3, 4, 5, 6, 7, 9, 10, 13):
        c.add_id(person, "wa_lid", str(300000000000 + person))
    c.add_name(1, "Wimds", source="owner")
    c.add_name(1, "Vinz😈")
    c.add_name(2, "Vincent")
    c.add_name(1, "Hidden Alias", source="owner", hidden=True)
    c.writer.execute(
        "INSERT INTO person_decisions(person_a,person_b,decision) VALUES (9,10,'not_same')"
    )
    c.set_roster("g1@g.us", [1, 2, 3, 4, 5, 6, 9, 10, 12, 14], label="Synthetic G1")
    c.set_roster("g2@g.us", [1, 2, 12, 14], label="Synthetic G2")
    c.set_roster("private@g.us", [7, 12, 14], label="PRIVATE_group")
    c.add_author("g1@g.us", 13, at_ms=100)
    gold = root / "gold.json"
    gold.write_bytes(Path(__file__).with_name("resolver_gold.json").read_bytes())
    return SimpleNamespace(people=c.path, history=root / "history.db", gold=gold, case=c)
