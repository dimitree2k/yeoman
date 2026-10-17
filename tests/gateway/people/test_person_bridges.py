import json
import math
import time

import pytest
from yeoman_gateway.history.live import HistoryPaused
from yeoman_gateway.history.queries import HistoryQueries
from yeoman_gateway.people.bridges import history_person, knowledge_person_ids
from yeoman_gateway.people.resolver import resolve_person

from .test_resolver_perf import real_size_directory as _real_size_directory


def read(c, person, query="note"):
    reader = c.reader()
    return HistoryQueries(reader.history).search_person(
        chat_ids=("g@g.us",),
        person=history_person(person, reader),
        at_ms=300,
        limit=50,
        query=query,
    )


def test_history_by_person_recycled_number_never_broadens_contact(directory_case):
    c = directory_case
    a, b = c.add_person("Old"), c.add_person("New")
    c.add_id(a, "phone", "4915000000011", end=200)
    c.add_id(b, "phone", "4915000000011", start=200)
    c.add_id(a, "phone", "4915000000012")
    c.add_id(a, "wa_lid", "300000000001")
    merged = c.add_person("Merged", ids=(("phone", "4915000000013"),), merged_into=a)
    c.hdb.execute("UPDATE identifier_history SET contact_id=? WHERE contact_id=?", (str(a), str(b)))
    for mid, ident, at in [
        ("old", "4915000000011@s.whatsapp.net", 199),
        ("new", "4915000000011@s.whatsapp.net", 200),
        ("second", "4915000000012@s.whatsapp.net", 200),
        ("lid", "300000000001@lid", 200),
        ("merged", c.endpoint(merged), 200),
        ("unknown-time", "4915000000011@s.whatsapp.net", None),
    ]:
        c.add_message(mid, sender_person=a, identifier=ident, at_ms=at)
    c.add_message("weak", sender_person=a, identifier=c.endpoint(a), at_ms=100, basis="push_name")
    assert {r["message_id"] for r in read(c, a)} == {"old", "second", "lid", "merged"}
    assert {r["message_id"] for r in read(c, b)} == {"new"}


def test_bridge_rebuild_hidden_rows_conflicts_and_no_writes(directory_case):
    c = directory_case
    p = c.add_person("Person", ids=(("phone", "4915000000011"),))
    c.add_id(p, "history_contact", str(p))
    c.hdb.execute(
        "INSERT INTO contacts VALUES ('rebuilt','person',NULL,'Archive','confirmed',NULL,'[]')"
    )
    c.hdb.execute(
        "UPDATE identifier_history SET contact_id=? WHERE contact_id=?", ("rebuilt", str(p))
    )
    # Old provenance is absent after a rebuild; exact channel proof rediscovers the new id.
    c.hdb.execute("DELETE FROM contacts WHERE contact_id=?", (str(p),))
    before = c.writer.total_changes, c.hdb.total_changes
    reader = c.reader()
    target = history_person(p, reader)
    assert {r.value for r in target.contact_intervals} == {"rebuilt"}
    assert knowledge_person_ids(p, reader) == ("rebuilt",)
    assert (c.writer.total_changes, c.hdb.total_changes) == before
    c.writer.execute("UPDATE person_ids SET hidden=1 WHERE person_id=? AND kind=?", (p, "phone"))
    assert history_person(p, c.reader()).identifiers == ()


def test_positive_bridge_disagreement_fails_closed_and_counts(directory_case):
    c = directory_case
    p = c.add_person("Person", ids=(("phone", "4915000000011"),))
    c.add_id(p, "history_contact", str(c.counterpart))
    assert history_person(p, c.reader()).identifiers == ()
    assert knowledge_person_ids(p, c.reader()) == ()
    assert c.directory._local.mapping_unavailable >= 1


def test_unknown_time_and_contact_only_authorship_require_unique_unbounded_binding(directory_case):
    c = directory_case
    p = c.add_person("Person", ids=(("phone", "4915000000011"),))
    c.add_id(p, "history_contact", str(p))
    c.add_message("unknown", sender_person=p, identifier=c.endpoint(p), at_ms=None)
    c.add_message("attested", sender_person=p, identifier=None, at_ms=100, basis="owner_attested")
    c.add_message("unattested", sender_person=p, identifier=None, at_ms=100)
    assert {r["message_id"] for r in read(c, p)} == {"unknown", "attested"}
    c.writer.execute("UPDATE person_ids SET valid_from_ms=1 WHERE person_id=?", (p,))
    assert {r["message_id"] for r in read(c, p)} == {"attested"}


def test_explicit_mentions_use_native_time_and_whole_jid_boundaries(directory_case):
    c = directory_case
    p = c.add_person("Unmentioned Name")
    c.add_id(p, "phone", "4915000000011", end=200)
    for mid, at, basis, mention in [
        ("yes", 199, "native", "4915000000011@s.whatsapp.net"),
        ("device",199,"native","4915000000011:2@s.whatsapp.net"),
        ("evil-device",199,"native","4915000000011:2evil@s.whatsapp.net"),
        ("ended", 200, "native", "4915000000011@s.whatsapp.net"),
        ("unknown", None, "unknown", "4915000000011@s.whatsapp.net"),
        ("derived", 199, "capture_time_approx", "4915000000011@s.whatsapp.net"),
        ("evil", 199, "native", "4915000000011@s.whatsapp.net.evil"),
    ]:
        c.add_message(
            mid,
            sender_person=c.owner,
            identifier=c.endpoint(c.owner),
            at_ms=at,
            time_basis=basis,
            mentions=[mention],
        )
    assert {r["message_id"] for r in read(c, p)} == {"yes", "device"}


