"""Reconnect identity components and reservation-tail admission (synthetic only)."""
import json
import sqlite3
from contextlib import closing
from time import perf_counter

import pytest
from hist_fixtures import T0, _bf, _raw, write_jsonl
from test_hist_incremental import CrashConnection, EngineFixture, observation
from yeoman_gateway.history.incremental import ProjectionIndex, apply_committed
from yeoman_gateway.history.layer1 import canonical_json
from yeoman_gateway.history.project import project
from yeoman_gateway.history.verify import table_digest
from yeoman_shared.raw_archive.records import append_line, enumerate_committed


def membership(number, *, new=False, ms=T0):
    participants = [{'lid': f'{980000 + i}@lid', 'phoneJid': f'{4915552000000 + i}@s.whatsapp.net'}
                    for i in range(number * 10, number * 10 + 120)]
    if new:
        participants += [{'lid': f'{990000 + number}@lid', 'phoneJid': f'{4915559000000 + number}@s.whatsapp.net'}]
    return _raw('membership_snapshot', 'membership_snapshot',
                {'chatJid': f'reconnect-{number}@g.us', 'participants': participants, 'complete': True,
                 'timestamp': ms}, received=ms)


def burst(new=False):
    rows = [membership(i, new=new and i < 3, ms=T0 + 60000) for i in range(18)]
    rows += [_raw('group_subject', 'group_subject', {'chatJid': f'reconnect-{i}@g.us',
              'subject': f'Synthetic group {i}', 'snapshot': True, 'timestamp': T0 + 60000}, received=T0 + 60000)
             for i in range(18)]
    rows += [_raw('group_description', 'group_description', {'chatJid': f'reconnect-{i}@g.us',
              'description': f'Synthetic description {i}', 'snapshot': True, 'timestamp': T0 + 60000}, received=T0 + 60000)
             for i in range(5)]
    rows += [observation(native_id=f'RECONNECT-{i}', sender='4915552000000@s.whatsapp.net',
                         chat='reconnect-0@g.us', ms=T0 + 60000 + i) for i in range(2)]
    assert len(rows) == 43
    return rows


def legacy_numerics():
    rows = []
    for i in range(0, 80, 2):
        number = str(4915552000000 + i)
        rows.append(_bf('journal', 'message', {'messageId': f'LEGACY-{i}', 'senderId': number,
                          'text': f'Legacy numeric {i}'}, chat='reconnect-0@g.us', ms=T0 - 100000))
        rows.append(_bf('journal', 'reaction', {'targetMessageId': f'LEGACY-{i}', 'senderId': number,
                          'emoji': 'synthetic'}, chat='reconnect-0@g.us', ms=T0 - 90000))
    return rows


def numeric_pair_burst(new=False):
    rows = burst(new)
    if new:
        for i in range(3):
            rows[i]['native']['payload']['participants'].append(
                {'lid': f'{995000 + i}@lid', 'phoneJid': f'{4915552000000 + i * 2}@s.whatsapp.net'})
    return rows


AMBIGUOUS_LID = '997777@lid'
AMBIGUOUS_PHONE = '4915559777777@s.whatsapp.net'


def double_bound_knowledge(*, windowed=False):
    rows = []
    for i in range(2):
        cid = f'00000000-0000-4000-8000-00000000000{i + 1}'
        rows.append(_bf('knowledge', 'contact_record', {'contactRef': cid, 'createdMs': 1}, channel='any'))
        binding = {'contactRef': cid, 'identifier': AMBIGUOUS_LID}
        rows.append(_bf('knowledge', 'identifier_record', binding))
        if windowed and i == 0:
            rows.append(_bf('knowledge', 'identifier_record', {'contactRef': cid,
                        'identifier': AMBIGUOUS_PHONE, 'validFromMs': T0 - 1}))
    return rows


def with_double_bound_member(rows):
    for row in rows[:14]:
        row['native']['payload']['participants'].append({'lid': AMBIGUOUS_LID, 'phoneJid': AMBIGUOUS_PHONE})
    return rows


