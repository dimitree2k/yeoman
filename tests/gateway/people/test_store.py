from __future__ import annotations

import pytest
from yeoman_gateway.people.identifiers import ObservedId
from yeoman_gateway.people.schema import open_people_db
from yeoman_gateway.people.store import PeopleIntegrityError, PeopleStore

NOW = 1_791_000_000_000


@pytest.fixture
def store(tmp_path):
    conn = open_people_db(tmp_path / "people.db")
    yield PeopleStore(conn)
    conn.close()


def store_person(store: PeopleStore) -> int:
    with store.transaction("sync"):
        return store.create_person(level="seen", now_ms=NOW)


def oid(kind, value, **times):
    return ObservedId(kind, value, **times)


def test_attach_adds_then_touches(store):
    p = store_person(store)
    assert store.attach_id(p, oid("wa_lid", "111", last_seen_ms=5), NOW) == "added"
    assert store.attach_id(p, oid("wa_lid", "111", last_seen_ms=9), NOW) == "touched"
    assert store.conn.execute("SELECT last_seen_ms, source FROM person_ids").fetchone()[:] == (9, "observed")


def test_attach_hidden_identifier_is_refused(store):
    p = store_person(store)
    store.conn.execute("INSERT INTO person_ids(person_id, kind, value, source, hidden) "
                       "VALUES (?, 'phone', '4915200000001', 'observed', 1)", (p,))
    assert store.attach_id(p, oid("phone", "4915200000001"), NOW) == "hidden"


def test_attach_taken_by_other_person(store):
    a, b = store_person(store), store_person(store)
    assert store.attach_id(a, oid("wa_lid", "111"), NOW) == "added"
    assert store.attach_id(b, oid("wa_lid", "111"), NOW) == "taken"


def test_attach_on_merged_alias_is_touched(store):
    a, b = store_person(store), store_person(store)
    store.attach_id(b, oid("wa_lid", "111"), NOW)
    assert store.merge(b, a, by="sync")
    assert store.attach_id(a, oid("wa_lid", "111"), NOW + 1) == "touched"


def test_attach_never_touches_owner_rows(store):
    p = store_person(store)
    store.conn.execute("INSERT INTO person_ids(person_id, kind, value) VALUES (?, 'wa_lid', '111')", (p,))
    assert store.attach_id(p, oid("wa_lid", "111", last_seen_ms=9), NOW) == "touched"
    assert store.conn.execute("SELECT last_seen_ms FROM person_ids").fetchone()[0] is None


def test_ended_identifier_inserted_once(store):
    p = store_person(store)
    ended = oid("phone", "4915200000001", valid_from_ms=1, valid_until_ms=2)
    assert store.attach_id(p, ended, NOW) == "added"
    assert store.attach_id(p, ended, NOW) == "touched"
    assert store.attach_id(p, oid("phone", "4915200000001"), NOW) == "added"   # no unique conflict


def test_find_active_at_time(store):
    a, b = store_person(store), store_person(store)
    store.attach_id(a, oid("phone", "4915200000001", valid_from_ms=10, valid_until_ms=20), NOW)
    store.attach_id(b, oid("phone", "4915200000001", valid_from_ms=20), NOW)
    assert store.find_active("phone", "4915200000001") == b
    assert store.find_active("phone", "4915200000001", at_ms=15) == a
    assert store.find_active("phone", "4915200000001", at_ms=25) == b
    assert store.find_active("phone", "4915200000001", at_ms=5) is None


def test_sync_fields_cannot_reach_owner_columns(store):
    p = store_person(store)
    with pytest.raises(ValueError):
        store.set_sync_fields(p, preferred_name="x")


def test_sync_fields_only_write_changes(store):
    p = store_person(store)
    assert store.set_sync_fields(p, level="contact", last_seen_ms=5, is_owner=True)
    assert not store.set_sync_fields(p, level="contact", last_seen_ms=5, is_owner=True)


