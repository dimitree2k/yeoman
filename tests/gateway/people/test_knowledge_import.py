from __future__ import annotations

import sqlite3

import pytest
from yeoman_gateway.knowledge.api import DirectorySeedContact, export_directory_seed
from yeoman_gateway.people.history_source import ObservedPerson
from yeoman_gateway.people.identifiers import ObservedId
from yeoman_gateway.people.knowledge_import import import_knowledge_seed, seed_identifiers
from yeoman_gateway.people.schema import open_people_db
from yeoman_gateway.people.store import PeopleStore
from yeoman_gateway.people.sync import sync_from_history

NOW = 1_791_000_000_000


@pytest.fixture
def store(tmp_path):
    conn = open_people_db(tmp_path / "people.db")
    yield PeopleStore(conn)
    conn.close()


def first_sync(store, observed):
    with store.transaction("sync"):
        return sync_from_history(store, observed, NOW)


def op(terminal, *, ids=(), names=()):
    return ObservedPerson(terminal, (terminal,), tuple(ids), tuple((n, "push_name") for n in names),
                          False, "contact", None)


def seed_contact(kid, *, identifiers=(), aliases=(), preferred=None, profile=()):
    return DirectorySeedContact(kid, None, preferred, tuple(identifiers), tuple(aliases), tuple(profile))


def run_import(store, seed):
    with store.transaction("import"):
        return import_knowledge_seed(store, seed, NOW)


def make_knowledge_fixture(tmp_path):
    from yeoman_gateway.knowledge._store import KnowledgeStore

    path = tmp_path / "knowledge.db"
    KnowledgeStore(path).close()
    conn = sqlite3.connect(path)
    with conn:
        for kid, name, preferred in (("k1", "Vinz", "Vincent"), ("k2", "Frank", None)):
            conn.execute("INSERT INTO contacts(id, display_name, created_at, updated_at, preferred_name) "
                         "VALUES (?, ?, 'x', 'x', ?)", (kid, name, preferred))
        conn.execute("INSERT INTO contact_identifiers VALUES ('whatsapp', '111@lid', 'k1', 'lid')")
        conn.execute("INSERT INTO contact_identifiers VALUES ('telegram', '42', 'k2', 'telegram_id')")
        for kid, alias, source, status in (("k1", "Wimds", "owner_confirmed", "confirmed"),
                                           ("k1", "Old", "observed", "retired"),
                                           ("k2", "Franky", "observed", "observed")):
            conn.execute("INSERT INTO contact_aliases(contact_id, alias, source, first_seen, last_seen, status) "
                         "VALUES (?, ?, ?, 'x', 'x', ?)", (kid, alias, source, status))
        conn.execute("INSERT INTO contact_fields(contact_id, kind, value, created_at, updated_at) "
                     "VALUES ('k1', 'city', 'Hamburg', 'x', 'x')")
    conn.close()
    return path


def test_export_reads_fixture_knowledge(tmp_path):
    db = make_knowledge_fixture(tmp_path)
    before = db.stat().st_mtime_ns
    seed = export_directory_seed(db)
    assert {c.knowledge_id for c in seed} == {"k1", "k2"}
    assert all(a[2] != "retired" for c in seed for a in c.aliases)
    k1 = next(c for c in seed if c.knowledge_id == "k1")
    assert k1.identifiers == (("whatsapp", "lid", "111@lid"),)
    assert k1.preferred_name == "Vincent" and k1.profile_values == ("Hamburg",)
    assert db.stat().st_mtime_ns == before


def test_seed_identifier_forms():
    assert seed_identifiers("lid", "111@lid") == [ObservedId("wa_lid", "111")]
    assert seed_identifiers("lid", "111") == [ObservedId("wa_lid", "111")]
    assert seed_identifiers("phone_jid", "4915200000001@s.whatsapp.net")[0].value == "4915200000001"
    assert seed_identifiers("phone_jid", "4915200000001")[0].value == "4915200000001"
    assert seed_identifiers("telegram_id", "telegram:42") == [ObservedId("telegram_id", "42")]
    assert seed_identifiers("telegram_id", "42|franky_t") == [
        ObservedId("telegram_id", "42"), ObservedId("telegram_username", "franky_t")]
    assert seed_identifiers("telegram_id", "42|x") == [ObservedId("telegram_id", "42")]
    assert seed_identifiers("telegram_id", "abc") == []
    assert seed_identifiers("telegram_id", "abc|franky_t") == []
    assert seed_identifiers("other", "x") == []