@pytest.mark.parametrize('windowed', [False, True])
def test_reconnect_restated_double_bound_known_link_metadata(tmp_path, monkeypatch, windowed):
    import yeoman_gateway.history.incremental as incremental

    e = EngineFixture(tmp_path)
    try:
        write_jsonl(e.root / 'backfill/knowledge.jsonl', double_bound_knowledge(windowed=windowed))
        for row in with_double_bound_member([membership(i) for i in range(18)]):
            e.append(row)
        e.append(observation(native_id='AMBIGUOUS', sender=AMBIGUOUS_LID))
        e.append(observation(native_id='PHONE', sender=AMBIGUOUS_PHONE))
        e.conn.close()
        project([e.root], e.db, publish_lineage_root=e.root)
        e.conn = sqlite3.connect(e.db)
        e.conn.execute('PRAGMA foreign_keys=ON')
        e.restart()
        bindings = e.index._resolution._identifier_index[('lid', AMBIGUOUS_LID)]
        assert len(bindings) == 2 and len({row.contact_id for row in bindings}) == 2
        assert all(row.evidence == 'knowledge_binding' for row in bindings)
        refs = {row.contact_id: row.source_refs for row in bindings}
        phone_refs = e.index._resolution._identifier_index[('pn_jid', AMBIGUOUS_PHONE)][0].source_refs
        contacts = {row.contact_id: row.source_refs for row in e.index._resolution.contacts}
        original, calls = incremental.resolve, []
        def measured(inp):
            calls.append(len(inp.sightings))
            return original(inp)
        monkeypatch.setattr(incremental, 'resolve', measured)
        for n in range(2):
            ms = T0 + (60000 if n == 0 else 30000)  # Also preserve resolver review order for a delayed burst.
            for row in with_double_bound_member([membership(i, ms=ms) for i in range(18)]):
                e.append(row)
            if not windowed and n == 0:
                before, before_review = e.state(), canonical_json(e.index._resolution.review)
                with pytest.raises(RuntimeError, match='crash'):
                    apply_committed(CrashConnection(e.conn, 'before_commit'), e.index, e.root, e.index.target(e.root))
                assert e.state() == before
                assert canonical_json(e.index._resolution.review) == before_review
            e.apply()  # RebuildRequired is a failure: these are known edges.
            assert len(calls) == (n + 1 if windowed else 0)
            bindings = e.index._resolution._identifier_index[('lid', AMBIGUOUS_LID)]
            assert all(row.last_seen_ms == T0 + 60000 and row.source_refs == refs[row.contact_id] for row in bindings)
            phone = e.index._resolution._identifier_index[('pn_jid', AMBIGUOUS_PHONE)][0]
            assert phone.last_seen_ms == T0 + 60000 and phone.source_refs == phone_refs
            assert all(row.source_refs == contacts[row.contact_id] for row in e.index._resolution.contacts
                       if row.contact_id in refs or row.contact_id == phone.contact_id)
            e.parity()
        review = e.index._resolution.review
        e.restart()
        for category in ('temporal_links_ambiguous', 'blocked_by_unmerge'):
            assert e.index._resolution.review[category] == review[category]
        e.parity()
    finally:
        e.close()


def assert_parity(root, db, oracle):
    project([root], oracle)
    assert table_digest(db) == table_digest(oracle)
    assert len(table_digest(db)) == 4
    with closing(sqlite3.connect(db)) as a, closing(sqlite3.connect(oracle)) as b:
        assert a.execute('SELECT * FROM messages_current ORDER BY message_id').fetchall() == b.execute(
            'SELECT * FROM messages_current ORDER BY message_id').fetchall()
        assert not a.execute('PRAGMA foreign_key_check').fetchall()


def test_reconnect_new_silent_members_resolve_only_local_components(tmp_path, monkeypatch):
    import yeoman_gateway.history.incremental as incremental

    e = EngineFixture(tmp_path)
    try:
        e.append(membership(0))
        e.apply()
        sizes = []
        original = incremental.resolve
        def measured(inp):
            sizes.append(len(inp.sightings))
            assert len(inp.sightings) <= 2
            return original(inp)
        monkeypatch.setattr(incremental, 'resolve', measured)
        e.append(membership(0, new=True, ms=T0 + 60000))
        e.apply()
        assert sizes == [2]
        e.append(membership(0, new=True, ms=T0 + 120000))
        e.apply()
        assert sizes == [2]  # Known snapshot never resolves again.
        e.parity()
    finally:
        e.close()