def test_knowledge_bridge_preserves_all_subjects_and_never_uses_names(directory_case):
    c = directory_case
    p = c.add_person("Same Name", ids=(("phone", "4915000000011"),))
    loser = c.add_person("Alias", merged_into=p)
    c.add_id(p, "knowledge_person", "legacy-a")
    c.add_id(loser, "knowledge_person", "legacy-b", end=200)
    assert knowledge_person_ids(p, c.reader()) == tuple(sorted(("legacy-a", "legacy-b", str(p))))
    q = c.add_person("Same Name")
    assert knowledge_person_ids(q, c.reader()) == ()


def test_retired_history_never_uses_cached_bridge(directory_case):
    c = directory_case
    p = c.add_person("Person", ids=(("phone", "4915000000011"),))
    reader = c.reader()
    history_person(p, reader)
    reader.history.close()
    with pytest.raises(HistoryPaused):
        history_person(p, reader)


def test_bridge_validity_and_conflicting_knowledge_provenance_fail_closed(directory_case):
    c = directory_case
    p = c.add_person("Person", ids=(("phone", "4915000000011"),))
    c.add_id(p, "history_contact", str(p), end=200)
    target = history_person(p, c.reader())
    assert target.identifiers and all(r.valid_until_ms == 200 for r in target.identifiers)


def test_conflicting_knowledge_provenance_fails_closed(directory_case):
    c = directory_case
    p = c.add_person("Person", ids=(("phone", "4915000000011"),))
    c.add_id(p, "knowledge_person", str(c.counterpart))
    assert knowledge_person_ids(p, c.reader()) == ()


def test_search_person_has_no_per_message_identity_queries(directory_case, monkeypatch):
    c = directory_case
    p = c.add_person("Person", ids=(("phone", "4915000000011"),))
    for n in range(10):
        c.add_message(str(n), sender_person=p, identifier=c.endpoint(p), at_ms=100, text="note")
    reader = c.reader()
    target = history_person(p, reader)

    def forbidden(*args, **kwargs):
        raise AssertionError("per-message identity proof")

    monkeypatch.setattr(HistoryQueries, "terminal", forbidden)
    monkeypatch.setattr(HistoryQueries, "resolve_identifier", forbidden)
    statements = []
    reader.history.connection.set_trace_callback(lambda _: statements.append(1))
    rows = HistoryQueries(reader.history).search_person(
        chat_ids=("g@g.us",), person=target, at_ms=300, limit=5, query="note"
    )
    assert len(rows) == 5 and len(statements) <= 12


@pytest.fixture
def large_bridge_case(tmp_path):
    yield from _real_size_directory.__wrapped__(tmp_path)


@pytest.mark.perf
def test_bridge_and_history_search_real_size(large_bridge_case, monkeypatch):
    c = large_bridge_case
    reader = c.reader()
    q = HistoryQueries(reader.history)
    statements = []
    reader.history.connection.set_trace_callback(lambda _: statements.append(1))
    chats = q.owner_member_chat_ids(reader.principal_id, 300)
    count = len(statements)
    reader.history.connection.set_trace_callback(None)
    assert len(chats) == 16
    assert count <= 100
    assert resolve_person('Person0004',reader).person_id == 4
    start = time.perf_counter_ns()
    person = history_person(4, reader)
    cold = (time.perf_counter_ns() - start) / 1e6
    assert cold <= 300

    def forbidden(*args, **kwargs):
        raise AssertionError("per-message identity")

    monkeypatch.setattr(q, "terminal", forbidden)
    monkeypatch.setattr(q, "resolve_identifier", forbidden)
    bridges, searches = [], []
    for _ in range(20):
        start = time.perf_counter_ns()
        assert history_person(4, reader) is person
        bridges.append((time.perf_counter_ns() - start) / 1e6)
        start = time.perf_counter_ns()
        rows = q.search_person(
            chat_ids=("g0@g.us",), person=person, at_ms=300, limit=50, query="note"
        )
        searches.append((time.perf_counter_ns() - start) / 1e6)
        assert len(rows) == 50 and all(r["sender_contact_id"] == "4" for r in rows)
    report = {
        "bridge_cold_ms": cold,
        "bridge_p95_ms": sorted(bridges)[math.ceil(0.95 * len(bridges)) - 1],
        "search_p95_ms": sorted(searches)[math.ceil(0.95 * len(searches)) - 1],
        "permission_sql_count": count,
    }
    print(json.dumps(report), flush=True)
    assert report["bridge_p95_ms"] <= 20
    assert report["search_p95_ms"] <= 300


def test_owner_membership_bulk_proof_keeps_numeric_ambiguity_closed(directory_case):
    c = directory_case
    c.add_person("Other", ids=(("wa_lid", "4915000000099"),))
    c.set_roster("g@g.us", [c.owner])
    q = HistoryQueries(c.snapshot)
    assert q.owner_member_chat_ids(c.endpoint(c.owner), 300) == ()