def test_import_maps_by_history_id_and_marks_owner_aliases(store):
    first_sync(store, [op("k1", ids=[ObservedId("wa_lid", "111")], names=["Vinz"])])   # history id == knowledge id
    seed = [seed_contact("k1", aliases=[("Wimds", "owner_confirmed", "observed"), ("V.", "observed", "observed")],
                         preferred="Vincent", profile=["likes climbing"])]
    r = run_import(store, seed)
    assert r.mapped_by_history_id == 1 and r.owner_nicknames == 1 and r.observed_names == 1
    assert store.person_rows(1)["preferred_name"] == "Vincent"
    assert "likes climbing" in store.person_rows(1)["notes"]
    kinds = dict(store.conn.execute("SELECT name, kind || '/' || source FROM person_names"))
    assert kinds == {"Vinz": "push_name/observed", "Wimds": "nickname/owner", "V.": "contact_name/observed"}
    assert store.conn.execute("SELECT DISTINCT writer FROM change_log WHERE writer <> 'sync'").fetchall()[0][0] == "import"


def test_import_does_not_overwrite_owner_preferred_name(store):
    first_sync(store, [op("k1")])
    store.conn.execute("UPDATE persons SET preferred_name = 'Vince', notes = 'own note' WHERE id = 1")
    r = run_import(store, [seed_contact("k1", preferred="Vincent", profile=["own note", "new"])])
    assert r.preferred_names_set == 0
    row = store.person_rows(1)
    assert row["preferred_name"] == "Vince" and row["notes"] == "own note\nnew"


def test_import_is_idempotent(store):
    first_sync(store, [op("k1", ids=[ObservedId("wa_lid", "111")])])
    seed = [seed_contact("k1", aliases=[("Wimds", "manual", "confirmed"), ("V.", "push_name", "observed")],
                         preferred="Vincent", profile=["a", "b"]),
            seed_contact("k9", identifiers=[("telegram", "telegram_id", "42")])]
    run_import(store, seed)
    before = store.conn.execute("SELECT count(*) FROM change_log").fetchone()[0]
    r = run_import(store, seed)
    assert store.conn.execute("SELECT count(*) FROM change_log").fetchone()[0] == before
    assert r.created == 0 and r.owner_nicknames == 0 and r.notes_set == 0


def test_import_creates_unmapped_contact_with_telegram_id(store):
    r = run_import(store, [seed_contact("k2", identifiers=[("telegram", "telegram_id", "42|franky_t")],
                                        aliases=[("Franky", "observed", "observed")])])
    assert r.created == 1 and r.telegram_ids == 1
    person = store.find_active("telegram_id", "42")
    assert store.find_active("telegram_username", "franky_t") == person
    assert store.find_active("knowledge_person", "k2") == person
    assert store.person_rows(person)["level"] == "seen"


def test_import_maps_by_identifier(store):
    first_sync(store, [op("h1", ids=[ObservedId("wa_lid", "111")])])
    r = run_import(store, [seed_contact("k1", identifiers=[("whatsapp", "lid", "111@lid")])])
    assert r.mapped_by_identifier == 1 and r.created == 0
    assert store.find_active("knowledge_person", "k1") == 1


def test_import_conflicting_identifiers_suggest_merge(store):
    first_sync(store, [op("h1", ids=[ObservedId("wa_lid", "111")]), op("h2", ids=[ObservedId("phone", "4915200000001")])])
    r = run_import(store, [seed_contact("k1", identifiers=[("whatsapp", "lid", "111@lid"),
                                                           ("whatsapp", "phone_jid", "4915200000001@s.whatsapp.net")])])
    assert r.conflicts == 1
    assert store.conn.execute("SELECT person_a, person_b, kind FROM suggestions").fetchall()[0][:] == (1, 2, "merge")
    assert store.terminal(2) == 2                                         # never merged automatically
