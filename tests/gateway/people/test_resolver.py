import os
import sqlite3
import time
from dataclasses import replace

import pytest
from yeoman_gateway.people.resolver import (
    PeopleResolver,
    PeopleUnavailable,
    name_tokens,
    resolve_people_in_text,
    resolve_person,
)


def ids(result):
    return (
        {result.person_id}
        if result.status == "resolved"
        else {c.person_id for c in result.candidates}
    )


def test_owner_exact_beats_observed_and_seen_needs_exact(directory_case):
    c = directory_case
    a, b = c.add_person("Vincent"), c.add_person("Other")
    c.add_name(b, "Vincent")
    assert ids(resolve_person("Vincent", c.reader())) == {a}
    s = c.add_person("Quiet Person", source="observed", level="seen")
    assert ids(resolve_person("Quiet Person", c.reader())) == {s}
    for query in ("Qui", "Vi"):
        assert resolve_person(query, c.reader()).status == "not_found"


def test_recycled_identifier_uses_half_open_validity(directory_case):
    c = directory_case
    a, b = c.add_person("Old"), c.add_person("New")
    c.add_id(a, "phone", "4915000000011", end=200)
    c.add_id(b, "phone", "4915000000011", start=200)
    assert ids(resolve_person("+49 150 00000011", c.reader(at_ms=199))) == {a}
    assert ids(resolve_person("004915000000011", c.reader(at_ms=200))) == {b}
    assert resolve_person("015000000011", c.reader()).status == "not_found"
    assert ids(resolve_person("015000000011", c.reader(national_calling_code="49"))) == {b}


def test_data_version_reload_and_owner_write_lock(directory_case):
    c = directory_case
    a = c.add_person("Before")
    assert ids(resolve_person("Before", c.reader())) == {a}
    c.writer.execute("BEGIN IMMEDIATE")
    c.writer.execute("UPDATE persons SET preferred_name='After' WHERE id=?", (a,))
    start = time.perf_counter()
    assert ids(resolve_person("Before", c.reader())) == {a}
    assert time.perf_counter() - start < 0.1
    c.writer.execute("COMMIT")
    assert ids(resolve_person("After", c.reader())) == {a}
    assert resolve_person("Before", c.reader()).status == "not_found"


def test_redirect_cycle_fails_closed(directory_case):
    c = directory_case
    a, b = c.add_person("One"), c.add_person("Two")
    c.writer.execute("UPDATE persons SET merged_into=? WHERE id=?", (b, a))
    c.writer.execute("UPDATE persons SET merged_into=? WHERE id=?", (a, b))
    with pytest.raises(PeopleUnavailable, match="^people_integrity$"):
        resolve_person("One", c.reader())


@pytest.mark.parametrize(
    "query",
    [
        "+49 150 00000011",
        "0049-150-00000011",
        "4915000000011",
        "phone:+4915000000011",
        "4915000000011@s.whatsapp.net",
        "4915000000011:2@s.whatsapp.net",
        "@4915000000011@s.whatsapp.net",
        "wa_lid:300000000001",
        "lid:300000000001",
        "300000000001@lid",
        "@300000000001@lid",
        "@300000000001",
    ],
)
def test_exact_identifier_forms_and_boundaries(directory_case, query):
    c = directory_case
    p = c.add_person("Subject", ids=(("phone", "4915000000011"), ("wa_lid", "300000000001")))
    assert ids(resolve_person(query, c.reader())) == {p}


@pytest.mark.parametrize(
    "query",
    [
        "x4915000000011y",
        "4915000000011@s.whatsapp.net.evil",
        "x300000000001@lid",
        "300000000001@lid.evil",
        "history_contact:4",
        "knowledge_person:4",
        "phone:Vincent",
        "person:bad",
        "telegram:1234567",
        "@synthetic_user",
    ],
)
def test_bad_identifier_never_falls_through_to_names(directory_case, query):
    c = directory_case
    c.add_person(query)
    assert resolve_person(query, c.reader()).status == "not_found"


