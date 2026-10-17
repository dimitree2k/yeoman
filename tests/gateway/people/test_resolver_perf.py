"""Real-size synthetic gate; emits aggregate timings only, runs serially with --run-perf."""

import itertools
import json
import math
import time
from dataclasses import replace

import pytest
from yeoman_gateway.people.disclosure import render_person
from yeoman_gateway.people.resolver import resolve_people_in_text, resolve_person

from .resolver_fixtures import DirectoryCase

# The synthetic fixture loads ~2.4x slower than the live directory (335 vs 142 ms on the Pi,
# 2026-10-17); the real-copy evaluation holds cold paths to 300 ms.
COLD_MS = 500


@pytest.fixture
def real_size_directory(tmp_path):
    c = DirectoryCase(tmp_path)
    c.writer.execute("BEGIN")
    c.hdb.execute("BEGIN")
    for p in (c.owner, c.counterpart, c.assistant):
        c.add_id(p, "wa_lid", str(300000000000 + p))
    for n in range(4, 1510):
        p = c.add_person(
            f"Person{n:04}", ids=(("phone", f"4916000{n:06}"), ("wa_lid", str(300000000000 + n)))
        )
        for alias in range(3):
            c.add_name(p, f"Alias{alias} Person{n:04}")
    count = c.writer.execute("SELECT count(*) FROM person_ids").fetchone()[0]
    c.writer.executemany(
        "INSERT INTO person_ids(person_id,kind,value,source) VALUES (?,'knowledge_person',?,'observed')",
        ((1 + n % 1509, f"synthetic-{n}") for n in range(6400 - count)),
    )
    c.hdb.execute(
        "DELETE FROM identifier_history WHERE id NOT IN (SELECT id FROM identifier_history ORDER BY id LIMIT 2652)"
    )
    c.hdb.executemany(
        "INSERT INTO contacts VALUES (?,'person',NULL,NULL,'confirmed',NULL,'[]')",
        ((f"unmapped-{n}",) for n in range(3703 - 1509)),
    )
    for group in range(16):
        people = list(range(1, 1456)) if group == 0 else list(range(1, 95))
        c.set_roster(f"g{group}@g.us", people, label=f"Synthetic Group {group}")
    c.hdb.execute(
        "UPDATE identifier_history SET contact_id='1500',kind='pn_jid',value=? WHERE id=(SELECT max(id) FROM identifier_history)",
        (c.endpoint(1500),),
    )
    chats = [f"g{n}@g.us" for n in range(16)] + [c.endpoint(p) for p in range(1, 5)]
    rows = []
    for n in range(30365):
        chat = chats[0] if n < 15893 else chats[1 + (n - 15893) % 19]
        p = 1500 if n % 94 == 0 else 1 + n % 94
        endpoint = c.endpoint(p)
        rows.append((f"m{n}", chat, str(p), endpoint, 10 + n % 200))
    c.hdb.executemany(
        "INSERT INTO messages(message_id,channel,chat_id,sender_contact_id,sender_identifier,sender_basis,direction,sent_ms,time_certainty,text,provenance,source_refs) VALUES (?,'whatsapp',?,?,?,'native_identifier','in',?,'native','note','native','[]')",
        rows,
    )
    c.writer.execute("COMMIT")
    c.hdb.execute("COMMIT")
    assert c.writer.execute("SELECT count(*) FROM persons").fetchone()[0] == 1509
    assert c.writer.execute("SELECT count(*) FROM person_ids").fetchone()[0] == 6400
    assert c.hdb.execute("SELECT count(*) FROM contacts").fetchone()[0] == 3703
    assert c.hdb.execute("SELECT count(*) FROM identifier_history").fetchone()[0] == 2652
    assert c.hdb.execute("SELECT count(*) FROM messages").fetchone()[0] == 30365
    assert c.hdb.execute("SELECT count(DISTINCT chat_id) FROM messages").fetchone()[0] == 20
    assert c.hdb.execute(
        "SELECT count(*),count(DISTINCT sender_identifier) FROM messages WHERE chat_id='g0@g.us'"
    ).fetchone() == (15893, 94)
    try:
        yield c
    finally:
        c.close()


def measure(call, repeats=30):
    samples = []
    for _ in range(repeats):
        start = time.perf_counter_ns()
        call()
        samples.append((time.perf_counter_ns() - start) / 1e6)
    return samples


def p95(samples):
    return sorted(samples)[math.ceil(0.95 * len(samples)) - 1]


