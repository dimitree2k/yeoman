from __future__ import annotations

import json
import sqlite3
import stat

import pytest
from yeoman_gateway.people.schema import open_people_db, people_db_path, writer


def seeded(tmp_path):
    conn = open_people_db(tmp_path / "people.db")
    conn.execute("INSERT INTO persons DEFAULT VALUES")
    conn.execute("INSERT INTO person_names(person_id, name, kind, source) "
                 "VALUES (1, 'Vinz', 'push_name', 'observed')")
    conn.execute("INSERT INTO person_ids(person_id, kind, value, source) "
                 "VALUES (1, 'wa_lid', '111', 'observed')")
    return conn


def seeded_two_persons(tmp_path):
    conn = open_people_db(tmp_path / "people.db")
    conn.execute("INSERT INTO persons DEFAULT VALUES")
    conn.execute("INSERT INTO persons DEFAULT VALUES")
    return conn


def test_owner_friendly_inserts(tmp_path):
    conn = open_people_db(tmp_path / "people.db")
    conn.execute("INSERT INTO persons DEFAULT VALUES")            # owner creates a person by hand
    conn.execute("INSERT INTO person_names(person_id, name) VALUES (1, 'Wimds')")
    conn.execute("INSERT INTO person_ids(person_id, kind, value) VALUES (1, 'phone', '4915223369086')")
    row = conn.execute("SELECT kind, source FROM person_names").fetchone()
    assert tuple(row) == ("nickname", "owner")
    assert conn.execute("SELECT source FROM person_ids").fetchone()[0] == "owner"
    assert conn.execute("SELECT created_ms FROM persons").fetchone()[0] > 0


def test_persons_and_observed_rows_cannot_be_deleted(tmp_path):
    conn = seeded(tmp_path)
    conn.execute("INSERT INTO person_names(person_id, name) VALUES (1, 'Wimds')")
    with pytest.raises(sqlite3.IntegrityError, match="never deleted"):
        conn.execute("DELETE FROM persons WHERE id = 1")
    with pytest.raises(sqlite3.IntegrityError, match="hidden = 1"):
        conn.execute("DELETE FROM person_names WHERE source = 'observed'")
    with pytest.raises(sqlite3.IntegrityError, match="hidden = 1"):
        conn.execute("DELETE FROM person_ids WHERE source = 'observed'")
    conn.execute("DELETE FROM person_names WHERE source = 'owner'")   # owner typo: allowed
    assert conn.execute("SELECT count(*) FROM person_names").fetchone()[0] == 1


def test_ids_never_reused(tmp_path):
    conn = open_people_db(tmp_path / "people.db")
    conn.execute("INSERT INTO persons DEFAULT VALUES")
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("DROP TRIGGER persons_no_delete")
    conn.execute("DELETE FROM persons")
    conn.execute("INSERT INTO persons DEFAULT VALUES")
    assert conn.execute("SELECT max(id) FROM persons").fetchone()[0] == 2


def test_one_active_row_per_identifier(tmp_path):
    conn = seeded_two_persons(tmp_path)
    conn.execute("INSERT INTO person_ids(person_id, kind, value) VALUES (1,'phone','4915200000001')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO person_ids(person_id, kind, value) VALUES (2,'phone','4915200000001')")
    conn.execute("UPDATE person_ids SET valid_until_ms = 1 WHERE person_id = 1")   # number ended
    conn.execute("INSERT INTO person_ids(person_id, kind, value) VALUES (2,'phone','4915200000001')")


def test_change_log_records_writer(tmp_path):
    conn = open_people_db(tmp_path / "people.db")
    conn.execute("BEGIN IMMEDIATE")
    with writer(conn, "sync"):
        conn.execute("INSERT INTO persons(level) VALUES ('contact')")
    conn.execute("COMMIT")
    conn.execute("UPDATE persons SET preferred_name = 'Vincent' WHERE id = 1")
    rows = conn.execute("SELECT tbl, op, writer FROM change_log ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [("persons", "insert", "sync"), ("persons", "update", "manual")]
    new = conn.execute("SELECT new FROM change_log WHERE id = 2").fetchone()[0]
    assert json.loads(new)["preferred_name"] == "Vincent"


def test_writer_needs_transaction(tmp_path):
    conn = open_people_db(tmp_path / "people.db")
    with pytest.raises(RuntimeError):
        with writer(conn, "sync"):
            pass


def test_effective_level_is_curated_after_owner_edit(tmp_path):
    conn = seeded(tmp_path)  # person 1, level 'seen', only observed rows
    assert conn.execute("SELECT effective_level FROM persons_v WHERE id=1").fetchone()[0] == "seen"
    conn.execute("UPDATE persons SET birthday = '--03-14' WHERE id = 1")
    assert conn.execute("SELECT effective_level FROM persons_v WHERE id=1").fetchone()[0] == "curated"


def test_birthday_check(tmp_path):
    conn = seeded(tmp_path)
    for bad in ("14.03.1990", "1990-3-14"):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE persons SET birthday = ? WHERE id = 1", (bad,))
    conn.execute("UPDATE persons SET birthday = '1990-03-14' WHERE id = 1")


def test_file_and_dir_modes(tmp_path):
    path = tmp_path / "people" / "people.db"
    open_people_db(path).close()
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_reopen_keeps_schema_and_refuses_newer(tmp_path):
    path = tmp_path / "people.db"
    conn = open_people_db(path)
    conn.execute("INSERT INTO persons DEFAULT VALUES")
    conn.close()
    conn = open_people_db(path)
    assert conn.execute("SELECT count(*) FROM persons").fetchone()[0] == 1
    conn.execute("UPDATE meta SET value = '2' WHERE key = 'schema_version'")
    conn.close()
    with pytest.raises(RuntimeError):
        open_people_db(path)


def test_readonly_open_refuses_writes(tmp_path):
    path = tmp_path / "people.db"
    open_people_db(path).close()
    conn = open_people_db(path, readonly=True)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO persons DEFAULT VALUES")


def test_people_db_path_under_yeoman_home(tmp_path, monkeypatch):
    monkeypatch.setenv("YEOMAN_HOME", str(tmp_path))
    path = people_db_path(create=True)
    assert path == tmp_path / "data" / "people" / "people.db"
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