@pytest.mark.parametrize(
    "query",
    [
        "telegram:1234567",
        "telegram_id:1234567",
        "1234567",
        "@1234567",
        "@synthetic_user",
        "telegram_username:synthetic_user",
    ],
)
def test_telegram_exact(directory_case, query):
    c = directory_case
    p = c.add_person(
        "Telegram Person", ids=(("telegram_id", "1234567"), ("telegram_username", "synthetic_user"))
    )
    assert ids(resolve_person(query, replace(c.reader(), channel="telegram"))) == {p}


def test_numeric_interpretations_remain_ambiguous(directory_case):
    c = directory_case
    a = c.add_person("Phone", ids=(("phone", "4915000000011"),))
    b = c.add_person("Lid", ids=(("wa_lid", "4915000000011"),))
    assert ids(resolve_person("4915000000011", c.reader())) == {a, b}
    assert ids(resolve_person(f"person:{a}", c.reader())) == {a}


def test_token_prefix_all_words_and_exact_short_name(directory_case):
    c = directory_case
    a = c.add_person("Alex Müller")
    b = c.add_person("Alexander Müller")
    c.add_person("Alexa Weber")
    short = c.add_person("Al")
    assert ids(resolve_person("Alex", c.reader())) == {a}
    assert ids(resolve_person("Ale Mül", c.reader())) == {a, b}
    assert ids(resolve_person("ＡＬＥＸ—Müller!!", c.reader())) == {a}
    assert ids(resolve_person("Al", c.reader())) == {short}
    c.add_name(a, "Different Alias")
    assert resolve_person("Alex Different", c.reader()).status == "not_found"
    assert resolve_person("😈", c.reader()).status == "not_found"
    assert name_tokens("ＡlexGrrrrr😈 42") == ("alexgrrrrr", "42")


def test_hidden_rows_never_match_after_reload(directory_case):
    c = directory_case
    a = c.add_person("Subject")
    c.add_name(a, "Hidden Alias", source="owner", hidden=True)
    c.add_name(a, "Hidden Alias")
    c.add_id(a, "phone", "4915000000011", hidden=True)
    for query in ("Hidden Alias", "+4915000000011"):
        assert resolve_person(query, c.reader()).status == "not_found"
    c.add_name(a, "Visible Alias")
    assert ids(resolve_person("Visible Alias", c.reader())) == {a}
    c.writer.execute("UPDATE person_names SET hidden=1 WHERE name='Visible Alias'")
    assert resolve_person("Visible Alias", c.reader()).status == "not_found"
    b = c.add_person("Hidden Alias")
    assert ids(resolve_person("Hidden Alias", c.reader())) == {b}


def test_merge_unmerge_reload_collapses_only_actual_redirects(directory_case):
    c = directory_case
    a, b = c.add_person("Sam Twins"), c.add_person("Sam Twins")
    c.writer.execute(
        "INSERT INTO person_decisions(person_a,person_b,decision) VALUES (?,?,'not_same')", (a, b)
    )
    assert ids(resolve_person("Sam Twins", c.reader())) == {a, b}
    c.writer.execute("UPDATE persons SET merged_into=? WHERE id=?", (a, b))
    assert ids(resolve_person("Sam Twins", c.reader())) == {a}
    assert ids(resolve_person(f"person:{b}", c.reader())) == {a}
    c.writer.execute("UPDATE persons SET merged_into=NULL WHERE id=?", (b,))
    assert ids(resolve_person("Sam Twins", c.reader())) == {a, b}


def test_overlapping_temporal_ids_are_ambiguous(directory_case):
    c = directory_case
    a, b = c.add_person("One"), c.add_person("Two")
    c.add_id(a, "phone", "4915000000011", end=400)
    c.add_id(b, "phone", "4915000000011", start=200)
    assert ids(resolve_person("+4915000000011", c.reader())) == {a, b}