@pytest.mark.perf
def test_real_size_resolver_latency(real_size_directory):
    c = real_size_directory
    base = c.reader()
    sentence = (
        "Please tell me what you know about Person0004 and their earlier messages in this group"
    )
    assert len(sentence.split()) == 15
    start = time.perf_counter_ns()
    c.directory._index()
    report = {"index_load_ms": (time.perf_counter_ns() - start) / 1e6}
    for kind in ("owner_dm", "group", "other_dm"):
        c.directory._local.state.memo.clear()
        reader = replace(
            base,
            kind=kind,
            chat_id="g0@g.us"
            if kind == "group"
            else c.endpoint(c.owner if kind == "owner_dm" else c.counterpart),
            principal_id="whatsapp:" + c.endpoint(c.owner if kind == "owner_dm" else c.counterpart),
        )
        start = time.perf_counter_ns()
        assert resolve_person("Person0004", reader).person_id == 4
        cold = (time.perf_counter_ns() - start) / 1e6
        report[kind] = {"cold_ms": cold}
        # Fail baseline promptly, before the old per-span implementation can exceed the run budget.
        print(json.dumps(report), flush=True)
        assert cold <= COLD_MS
        for offset in range(1, 4):
            start = time.perf_counter_ns()
            assert (
                resolve_person("Person0004", replace(reader, at_ms=reader.at_ms + offset)).person_id
                == 4
            )
            cold = max(cold, (time.perf_counter_ns() - start) / 1e6)
        report[kind]["cold_ms"] = cold
        assert cold <= COLD_MS
        queries = itertools.cycle(
            ("Person0004", "Person", "Pers", "Missing Synthetic Person", "person:4")
        )
        assert resolve_person("Person1500", reader).person_id == 1500
        warm = measure(lambda: resolve_person(next(queries), reader))
        assert resolve_people_in_text(sentence, reader) == (("Person0004", 4),)
        text = measure(lambda: resolve_people_in_text(sentence, reader), repeats=20)
        assert render_person(4, reader).display_name == "Person0004"
        rendered = measure(lambda: render_person(4, reader))
        report[kind].update(
            resolve_p95_ms=p95(warm), text_max_ms=max(text), render_max_ms=max(rendered)
        )
        print(json.dumps(report), flush=True)
        assert p95(warm) <= 20
        assert max(text) <= 100
        assert max(rendered) <= 20


def sync_service(c, tmp_path):
    from yeoman_gateway.people.history_source import ObservedPerson
    from yeoman_gateway.people.identifiers import ObservedId
    from yeoman_gateway.people.service import PeopleSyncService

    observed = ObservedPerson(
        "4",
        ("4",),
        (ObservedId("phone", "4916000000004"), ObservedId("wa_lid", "300000000004")),
        (),
        False,
        "contact",
        300,
    )
    service = PeopleSyncService(
        projector=None,
        db_path=c.path,
        people=c.directory,
        backup_dir=tmp_path / "backups",
        clock=lambda: 1700000000000,
    )
    assert service._apply([observed]) is not None
    c.directory.refresh()
    return service, observed


@pytest.mark.perf
def test_resolver_after_noop_sync_p95(real_size_directory, tmp_path):
    c = real_size_directory
    reader = c.reader()
    service, observed = sync_service(c, tmp_path)
    index = c.directory._index()
    samples = []
    for n in range(20):
        report = service._apply([replace(observed, last_seen_ms=400 + n)])
        assert report is not None and not any(report.as_dict().values())
        start = time.perf_counter_ns()
        assert resolve_person("Person0004", reader).person_id == 4
        samples.append((time.perf_counter_ns() - start) / 1e6)
        assert c.directory._index() is index
    report = {"noop_sync_p95_ms": p95(samples)}
    print(json.dumps(report), flush=True)
    assert report["noop_sync_p95_ms"] <= 20


@pytest.mark.perf
def test_resolver_after_real_sync_refresh_p95(real_size_directory, tmp_path):
    c = real_size_directory
    reader = c.reader()
    service, observed = sync_service(c, tmp_path)
    samples = []
    for n in range(20):
        alias = f"FreshSyncAlias{n:04}"
        report = service._apply([replace(observed, names=((alias, "push_name"),))])
        assert report is not None and report.names_added == 1
        c.directory.refresh()
        start = time.perf_counter_ns()
        assert resolve_person(alias, reader).person_id == 4
        samples.append((time.perf_counter_ns() - start) / 1e6)
    report = {"refreshed_sync_p95_ms": p95(samples)}
    print(json.dumps(report), flush=True)
    assert report["refreshed_sync_p95_ms"] <= 20


@pytest.mark.perf
def test_resolver_manual_owner_edit_next_call(real_size_directory):
    c = real_size_directory
    reader = c.reader()
    assert resolve_person("Person0004", reader).person_id == 4
    samples = []
    for n in range(20):
        name = f"Edited Person{n:04}"
        c.writer.execute("UPDATE persons SET preferred_name=? WHERE id=4", (name,))
        start = time.perf_counter_ns()
        assert resolve_person(name, reader).person_id == 4
        samples.append((time.perf_counter_ns() - start) / 1e6)
    report = {"manual_edit_next_call_max_ms": max(samples), "manual_edit_p95_ms": p95(samples)}
    print(json.dumps(report), flush=True)
    assert max(samples) <= COLD_MS