def test_merge_refused_after_not_same(store):
    a, b = store_person(store), store_person(store)
    store.conn.execute("INSERT INTO person_decisions(person_a, person_b, decision) VALUES (?, ?, 'not_same')", (a, b))
    assert store.merge(b, a, by="sync") is False


def test_merge_refused_when_not_same_inside_groups(store):
    a, b, c = (store_person(store) for _ in range(3))
    assert store.merge(c, b, by="sync")
    store.conn.execute("INSERT INTO person_decisions(person_a, person_b, decision) VALUES (?, ?, 'not_same')", (a, c))
    assert store.merge(b, a, by="sync") is False


def test_latest_decision_wins(store):
    a, b = store_person(store), store_person(store)
    store.conn.execute("INSERT INTO person_decisions(person_a, person_b, decision) VALUES (?, ?, 'not_same')", (a, b))
    store.conn.execute("INSERT INTO person_decisions(person_a, person_b, decision) VALUES (?, ?, 'same')", (b, a))
    assert store.latest_decision(a, b) == "same"


def test_unmerge_sync_leaves_owner_merges(store):
    a, b, c = (store_person(store) for _ in range(3))
    store.merge(b, a, by="sync")
    store.merge(c, a, by="owner")
    assert store.unmerge_sync(b) is True and store.unmerge_sync(c) is False
    assert store.terminal(b) == b and store.terminal(c) == a


def test_terminal_detects_cycle(store):
    a, b = store_person(store), store_person(store)
    store.conn.execute("UPDATE persons SET merged_into = ? WHERE id = ?", (b, a))
    store.conn.execute("UPDATE persons SET merged_into = ? WHERE id = ?", (a, b))
    with pytest.raises(PeopleIntegrityError):
        store.terminal(a)


def test_emoji_only_name_is_not_stored(store):
    assert store.add_observed_name(store_person(store), "😈🔥", "push_name", NOW) == "empty"
    assert store.add_observed_name(store_person(store), "   ", "push_name", NOW) == "empty"


def test_name_added_then_touched(store):
    p = store_person(store)
    assert store.add_observed_name(p, "Vinz", "push_name", NOW) == "added"
    assert store.add_observed_name(p, "Vinz", "push_name", NOW + 1) == "touched"


def test_hidden_name_blocks_any_kind(store):
    p = store_person(store)
    store.conn.execute("INSERT INTO person_names(person_id, name, kind, source, hidden) "
                       "VALUES (?, 'AlexGrrrrr😈', 'push_name', 'observed', 1)", (p,))
    assert store.add_observed_name(p, "AlexGrrrrr😈", "contact_name", NOW) == "hidden"


def test_suggest_once_regardless_of_order(store):
    a, b = store_person(store), store_person(store)
    assert store.suggest("merge", a, b) is True
    assert store.suggest("merge", b, a) is False
    store.conn.execute("UPDATE suggestions SET status = 'rejected'")
    assert store.suggest("merge", a, b) is False


def test_move_observed_ids_skips_owner_rows(store):
    a, b = store_person(store), store_person(store)
    store.attach_id(a, oid("wa_lid", "111"), NOW)
    store.conn.execute("INSERT INTO person_ids(person_id, kind, value) VALUES (?, 'wa_lid', '222')", (a,))
    ids = [r[0] for r in store.conn.execute("SELECT id FROM person_ids")]
    assert store.move_observed_ids(ids, b) == 1


def test_transaction_rolls_back_on_error(store):
    with pytest.raises(RuntimeError):
        with store.transaction("sync"):
            store.create_person(level="seen", now_ms=NOW)
            raise RuntimeError
    assert store.conn.execute("SELECT count(*) FROM persons").fetchone()[0] == 0
    assert store.conn.execute("SELECT name FROM writer_context").fetchone()[0] is None


def test_transaction_labels_change_log(store):
    store_person(store)
    assert store.conn.execute("SELECT writer FROM change_log").fetchone()[0] == "sync"
    assert store.conn.execute("SELECT name FROM writer_context").fetchone()[0] is None
