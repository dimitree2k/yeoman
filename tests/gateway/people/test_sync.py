from __future__ import annotations

import time

import pytest
from yeoman_gateway.people.history_source import ObservedPerson
from yeoman_gateway.people.identifiers import ObservedId
from yeoman_gateway.people.schema import open_people_db
from yeoman_gateway.people.store import PeopleStore
from yeoman_gateway.people.sync import sync_from_history

NOW = 1_791_000_000_000


@pytest.fixture
def store(tmp_path):
    conn = open_people_db(tmp_path / "people.db")
    yield PeopleStore(conn)
    conn.close()


def pn(value, **times):
    return ObservedId("phone", value, **times)


def lid(value, **times):
    return ObservedId("wa_lid", value, **times)


def op(terminal, *, members=None, ids=(), names=(), level="seen", is_owner=False, last_seen_ms=None):
    return ObservedPerson(terminal, tuple(members or [terminal]), tuple(ids),
                          tuple((n, "push_name") for n in names), is_owner, level, last_seen_ms)


def first_sync(store, observed, now_ms=NOW):
    with store.transaction("sync"):
        return sync_from_history(store, observed, now_ms)


def count(store, table):
    return store.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def names(store, person, *, source):
    return [r[0] for r in store.conn.execute(
        "SELECT name FROM person_names WHERE person_id = ? AND source = ? ORDER BY id", (person, source))]


def visible_names(store, person):
    return [r[0] for r in store.conn.execute(
        "SELECT name FROM person_names WHERE person_id = ? AND hidden = 0", (person,))]


def active(store, kind, value):
    return store.find_active(kind, value) is not None


def person_of(store, kind, value):
    return store.terminal(store.find_active(kind, value))


def ended_rows(store, person, value):
    return store.conn.execute(
        "SELECT count(*) FROM person_ids WHERE person_id = ? AND value = ? AND valid_until_ms IS NOT NULL",
        (person, value)).fetchone()[0]


def test_first_pass_creates_persons_and_second_pass_is_a_noop(store):
    obs = [op("h1", ids=[pn("4915200000001"), lid("111")], names=["Vinz"]), op("h2", ids=[lid("222")])]
    r1 = first_sync(store, obs)
    assert r1.persons_created == 2 and r1.ids_added == 5 and r1.names_added == 1
    before = count(store, "change_log")
    r2 = first_sync(store, obs, NOW + 300_000)
    assert count(store, "change_log") == before and r2.persons_created == 0
    assert set(r2.as_dict().values()) == {0}


def test_owner_columns_and_rows_untouched(store):
    first_sync(store, [op("h1", ids=[lid("111")], names=["Vinz"])])
    store.conn.execute("UPDATE persons SET preferred_name='Vincent', notes='n' WHERE id=1")
    store.conn.execute("INSERT INTO person_names(person_id, name) VALUES (1, 'Wimds')")
    store.conn.execute("INSERT INTO person_ids(person_id, kind, value) VALUES (1, 'telegram_id', '42')")
    first_sync(store, [op("h1", ids=[lid("111")], names=["Vinz", "Vinz 2"], level="contact")])
    row = store.person_rows(1)
    assert (row["preferred_name"], row["notes"], row["level"]) == ("Vincent", "n", "contact")
    assert names(store, 1, source="owner") == ["Wimds"]
    assert names(store, 1, source="observed") == ["Vinz", "Vinz 2"]
    writers = {r[0] for r in store.conn.execute(
        "SELECT writer FROM change_log WHERE tbl='person_ids' AND op='update'")}
    assert writers <= {"manual"}


def test_hidden_rows_never_resurrected(store):
    first_sync(store, [op("h1", ids=[lid("111"), pn("4915200000001")], names=["AlexGrrrrr😈"])])
    store.conn.execute("UPDATE person_names SET hidden=1 WHERE name='AlexGrrrrr😈'")
    store.conn.execute("UPDATE person_ids SET hidden=1 WHERE value='4915200000001'")
    for _ in range(2):
        r = first_sync(store, [op("h1", ids=[lid("111"), pn("4915200000001")], names=["AlexGrrrrr😈"])])
        assert r.ids_skipped_hidden == 1 and r.names_skipped_hidden == 1
    # a history rebuild renamed the terminal contact id: the person is found via lid 111, still hidden
    r = first_sync(store, [op("h1-rebuilt", ids=[lid("111"), pn("4915200000001")], names=["AlexGrrrrr😈"])])
    assert r.persons_created == 0
    assert visible_names(store, 1) == [] and not active(store, "phone", "4915200000001")