def test_reconnect_lineage_publication_keeps_original_native_target(tmp_path, monkeypatch):
    e = EngineFixture(tmp_path)
    try:
        target = e.append(observation(native_id='NEW', sender='4915559000001@s.whatsapp.net'))
        original = e.index.plan
        def arriving(lines):
            delta = original(lines)
            append_line(e.root / 'whatsapp/2026-01.jsonl', canonical_json(observation(native_id='TAIL')))
            return delta
        monkeypatch.setattr(e.index, 'plan', arriving)
        e.apply(target)
        assert e.conn.execute("SELECT native_message_id FROM messages WHERE native_message_id IN ('NEW','TAIL')").fetchall() == [('NEW',)]
        assert next(b for b in e.boundaries() if b.relative_path.startswith('whatsapp/')) == next(
            b for b in target if b.relative_path.startswith('whatsapp/'))
        monkeypatch.setattr(e.index, 'plan', original)
        e.apply(e.index.target(e.root))
        e.parity()
    finally:
        e.close()


@pytest.mark.perf
@pytest.mark.parametrize('new', [False, True])
@pytest.mark.parametrize('per_line', [False, True])
def test_reconnect_burst_cost_at_30k(tmp_path, monkeypatch, new, per_line):
    import yeoman_gateway.history.incremental as incremental
    root, db = tmp_path / 'raw', tmp_path / 'history.db'
    rows = [observation(native_id=f'M{i}', sender=f'{4915554000000 + i % 1500}@s.whatsapp.net',
                        chat='large@g.us' if i < 10000 else f'chat-{i % 200}@g.us',
                        text=f'Synthetic body {i}', ms=T0 + i) for i in range(30000)]
    rows += with_double_bound_member([membership(i) for i in range(18)])
    path = write_jsonl(root / 'whatsapp/2026-01.jsonl', rows)
    write_jsonl(root / 'backfill/journal.jsonl', legacy_numerics())
    write_jsonl(root / 'backfill/knowledge.jsonl', double_bound_knowledge())
    project([root], db, publish_lineage_root=root)
    with closing(sqlite3.connect(db)) as conn:
        conn.execute('PRAGMA foreign_keys=ON')
        index = ProjectionIndex.from_prefix(root, enumerate_committed(root))
        samples, resolved = [], []
        original = incremental.resolve
        def measured(inp):
            resolved.append(len(inp.sightings))
            assert len(inp.sightings) <= 18
            return original(inp)
        monkeypatch.setattr(incremental, 'resolve', measured)
        for row in with_double_bound_member(numeric_pair_burst(new)):
            append_line(path, canonical_json(row))
            if per_line:
                target = index.target(root)
                started = perf_counter()
                apply_committed(conn, index, root, target)
                samples.append(perf_counter() - started)
        if not per_line:
            target = index.target(root)
            started = perf_counter()
            apply_committed(conn, index, root, target)
            samples.append(perf_counter() - started)
        print(f'reconnect new={new} per_line={per_line} apply_seconds={samples}', flush=True)
        assert max(samples) <= (0.5 if per_line else 1.0)
        assert len(resolved) == ((3 if per_line else 1) if new else 0)
    assert_parity(root, db, tmp_path / 'oracle.db')


def test_reconnect_known_snapshot_writes_only_changed_identifier_rows(tmp_path, monkeypatch):
    import yeoman_gateway.history.incremental as incremental
    import yeoman_gateway.history.lineage as lineage

    e = EngineFixture(tmp_path)
    try:
        e.append(membership(0))
        e.apply()
        # Same time changes pair source refs, but not the unrelated seeded contact's rows.
        original = e.index._resolution
        def forbidden(*args, **kwargs):
            pytest.fail('known membership resolved or revalidated cached lineage')
        monkeypatch.setattr(incremental, 'resolve', forbidden)
        monkeypatch.setattr(lineage, '_validate_contact_id_record', forbidden)
        e.append(membership(0))
        statements = []
        e.conn.set_trace_callback(statements.append)
        e.apply()
        assert e.index._resolution is original
        assert not any(sql.startswith('DELETE FROM identifier_history WHERE contact_id=') and ' AND channel=' not in sql
                       for sql in statements)
        assert not any('INSERT INTO identifier_history' in sql and '4915550000001@s.whatsapp.net' in sql
                       for sql in statements)
        e.parity()
    finally:
        e.close()


