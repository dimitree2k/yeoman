from __future__ import annotations

import json
import sqlite3

import pytest
from yeoman_gateway.history import schema as history_schema
from yeoman_gateway.people.history_source import read_observed_persons

from tests.gateway.people.people_fixtures import (
    ALEX_LID,
    BIGTALKER_LID,
    DM_PN,
    EXTRA_LID,
    LURKER_LID,
    level_history,
)


def by_raw_id(persons, raw: str):
    digits = raw.split("@", 1)[0]
    matches = [p for p in persons if any(i.value == digits for i in p.ids)]
    assert len(matches) == 1, raw
    return matches[0]


@pytest.fixture
def persons(tmp_path):
    history = level_history(tmp_path)
    try:
        yield read_observed_persons(history.snapshot().connection, contact_group_max_members=4)
    finally:
        history.close()


def test_levels_and_terminals(persons):
    assert by_raw_id(persons, ALEX_LID).level == "contact"       # wrote in small group G
    assert by_raw_id(persons, LURKER_LID).level == "seen"        # only in B's member list
    assert by_raw_id(persons, EXTRA_LID).level == "seen"
    assert by_raw_id(persons, BIGTALKER_LID).level == "seen"     # wrote only in big group B
    assert by_raw_id(persons, DM_PN).level == "contact"          # DM counterpart
    assert sum(p.is_owner for p in persons) == 1
    assert next(p for p in persons if p.is_owner).level == "contact"


def test_phone_and_lid_of_one_person_share_a_terminal(persons):
    person = by_raw_id(persons, ALEX_LID)
    assert {i.kind for i in person.ids} == {"phone", "wa_lid"}
    assert ("Alex Grrr", "push_name") in person.names
    assert person.last_seen_ms is not None


def test_assistant_is_skipped(persons):
    assert not any(any(i.value == "4915202777685" for i in p.ids) for p in persons)


def test_big_group_threshold_default_counts_small_groups(tmp_path):
    history = level_history(tmp_path)
    try:
        persons = read_observed_persons(history.snapshot().connection)
    finally:
        history.close()
    assert by_raw_id(persons, BIGTALKER_LID).level == "contact"   # 5 members <= 100


def bare_history(tmp_path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "history.db")
    history_schema.create(conn)
    return conn


def contact(conn, cid, *, merged_into=None, role=None, name=None, kind="person"):
    conn.execute("INSERT INTO contacts VALUES (?, ?, ?, ?, 'confirmed', ?, '[]')",
                 (cid, kind, role, name, merged_into))


def ident(conn, cid, kind, value, **times):
    conn.execute(
        "INSERT INTO identifier_history(contact_id, channel, kind, value, strength, evidence, "
        "first_seen_ms, last_seen_ms, ended_ms, valid_from_ms, valid_until_ms, source_refs) "
        "VALUES (?, 'whatsapp', ?, ?, 'strong', 'observed', ?, ?, ?, ?, ?, '[]')",
        (cid, kind, value, times.get("first"), times.get("last"), times.get("ended"),
         times.get("valid_from"), times.get("valid_until")))


def test_merged_history_contacts_are_members_of_one_terminal(tmp_path):
    conn = bare_history(tmp_path)
    contact(conn, "c")
    contact(conn, "b", merged_into="c")
    contact(conn, "a", merged_into="b")
    ident(conn, "a", "lid", "111@lid")
    ident(conn, "b", "pn_jid", "4915200000001@s.whatsapp.net", ended=50)
    ident(conn, "c", "push_name", "Vinz")
    [person] = read_observed_persons(conn)
    assert person.history_terminal == "c"
    assert person.history_members == ("a", "b", "c")
    assert {(i.kind, i.valid_until_ms) for i in person.ids} == {("wa_lid", None), ("phone", 50)}
    assert person.names == (("Vinz", "push_name"),)
    assert person.level == "seen"


def test_newsletter_and_cycles_do_not_crash(tmp_path):
    conn = bare_history(tmp_path)
    contact(conn, "n")
    ident(conn, "n", "newsletter", "1203@newsletter")
    contact(conn, "x", merged_into="y")
    contact(conn, "y", merged_into="x")
    ident(conn, "x", "lid", "222@lid")
    contact(conn, "e", name="😈")
    ident(conn, "e", "numeric", "4915200000002")
    persons = read_observed_persons(conn)
    assert not any(p.history_terminal == "n" for p in persons)
    assert sum(1 for p in persons if any(i.value == "222" for i in p.ids)) == 1
    emoji = next(p for p in persons if p.history_terminal == "e")
    assert [i.kind for i in emoji.ids] == ["phone"]


def test_duplicate_identifiers_merge_seen_times(tmp_path):
    conn = bare_history(tmp_path)
    contact(conn, "b")
    contact(conn, "a", merged_into="b")
    ident(conn, "a", "lid", "111@lid", first=5, last=10)
    ident(conn, "b", "lid", "111:2@lid", first=3, last=7)
    [person] = read_observed_persons(conn)
    [only] = person.ids
    assert (only.first_seen_ms, only.last_seen_ms) == (3, 10)


def test_group_without_snapshot_counts_as_small(tmp_path):
    conn = bare_history(tmp_path)
    contact(conn, "a")
    ident(conn, "a", "lid", "111@lid")
    conn.execute(
        "INSERT INTO messages(message_id, channel, chat_id, sender_contact_id, sender_basis, direction, "
        "sent_ms, time_certainty, provenance, source_refs) VALUES "
        "('m', 'whatsapp', 'g@g.us', 'a', 'native_identifier', 'in', 99, 'native', 'native', '[]')")
    big = json.dumps({"participants": [[str(n)] for n in range(5)]})
    conn.execute(
        "INSERT INTO message_events(event_id, kind, channel, chat_id, actor_basis, occurred_ms, "
        "time_certainty, payload_json, provenance, source_refs) VALUES "
        "('e', 'member_snapshot', 'whatsapp', 'other@g.us', 'unknown', 1, 'native', ?, 'native', '[]')", (big,))
    [person] = read_observed_persons(conn, contact_group_max_members=2)
    assert person.level == "contact" and person.last_seen_ms == 99