def test_history_merge_merges_directory_persons(store):
    first_sync(store, [op("h1", ids=[lid("111")]), op("h2", ids=[pn("4915200000002")])])
    r = first_sync(store, [op("h1", members=["h1", "h2"], ids=[lid("111"), pn("4915200000002")])])
    assert r.persons_merged == 1
    assert store.terminal(2) == 1 and store.person_rows(2)["merged_by"] == "sync"


def test_history_unmerge_reverts_sync_merge_only(store):
    first_sync(store, [op("h1", ids=[lid("111")]), op("h2", ids=[lid("222")]), op("h3", ids=[lid("333")])])
    first_sync(store, [op("h1", members=["h1", "h2", "h3"], ids=[lid("111"), lid("222"), lid("333")])])
    store.conn.execute("UPDATE persons SET merged_by='owner' WHERE id=3")          # owner confirmed 3 = 1
    r = first_sync(store, [op("h1", ids=[lid("111")]), op("h2", ids=[lid("222")]), op("h3", ids=[lid("333")])])
    assert store.terminal(2) == 2          # sync merge reverted
    assert store.terminal(3) == 1          # owner merge kept
    assert r.persons_unmerged == 1
    assert count(store, "persons") == 3    # no new ids


def test_split_when_merged_before_first_fill(store):
    first_sync(store, [op("h1", members=["h1", "h2"], ids=[lid("111"), lid("222")])])
    r = first_sync(store, [op("h1", ids=[lid("111")]), op("h2", ids=[lid("222")])])
    assert r.persons_split == 1
    assert person_of(store, "wa_lid", "111") == 1 and person_of(store, "wa_lid", "222") != 1
    assert person_of(store, "history_contact", "h2") == person_of(store, "wa_lid", "222")
    before = count(store, "change_log")
    first_sync(store, [op("h1", ids=[lid("111")]), op("h2", ids=[lid("222")])])
    assert count(store, "change_log") == before


def test_split_never_moves_owner_rows(store):
    first_sync(store, [op("h1", members=["h1", "h2"], ids=[lid("111"), lid("222")])])
    store.conn.execute("INSERT INTO person_ids(person_id, kind, value) VALUES (1, 'phone', '4915200000005')")
    first_sync(store, [op("h1", ids=[lid("111")]), op("h2", ids=[lid("222"), pn("4915200000005")])])
    assert person_of(store, "phone", "4915200000005") == 1


def test_not_same_blocks_proof_merge_and_suggests(store):
    first_sync(store, [op("h1", ids=[lid("111")]), op("h2", ids=[lid("222")])])
    store.conn.execute("INSERT INTO person_decisions(person_a, person_b, decision) VALUES (1, 2, 'not_same')")
    r = first_sync(store, [op("h1", members=["h1", "h2"], ids=[lid("111"), lid("222")])])
    assert store.terminal(2) == 2 and r.suggestions_added == 1
    r2 = first_sync(store, [op("h1", members=["h1", "h2"], ids=[lid("111"), lid("222")])])
    assert r2.suggestions_added == 0       # never re-suggested
    assert store.terminal(2) == 2


def test_not_same_after_sync_merge_unmerges(store):
    first_sync(store, [op("h1", ids=[lid("111")]), op("h2", ids=[lid("222")])])
    first_sync(store, [op("h1", members=["h1", "h2"], ids=[lid("111"), lid("222")])])
    store.conn.execute("INSERT INTO person_decisions(person_a, person_b, decision) VALUES (2, 1, 'not_same')")
    r = first_sync(store, [op("h1", ids=[lid("111")]), op("h2", ids=[lid("222")])])
    assert store.terminal(2) == 2 and r.owner_decisions_applied == 1


