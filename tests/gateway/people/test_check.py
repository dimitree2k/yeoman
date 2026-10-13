from __future__ import annotations

from yeoman_gateway.people.check import check_people_db
from yeoman_gateway.people.schema import open_people_db


def codes(conn):
    return {f.code for f in check_people_db(conn)}


def test_check_flags_unnormalized_phone(tmp_path):
    conn = open_people_db(tmp_path / "people.db")
    conn.execute("INSERT INTO persons DEFAULT VALUES")
    conn.execute("INSERT INTO person_ids(person_id, kind, value) VALUES (1, 'phone', '+49 152 2336 9086')")
    findings = check_people_db(conn)
    assert "invalid_value" in {f.code for f in findings}
    assert all("152" not in f.detail for f in findings)       # no values in findings


def test_check_finds_cycle_and_overlap_and_bad_birthday(tmp_path):
    conn = open_people_db(tmp_path / "people.db")
    for _ in range(4):
        conn.execute("INSERT INTO persons DEFAULT VALUES")
    conn.execute("UPDATE persons SET merged_into = 2 WHERE id = 1")
    conn.execute("UPDATE persons SET merged_into = 1 WHERE id = 2")
    conn.execute("INSERT INTO person_ids(person_id, kind, value, valid_until_ms) VALUES (3, 'phone', '4915200000001', 100)")
    conn.execute("INSERT INTO person_ids(person_id, kind, value, valid_from_ms) VALUES (4, 'phone', '4915200000001', 50)")
    conn.execute("UPDATE persons SET birthday = '2026-02-30', preferred_language = 'German' WHERE id = 3")
    found = codes(conn)
    assert {"merge_cycle", "validity_overlap", "invalid_birthday", "invalid_language"} <= found


def test_check_merge_chain_and_ambiguous_nickname(tmp_path):
    conn = open_people_db(tmp_path / "people.db")
    for _ in range(4):
        conn.execute("INSERT INTO persons DEFAULT VALUES")
    conn.execute("UPDATE persons SET merged_into = 3 WHERE id = 2")
    conn.execute("UPDATE persons SET merged_into = 2 WHERE id = 1")
    conn.execute("INSERT INTO person_names(person_id, name) VALUES (1, 'Alex')")
    conn.execute("INSERT INTO person_names(person_id, name) VALUES (4, 'alex')")
    findings = check_people_db(conn)
    assert {(f.code, f.severity) for f in findings} == {("merge_chain", "info"), ("ambiguous_owner_nickname", "info")}


def test_check_clean_db_has_no_errors(tmp_path):
    conn = open_people_db(tmp_path / "people.db")
    conn.execute("INSERT INTO persons(birthday, preferred_language) VALUES ('--02-29', 'de-AT')")
    conn.execute("INSERT INTO persons DEFAULT VALUES")
    conn.execute("INSERT INTO person_ids(person_id, kind, value, valid_until_ms) VALUES (1, 'phone', '4915200000001', 50)")
    conn.execute("INSERT INTO person_ids(person_id, kind, value, valid_from_ms) VALUES (2, 'phone', '4915200000001', 50)")
    conn.execute("INSERT INTO person_ids(person_id, kind, value) VALUES (2, 'history_contact', 'any-uuid')")
    assert check_people_db(conn) == []


def test_check_foreign_key_and_stuck_writer(tmp_path):
    conn = open_people_db(tmp_path / "people.db")
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("INSERT INTO person_names(person_id, name) VALUES (99, 'x')")
    conn.execute("UPDATE writer_context SET name = 'sync'")
    assert {"foreign_key", "writer_context_stuck"} <= codes(conn)