def test_reconnect_numeric_group_dependency_uses_global_guard(tmp_path):
    e = EngineFixture(tmp_path)
    try:
        e.append(observation(native_id='GROUP', chat='998877@g.us'))
        e.apply()
        e.append(observation(native_id='NUMERIC', sender='998877'))
        e.apply()
        e.parity()
        assert e.conn.execute("SELECT sender_contact_id FROM messages WHERE native_message_id='NUMERIC'").fetchone() == (None,)
    finally:
        e.close()


@pytest.mark.asyncio
async def test_reconnect_live_arriving_tail_never_fences(tmp_path, monkeypatch):
    from history_live_benchmark import Measurements
    from test_hist_live import oracle_parity, start_ready
    from yeoman_gateway.history import live
    from yeoman_shared.raw_archive.writer import RawArchive

    root, db = tmp_path / 'raw', tmp_path / 'history.db'
    write_jsonl(root / 'whatsapp/2026-10.jsonl', [observation(native_id='SEED')])
    project([root], db, publish_lineage_root=root)
    archive = RawArchive(root, spool=tmp_path / 'spool', status_path=tmp_path / 'status.json')
    p = live.HistoryProjector(root, db, archive)
    samples = Measurements()
    samples._original_apply = live.apply_committed
    monkeypatch.setattr(live, 'apply_committed', samples.apply_committed)
    await start_ready(p)
    original, arrived = p._index.plan, False
    def arriving_tail(lines):
        nonlocal arrived
        delta = original(lines)
        if not arrived:
            arrived = True
            append_line(root / 'whatsapp/2026-10.jsonl', canonical_json(observation(native_id='TAIL')))
        return delta
    monkeypatch.setattr(p._index, 'plan', arriving_tail)
    append_line(root / 'whatsapp/2026-10.jsonl', canonical_json(membership(0, new=True)))
    try:
        await p.barrier()
        assert not samples.rebuilds and samples.apply
        await oracle_parity(p, root, tmp_path)
    finally:
        await p.stop()


def test_reconnect_external_lineage_during_plan_still_requires_fence(tmp_path, monkeypatch):
    from yeoman_gateway.history.incremental import RebuildRequired

    e = EngineFixture(tmp_path)
    try:
        original = e.index.plan
        def external(lines):
            delta = original(lines)
            path = e.root / 'derived/contact-ids.jsonl'
            record = json.loads(path.read_text().splitlines()[0])
            append_line(path, canonical_json(record))
            return delta
        monkeypatch.setattr(e.index, 'plan', external)
        target = e.append(observation(native_id='NEW', sender='4915559000001@s.whatsapp.net'))
        before = e.state()
        with pytest.raises(RebuildRequired, match='external contact ID lineage'):
            e.apply(target)
        assert e.state() == before
    finally:
        e.close()


def test_reconnect_new_pair_attaches_locally_without_changing_existing_owner(tmp_path, monkeypatch):
    import yeoman_gateway.history.incremental as incremental

    e = EngineFixture(tmp_path)
    try:
        phone = '4915559000099@s.whatsapp.net'
        e.append(observation(native_id='SILENT-PHONE', sender=phone))
        e.apply()
        old_id = e.conn.execute("SELECT sender_contact_id FROM messages WHERE native_message_id='SILENT-PHONE'").fetchone()[0]
        original, sizes = incremental.resolve, []
        def measured(inp):
            sizes.append(len(inp.sightings))
            assert len(inp.sightings) == 2
            return original(inp)
        monkeypatch.setattr(incremental, 'resolve', measured)
        row = membership(0, ms=T0 + 60000)
        row['native']['payload']['participants'] = [{'lid': '991199@lid', 'phoneJid': phone}]
        e.append(row)
        e.apply()
        assert sizes == [2]
        assert e.index._resolution.resolve(incremental.classify('991199@lid'))[0] == old_id
        e.parity()
    finally:
        e.close()


def test_reconnect_matching_external_reservation_during_publish_requires_fence(tmp_path, monkeypatch):
    import yeoman_gateway.history.incremental as incremental

    e = EngineFixture(tmp_path)
    try:
        original = incremental.publish_lineage
        def external(root, generated, **kwargs):
            original(root, generated, first_published_ms=1)
            return original(root, generated, **kwargs)
        monkeypatch.setattr(incremental, 'publish_lineage', external)
        target = e.append(observation(native_id='NEW', sender='4915559000001@s.whatsapp.net'))
        before = e.state()
        with pytest.raises(incremental.RebuildRequired, match='external contact ID lineage'):
            e.apply(target)
        assert e.state() == before
    finally:
        e.close()