def test_reader_scope_group_dm_owner_and_seen(directory_case):
    c = directory_case
    member = c.add_person("Quiet Person", source="observed", level="seen")
    former, weak, future, outsider = [
        c.add_person(n) for n in ("Former", "Weak", "Future", "Outsider")
    ]
    c.set_roster("g@g.us", [c.counterpart, c.assistant, member])
    c.add_author("g@g.us", former, at_ms=100)
    c.add_author("g@g.us", weak, at_ms=100, basis="push_name")
    c.add_author("g@g.us", future, at_ms=400)
    for kind in ("group", "other_dm"):
        reader = c.reader(kind)
        assert ids(resolve_person("Quiet Person", reader)) == {member}
        assert ids(resolve_person("Former", reader)) == {former}
        assert resolve_person("Qui", reader).status == "not_found"
        for p in (weak, future, outsider):
            assert resolve_person(f"person:{p}", reader).status == "not_found"
    assert ids(resolve_person("Outsider", c.reader())) == {outsider}
    c.hdb.execute("UPDATE contacts SET role=NULL WHERE role='assistant'")
    assert c.directory.scope(c.reader("other_dm")).eligible_ids == frozenset({c.counterpart})


def test_candidate_chats_are_reader_shared_only(directory_case):
    c = directory_case
    p = c.add_person("Subject")
    c.set_roster("g@g.us", [p, c.counterpart, c.assistant])
    c.set_roster("private@g.us", [p, c.owner, c.assistant])
    assert c.directory.scope(c.reader("other_dm")).shared_chats == ("g@g.us",)


def test_index_reopens_on_file_replacement(directory_case, tmp_path):
    c = directory_case
    c.writer.execute("PRAGMA journal_mode=DELETE")
    c.add_person("Before")
    assert resolve_person("Before", c.reader()).status == "resolved"
    replacement = tmp_path / "replacement.db"
    with sqlite3.connect(replacement) as target:
        c.writer.backup(target)
        target.execute("UPDATE persons SET preferred_name='After' WHERE preferred_name='Before'")
    os.replace(replacement, c.path)
    assert resolve_person("Before", c.reader()).status == "not_found"
    assert resolve_person("After", c.reader()).status == "resolved"


def test_text_spans_refuse_ambiguity(directory_case):
    c = directory_case
    a = c.add_person("Vincent Long")
    c.add_name(a, "Vinz")
    assert resolve_people_in_text("Was weißt du über Vincent Long und Vinz?", c.reader()) == (
        ("Vincent Long", a),
    )
    c.add_person("Alex One")
    c.add_person("Alex Two")
    assert resolve_people_in_text("Vincent Long und Alex?", c.reader()) == ()
    assert resolve_people_in_text("Was weißt du über ihn?", c.reader()) == ()


def test_missing_database_is_content_free_and_never_created(tmp_path):
    directory = PeopleResolver(tmp_path / "absent.db")
    with pytest.raises(PeopleUnavailable, match="^people_unavailable$"):
        directory.scope(None)
    assert not (tmp_path / "absent.db").exists()


def test_connections_are_readonly_and_owned_by_execution_thread(directory_case):
    from concurrent.futures import ThreadPoolExecutor

    c = directory_case
    p = c.add_person("Thread Person")
    reader = replace(c.reader(), history=None)
    assert resolve_person("Thread Person", reader).person_id == p
    connection = c.directory._local.state.connection
    assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
    assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 0
    assert not connection.in_transaction
    with pytest.raises(sqlite3.OperationalError):
        connection.execute("UPDATE persons SET notes='forbidden'")

    def worker():
        try:
            assert resolve_person("Thread Person", reader).person_id == p
            assert c.directory._local.state.connection is not connection
        finally:
            c.directory.close()

    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(worker).result(timeout=30)
    assert resolve_person("Thread Person", reader).person_id == p


def test_failed_reload_never_uses_stale_index(directory_case):
    c = directory_case
    p = c.add_person("Before")
    assert resolve_person("Before", c.reader()).person_id == p
    c.writer.execute("DROP VIEW persons_v")
    with pytest.raises(PeopleUnavailable, match="^people_unavailable$"):
        resolve_person("Before", c.reader())


@pytest.mark.parametrize("shape", ["dangling", "too_deep"])
def test_invalid_redirect_chain_fails_closed(directory_case, shape):
    c = directory_case
    p = c.add_person("First")
    if shape == "dangling":
        c.writer.execute("PRAGMA foreign_keys=OFF")
        c.writer.execute("UPDATE persons SET merged_into=999999 WHERE id=?", (p,))
    else:
        for n in range(65):
            p = c.add_person("Link " + str(n), merged_into=p)
    with pytest.raises(PeopleUnavailable, match="^people_integrity$"):
        resolve_person("First", c.reader())