def test_not_same_against_owner_merge_only_suggests(store):
    first_sync(store, [op("h1", ids=[lid("111")]), op("h2", ids=[lid("222")])])
    store.conn.execute("UPDATE persons SET merged_into = 1, merged_by = 'owner' WHERE id = 2")
    store.conn.execute("INSERT INTO person_decisions(person_a, person_b, decision) VALUES (1, 2, 'not_same')")
    r = first_sync(store, [op("h1", ids=[lid("111")]), op("h2", ids=[lid("222")])])
    assert store.terminal(2) == 1 and r.suggestions_added == 1


def test_owner_same_decision_is_applied(store):
    first_sync(store, [op("h1", ids=[lid("111")]), op("h2", ids=[lid("222")])])
    store.conn.execute("INSERT INTO person_decisions(person_a, person_b, decision) VALUES (2, 1, 'same')")
    r = first_sync(store, [op("h1", ids=[lid("111")]), op("h2", ids=[lid("222")])])
    assert r.owner_decisions_applied == 1
    assert store.terminal(2) == 1 and store.person_rows(2)["merged_by"] == "owner"
    r2 = first_sync(store, [op("h1", ids=[lid("111")]), op("h2", ids=[lid("222")])])
    assert r2.owner_decisions_applied == 0 and store.terminal(2) == 1


def test_recycled_number_goes_to_new_person(store):
    first_sync(store, [op("h1", ids=[pn("4915200000001")])])
    store.conn.execute("UPDATE person_ids SET valid_until_ms=? WHERE value='4915200000001'", (NOW - 10,))
    first_sync(store, [op("h9", ids=[pn("4915200000001", valid_from_ms=NOW)])])
    assert person_of(store, "phone", "4915200000001") != 1
    assert ended_rows(store, 1, "4915200000001") == 1


def test_odd_identifiers_and_names(store):
    r = first_sync(store, [op("h1", ids=[], names=["😈", "  "])])
    assert r.persons_created == 1 and r.names_added == 0


def test_level_never_downgrades(store):
    first_sync(store, [op("h1", ids=[lid("111")], level="contact")])
    first_sync(store, [op("h1", ids=[lid("111")], level="seen")])
    assert store.person_rows(1)["level"] == "contact"


def test_last_seen_only_grows(store):
    first_sync(store, [op("h1", ids=[lid("111")], last_seen_ms=10)])
    first_sync(store, [op("h1", ids=[lid("111")], last_seen_ms=5)])
    assert store.person_rows(1)["last_seen_ms"] == 10


def test_single_owner_flag(store):
    first_sync(store, [op("h1", ids=[lid("111")], is_owner=True), op("h2", ids=[lid("222")], is_owner=True)])
    assert count(store, "persons") == 2
    assert store.conn.execute("SELECT id FROM persons WHERE is_owner = 1").fetchall()[0][0] == 1


def test_owner_id_row_attaches_history_to_curated_person(store):
    """The owner typed a phone on a hand-made person; history finds that person by proof."""
    store.conn.execute("INSERT INTO persons DEFAULT VALUES")
    store.conn.execute("INSERT INTO person_ids(person_id, kind, value) VALUES (1, 'phone', '4915200000001')")
    r = first_sync(store, [op("h1", ids=[pn("4915200000001"), lid("111")], names=["Vinz"])])
    assert r.persons_created == 0 and person_of(store, "wa_lid", "111") == 1


def test_sync_never_writes_owner_columns_in_change_log(store):
    first_sync(store, [op("h1", ids=[lid("111")], names=["Vinz"], level="contact", is_owner=True)])
    import json
    owner_cols = {"preferred_name", "home_city", "address", "birthday", "relation", "preferred_language", "notes"}
    for old, new in store.conn.execute(
            "SELECT old, new FROM change_log WHERE writer = 'sync' AND tbl = 'persons' AND op = 'update'"):
        changed = {k for k, v in json.loads(new).items() if json.loads(old).get(k) != v}
        assert not changed & owner_cols


@pytest.mark.perf
def test_sync_performance(store):
    observed = [op(f"h{n}", ids=[lid(str(10_000 + n)), pn(str(4915200000000 + n)),
                                 lid(str(90_000 + n), valid_until_ms=5)], names=[f"P{n}"])
                for n in range(1600)]
    started = time.monotonic()
    first_sync(store, observed)
    first = time.monotonic() - started
    started = time.monotonic()
    first_sync(store, observed)
    second = time.monotonic() - started
    assert first < 5 and second < 2, (first, second)
