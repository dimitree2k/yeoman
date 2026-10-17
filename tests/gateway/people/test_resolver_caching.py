"""Synthetic witnesses for bounded work and temporal cache isolation."""

from dataclasses import replace

import pytest
from yeoman_gateway.history.live import HistoryPaused
from yeoman_gateway.history.queries import HistoryQueries
from yeoman_gateway.people.disclosure import render_person
from yeoman_gateway.people.resolver import resolve_people_in_text, resolve_person


def test_owner_recognition_does_no_history_sql_and_text_refreshes_once(directory_case):
    c = directory_case
    p = c.add_person("Named Person")
    c.set_roster("g@g.us", [c.owner, c.counterpart, c.assistant, p])
    reader = c.reader()
    resolve_person("Named Person", reader)
    history_reads, directory_reads = [], []
    reader.history.connection.set_trace_callback(lambda _: history_reads.append(1))
    c.directory._local.state.connection.set_trace_callback(lambda _: directory_reads.append(1))
    assert resolve_people_in_text(
        "Please tell me what you know about Named Person and their earlier messages today", reader
    ) == (("Named Person", p),)
    assert len(history_reads) == 0
    assert len(directory_reads) == 1


def test_group_work_is_distinct_and_roster_is_local_and_cached(directory_case, monkeypatch):
    c = directory_case
    p = c.add_person("Former")
    c.hdb.execute("BEGIN")
    for _ in range(100):
        c.add_author("g@g.us", p, at_ms=100)
    c.hdb.execute("COMMIT")
    c.set_roster("g@g.us", [c.counterpart, c.assistant])
    c.set_roster("elsewhere@g.us", [c.owner])
    reader = c.reader("group")
    calls, statements = [], []
    original = HistoryQueries.members

    def members(self, **kwargs):
        calls.append(kwargs["chat_id"])
        return original(self, **kwargs)

    monkeypatch.setattr(HistoryQueries, "members", members)
    reader.history.connection.set_trace_callback(lambda _: statements.append(1))
    assert resolve_person("Former", reader).person_id == p
    assert calls == ["g@g.us"]
    assert len(statements) <= 15
    statements.clear()
    assert resolve_person("Former", reader).person_id == p
    assert not statements
    render_person(p, reader)
    statements.clear()
    render_person(p, reader)
    assert not statements


def test_scope_cache_is_bounded_and_rechecks_generation_and_directory(directory_case):
    c = directory_case
    p = c.add_person("Visible")
    c.set_roster("g@g.us", [p])
    reader = c.reader("group")
    assert resolve_person("Visible", reader).person_id == p
    c.writer.execute("UPDATE persons SET preferred_name='Changed' WHERE id=?", (p,))
    assert resolve_person("Visible", reader).status == "not_found"
    assert resolve_person("Changed", reader).person_id == p
    for moment in range(300, 500):
        resolve_person("Changed", replace(reader, kind="owner_dm", at_ms=moment))
    assert len(c.directory._local.state.memo) <= 128
    reader.history.close()
    with pytest.raises(HistoryPaused):
        resolve_person("Changed", reader)


@pytest.mark.parametrize("middle_message", [False, True])
def test_grouped_authors_keep_temporal_owners_without_inventing_messages(
    directory_case, middle_message
):
    c = directory_case
    a, b = c.add_person("First Holder"), c.add_person("Middle Holder")
    phone = "4915000000011"
    c.add_id(a, "phone", phone, end=150)
    c.add_id(b, "phone", phone, start=150, end=200)
    c.add_id(a, "phone", phone, start=200)
    c.hdb.execute(
        "UPDATE identifier_history SET contact_id=? WHERE value=?",
        (str(a), phone + "@s.whatsapp.net"),
    )
    c.add_author("g@g.us", a, at_ms=100)
    c.add_author("g@g.us", a, at_ms=250)
    if middle_message:
        c.add_author("g@g.us", a, at_ms=175)
    reader = c.reader("group")
    assert resolve_person("First Holder", reader).person_id == a
    assert (resolve_person("Middle Holder", reader).person_id == b) is middle_message


def test_batched_assistant_role_keeps_numeric_principal_ambiguity_closed(directory_case):
    c = directory_case
    p = c.add_person("Outside")
    c.add_id(p, "wa_lid", "4915000000097")
    c.set_roster("g@g.us", [c.counterpart, c.assistant, p])
    reader = c.reader("other_dm")
    assert resolve_person("Outside", reader).status == "not_found"


def test_shared_group_author_labels_do_not_depend_on_membership_elsewhere(directory_case):
    c = directory_case
    p = c.add_person("Former")
    c.set_roster("g@g.us", [c.counterpart, c.assistant], label="Earlier Group")
    c.add_author("g@g.us", p, at_ms=100)
    c.set_roster("other@g.us", [c.counterpart, c.assistant, p], label="Current Group")
    reader = c.reader("other_dm")
    assert resolve_person("Former", reader).person_id == p
    assert render_person(p, reader).shared_chat_names == ("Current Group", "Earlier Group")


def test_reused_roster_parsing_never_reuses_temporal_membership(directory_case):
    c = directory_case
    p = c.add_person("Member")
    c.set_roster("g@g.us", [p], at_ms=1)
    c.set_roster("other@g.us", [p], at_ms=1)
    c.set_roster("g@g.us", [], at_ms=200)
    reader = c.reader("group", at_ms=100)
    assert resolve_person("Member", reader).person_id == p
    assert resolve_person("Member", replace(reader, chat_id="other@g.us")).person_id == p
    assert resolve_person("Member", replace(reader, at_ms=300)).status == "not_found"
    assert resolve_person("Member", replace(reader, chat_id="other@g.us", at_ms=300)).person_id == p