def test_reconnect_numeric_edges_resolve_locally_and_preserve_legacy_attribution(tmp_path, monkeypatch):
    import yeoman_gateway.history.incremental as incremental

    e = EngineFixture(tmp_path)
    try:
        for row in burst():
            e.append(row)
        for row in legacy_numerics():
            e.append(row, rel='backfill/journal.jsonl')
        e.conn.close()
        project([e.root], e.db, publish_lineage_root=e.root)
        e.conn = sqlite3.connect(e.db)
        e.conn.execute('PRAGMA foreign_keys=ON')
        e.restart()
        before = e.conn.execute("SELECT message_id, sender_contact_id FROM messages WHERE native_message_id LIKE 'LEGACY-%' ORDER BY message_id").fetchall()
        assert before and all(cid for _, cid in before)
        old_events = e.conn.execute("SELECT event_id, actor_contact_id FROM message_events WHERE kind='reaction' ORDER BY event_id").fetchall()
        original, sizes = incremental.resolve, []
        def measured(inp):
            sizes.append(len(inp.sightings))
            assert len(inp.sightings) <= 18  # Three numeric components plus three independent pairs.
            return original(inp)
        monkeypatch.setattr(incremental, 'resolve', measured)
        for row in numeric_pair_burst(True):
            e.append(row)
        e.apply()
        assert sizes and len(sizes) == 1
        assert e.conn.execute("SELECT message_id, sender_contact_id FROM messages WHERE native_message_id LIKE 'LEGACY-%' ORDER BY message_id").fetchall() == before
        assert e.conn.execute("SELECT event_id, actor_contact_id FROM message_events WHERE kind='reaction' ORDER BY event_id").fetchall() == old_events
        e.parity()
        e.restart()
        for row in numeric_pair_burst(True):
            e.append(row)
        e.apply()
        assert len(sizes) == 1
        e.parity()
    finally:
        e.close()


def test_reconnect_known_sighting_metadata_uses_single_row_updates(tmp_path):
    e = EngineFixture(tmp_path)
    try:
        e.append(membership(0))
        e.apply()
        before = e.conn.execute('SELECT id, contact_id, kind, value FROM identifier_history ORDER BY id').fetchall()
        e.append(membership(0, ms=T0 + 60000))
        statements = []
        e.conn.set_trace_callback(statements.append)
        e.apply()
        after = e.conn.execute('SELECT id, contact_id, kind, value FROM identifier_history ORDER BY id').fetchall()
        assert after == before
        assert not any(sql.startswith(('DELETE FROM identifier_history', 'INSERT INTO identifier_history')) for sql in statements)
        updates = [sql for sql in statements if sql.startswith('UPDATE identifier_history')]
        assert len(updates) == 240
        print(f'known snapshot writes: identifier_updates={len(updates)} identifier_delete_insert=0', flush=True)
        e.parity()
    finally:
        e.close()


def test_reconnect_numeric_ambiguity_between_existing_owners_uses_global_path(tmp_path, monkeypatch):
    import yeoman_gateway.history.incremental as incremental

    e = EngineFixture(tmp_path)
    try:
        number = '4915552000099'
        e.append(observation(native_id='PN', sender=number + '@s.whatsapp.net'))
        e.append(observation(native_id='LID', sender=number + '@lid'))
        e.append(observation(native_id='NUMERIC', sender=number))
        e.apply()
        e.parity()
        owners = [row[0] for row in e.conn.execute("SELECT sender_contact_id FROM messages WHERE native_message_id IN ('PN','LID')")]
        assert len(set(owners)) == 2
        original, calls = incremental.resolve, []
        def measured(inp):
            calls.append(inp)
            assert incremental.classify('4915550000001@s.whatsapp.net') in inp.sightings  # Unrelated seed proves global input.
            return original(inp)
        monkeypatch.setattr(incremental, 'resolve', measured)
        row = membership(0, ms=T0 + 60000)
        row['native']['payload']['participants'] = [{'lid': '997799@lid', 'phoneJid': number + '@s.whatsapp.net'}]
        e.append(row)
        e.apply()
        assert len(calls) == 1
        e.parity()
    finally:
        e.close()
