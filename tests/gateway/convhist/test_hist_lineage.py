"""Published synthetic IDs survive loss of the DB, without inventing ownership."""
import hashlib
import json
import os
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager
from pathlib import Path

import pytest
from hist_fixtures import _bf, _raw
from yeoman_gateway.history.attestations import make
from yeoman_gateway.history.extract import extract
from yeoman_gateway.history.incremental import ProjectionIndex, RebuildRequired, apply_committed
from yeoman_gateway.history.layer1 import canonical_json, iter_layer1
from yeoman_gateway.history.project import build_rows, project
from yeoman_gateway.history.verify import table_digest, verify
from yeoman_shared.raw_archive import records
from yeoman_shared.raw_archive.purge import PurgeSelector, purge

A, P, L, B = ('491100000001@s.whatsapp.net', '491100000003@s.whatsapp.net',
              '777000000001@lid', '491100000002@s.whatsapp.net')


class Witness:
    def __init__(self, base):
        self.root, self.db = base / 'raw', base / 'history.db'
        self.clock = 0

    def owner(self, type_, **fields):
        self.clock += 1
        return records.append_owner_record(self.root, make(type_, at_ms=self.clock, note='synthetic', **fields))

    def append(self, record, relative='whatsapp/2026-10.jsonl'):
        if record.get('archive_version') == 1:
            record['chat_id'] = 'synthetic@g.us'
        records.append_line(self.root / relative, canonical_json(record),
                            coordination_lock=self.root / records.PURGE_DISPOSITION_LOCK)

    def publish_initial_assertions_and_pairs(self, bounds, pair_times):
        for start, end in bounds:
            self.append_same_owner_assertion((start, end))
        for ms in pair_times:
            self.append(_raw('message', 'message', {'messageId': f'm{ms}', 'chatJid': 'synthetic@g.us',
                        'participantJid': L, 'senderPhoneJid': P, 'providerTimestampMs': ms, 'text': 'synthetic'}))
        project([self.root], self.db, publish_lineage_root=self.root)

    def publish_binding_and_end(self, bounds, ended_ms):
        self.append_same_owner_assertion(bounds)
        self.owner('identifier_ended', identifier=P, ended_ms=ended_ms)
        project([self.root], self.db, publish_lineage_root=self.root)

    def append_same_owner_assertion(self, bounds):
        return self.owner('identifier', anchor=A, identifier=P, valid_from_ms=bounds[0], valid_until_ms=bounds[1])

    def lineage(self):
        path = self.root / 'derived/contact-ids.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()]

    def published_generated_id(self, value, bounds):
        rows = [r for r in self.lineage() if r['value'] == value
                and (r['valid_from_ms'], r['valid_until_ms']) == bounds]
        assert len(rows) == 1
        old = rows[0]['contact_id']
        assert self.contact_row(old) is not None
        return old

    def contact_row(self, ident):
        with closing(sqlite3.connect(self.db)) as conn:
            return conn.execute('SELECT * FROM contacts WHERE contact_id=?', (ident,)).fetchone()

    def terminal(self, ident):
        with closing(sqlite3.connect(self.db)) as conn:
            seen = set()
            while True:
                assert ident not in seen
                seen.add(ident)
                row = conn.execute('SELECT merged_into FROM contacts WHERE contact_id=?', (ident,)).fetchone()
                if row is None or row[0] is None:
                    return ident
                ident = row[0]

    def owner_id(self, value):
        with closing(sqlite3.connect(self.db)) as conn:
            return conn.execute('SELECT contact_id FROM identifier_history WHERE value=?', (value,)).fetchone()[0]

    def rebuild_into_fresh_database_without_previous_db(self):
        self.db.unlink()
        self.report = project([self.root], self.db, publish_lineage_root=self.root)
        assert verify([self.root], self.db, scratch=None)['review'] == self.report['review']

    def assert_native_ownership_edges_unchanged_by_lineage(self):
        ex = extract(iter_layer1([self.root]))
        expected = build_rows(ex).resolution.identifiers
        ex.contact_id_records = []
        assert build_rows(ex).resolution.identifiers == expected


@pytest.fixture
def witness(tmp_path):
    return Witness(tmp_path)


def test_contact_id_lineage_combined_cut_witness_a(witness):
    witness.publish_initial_assertions_and_pairs(bounds=[(100, 300), (200, 400)], pair_times=[125, 175, 250, 350])
    old_id = witness.published_generated_id(value=L, bounds=(100, 200))
    assert any(r['contact_id'] == old_id for r in witness.lineage())
    witness.append_same_owner_assertion(bounds=(150, 200))
    witness.rebuild_into_fresh_database_without_previous_db()
    assert witness.contact_row(old_id) is not None
    assert witness.terminal(old_id) == witness.terminal(witness.owner_id(A))
    witness.assert_native_ownership_edges_unchanged_by_lineage()


def test_contact_id_lineage_shorthand_witness_b(witness):
    witness.publish_binding_and_end(bounds=(100, 300), ended_ms=200)
    old_id = witness.published_generated_id(value=P, bounds=(100, 200))
    witness.append_same_owner_assertion(bounds=(150, 300))
    witness.rebuild_into_fresh_database_without_previous_db()
    assert witness.report['review']['identifier_ended_not_applied'][0]['resolution'] == 'multiple'
    assert witness.contact_row(old_id) is not None
    assert witness.terminal(old_id) == witness.terminal(witness.owner_id(A))
    with closing(sqlite3.connect(witness.db)) as conn:
        assert [r[0] for r in conn.execute('SELECT valid_until_ms FROM identifier_history WHERE value=? ORDER BY valid_from_ms', (P,))] == [300, 300]


@pytest.mark.parametrize('case', ['a', 'b'])
def test_contact_id_lineage_multi_owner_is_reviewed(witness, case):
    if case == 'a':
        witness.publish_initial_assertions_and_pairs(bounds=[(100, 300), (200, 400)], pair_times=[125, 175, 250, 350])
        old = witness.published_generated_id(L, (100, 200))
    else:
        witness.publish_binding_and_end((100, 300), 200)
        old = witness.published_generated_id(P, (100, 200))
    witness.owner('identifier_ended', identifier=P, ended_ms=150)
    witness.owner('identifier', anchor=B, identifier=P, valid_from_ms=150, valid_until_ms=200)
    witness.rebuild_into_fresh_database_without_previous_db()
    assert witness.terminal(old) not in {witness.terminal(witness.owner_id(A)), witness.terminal(witness.owner_id(B))}
    assert any(r['contact_id'] == old for r in witness.report['review']['contact_id_lineage'])
    assert witness.terminal(witness.owner_id(A)) != witness.terminal(witness.owner_id(B))
    witness.assert_native_ownership_edges_unchanged_by_lineage()


def test_lineage_fsync_failure_and_restart_before_database_commit(witness, monkeypatch):
    import yeoman_gateway.history.project as projector

    witness.append(_raw('message', 'message', {'messageId': 'first', 'senderId': L, 'text': 'synthetic'}))
    original_fsync, original_write = os.fsync, projector._write
    order = []
    synced = []

    def fail_lineage(fd):
        if str(os.readlink(f'/proc/self/fd/{fd}')).endswith('contact-ids.jsonl'):
            raise OSError('synthetic fsync failure')
        original_fsync(fd)

    monkeypatch.setattr(os, 'fsync', fail_lineage)
    with pytest.raises(OSError):
        project([witness.root], witness.db, publish_lineage_root=witness.root)
    assert not witness.db.exists()
    def tracked_fsync(fd):
        original_fsync(fd)
        if str(os.readlink(f'/proc/self/fd/{fd}')).endswith('contact-ids.jsonl'):
            synced.append('lineage_file')

    monkeypatch.setattr(os, 'fsync', tracked_fsync)

    def crash_before_commit(*args, **kwargs):
        assert witness.lineage() and synced
        order.append('durable_lineage')
        raise RuntimeError('synthetic crash before DB commit')

    monkeypatch.setattr(projector, '_write', crash_before_commit)
    with pytest.raises(RuntimeError):
        project([witness.root], witness.db, publish_lineage_root=witness.root)
    assert not witness.db.exists()
    reserved = witness.lineage()
    assert len({r['contact_id'] for r in reserved}) == len(reserved) == 1
    monkeypatch.setattr(projector, '_write', original_write)
    project([witness.root], witness.db, publish_lineage_root=witness.root)
    assert witness.lineage() == reserved and order == ['durable_lineage']
    assert witness.contact_row(reserved[0]['contact_id'])
    project([witness.root], witness.db, publish_lineage_root=witness.root)
    assert witness.lineage() == reserved


def test_lineage_never_overwrites_existing_contact_or_purged_ref(witness, tmp_path):
    witness.publish_binding_and_end((100, 300), 200)
    old = witness.published_generated_id(P, (100, 200))
    witness.append(_bf('knowledge', 'contact_record', {'contactRef': old, 'displayName': 'synthetic retained'},
                       channel='any', ms=None, certainty='unknown'), 'backfill/knowledge.jsonl')
    witness.append(_bf('knowledge', 'identifier_record', {'contactRef': old, 'identifier': B}, ms=None,
                       certainty='unknown'), 'backfill/knowledge.jsonl')
    witness.append_same_owner_assertion((150, 300))
    witness.rebuild_into_fresh_database_without_previous_db()
    assert witness.terminal(old) == old
    assert 'synthetic retained' in witness.contact_row(old)
    assert any(r['contact_id'] == old and r['reason'] == 'collision' for r in witness.report['review']['contact_id_lineage'])
    other = Witness(tmp_path / 'purge')
    other.append(_raw('message', 'message', {'messageId': 'purged', 'senderId': L, 'text': 'synthetic'}, received=10))
    project([other.root], other.db, publish_lineage_root=other.root)
    reserved = other.lineage()[0]
    purge(other.root, PurgeSelector('whatsapp', chat_id='synthetic@g.us', native_id='purged'), operator='synthetic', now_ms=100)
    other.rebuild_into_fresh_database_without_previous_db()
    assert other.contact_row(reserved['contact_id']) is None
    assert other.report['outcomes']['derived/contact-ids.jsonl'] == {'skipped:purged': 1}
    from yeoman_gateway.history.lineage import publish_lineage
    from yeoman_gateway.history.resolve import GeneratedContactId
    generated = GeneratedContactId(*(reserved[k] for k in ('contact_id', 'seed', 'value', 'valid_from_ms', 'valid_until_ms')), tuple(reserved['source_refs']))
    with pytest.raises(ValueError, match='disposed'):
        publish_lineage(other.root, [generated], first_published_ms=200)
    other.append(_raw('message', 'message', {'messageId': 'fresh', 'senderId': L, 'text': 'synthetic'}, received=300))
    fresh = build_rows(extract(iter_layer1([other.root]))).resolution.generated_ids
    with pytest.raises(ValueError, match='disposed'):
        publish_lineage(other.root, fresh, first_published_ms=300)
    assert other.contact_row(reserved['contact_id']) is None


def test_lineage_incremental_publication_and_external_reservation(witness, monkeypatch):
    witness.append(_raw('message', 'message', {'messageId': 'initial', 'senderId': A}))
    project([witness.root], witness.db, publish_lineage_root=witness.root)
    index = ProjectionIndex.from_prefix(witness.root, records.enumerate_committed(witness.root))
    witness.append(_raw('message', 'message', {'messageId': 'new', 'senderId': L}))
    with closing(sqlite3.connect(witness.db)) as conn:
        conn.execute('PRAGMA foreign_keys=ON')
        apply_committed(conn, index, witness.root, index.target(witness.root))
        assert conn.execute('SELECT count(*) FROM contacts').fetchone()[0] == 2
        assert len(witness.lineage()) == 2
        assert records.enumerate_committed(witness.root) == index._boundaries
        oracle = witness.db.with_name('oracle.db')
        project([witness.root], oracle)
        assert table_digest(witness.db) == table_digest(oracle)
        record = dict(witness.lineage()[0])
        # An externally reserved alias is never silently skipped by a fast identity plan.
        record['source_refs'] = []
        records.append_line(witness.root / 'derived/contact-ids.jsonl', canonical_json(record),
                            coordination_lock=witness.root / records.PURGE_DISPOSITION_LOCK)
        before = conn.execute('SELECT * FROM projector_state').fetchall()
        with pytest.raises(RebuildRequired):
            apply_committed(conn, index, witness.root, index.target(witness.root))
        assert conn.execute('SELECT * FROM projector_state').fetchall() == before


def test_lineage_full_window_gap_and_invalid_health(witness):
    witness.append_same_owner_assertion((100, 300))
    project([witness.root], witness.db, publish_lineage_root=witness.root)
    old = witness.published_generated_id(P, (100, 300))
    witness.owner('identifier_ended', identifier=P, ended_ms=200)
    witness.rebuild_into_fresh_database_without_previous_db()
    # Native inference remains accepted; an incomplete issued window adds review only.
    witness.assert_native_ownership_edges_unchanged_by_lineage()
    assert any(r['contact_id'] == old and r['reason'] == 'gap' for r in witness.report['review']['contact_id_lineage'])
    assert all(not row.get('text') and not row.get('display_name') for row in witness.lineage())
    before = witness.db.read_bytes()
    witness.append({'kind': 'contact_id', 'contact_id': old}, 'derived/contact-ids.jsonl')
    ex = extract(iter_layer1([witness.root]))
    assert ex.outcomes[('derived/contact-ids.jsonl', 'skipped:invalid_contact_id_lineage')] == 1
    assert ex.review['lineage_health']
    with pytest.raises(ValueError, match='lineage'):
        project([witness.root], witness.db, publish_lineage_root=witness.root)
    assert witness.db.read_bytes() == before


@pytest.mark.parametrize('stage', ['fsync', 'before_commit'])
def test_lineage_incremental_failure_leaves_rows_cursor_and_cache_unchanged(witness, monkeypatch, stage):
    class CrashBeforeCommit(sqlite3.Connection):
        def commit(self):
            if stage == 'before_commit':
                raise OSError('synthetic failure after rows/cursor before commit')
            super().commit()

    witness.append(_raw('message', 'message', {'messageId': 'initial', 'senderId': A}))
    project([witness.root], witness.db, publish_lineage_root=witness.root)
    index = ProjectionIndex.from_prefix(witness.root, records.enumerate_committed(witness.root))
    witness.append(_raw('message', 'message', {'messageId': 'new', 'senderId': L}))
    with closing(sqlite3.connect(witness.db, factory=CrashBeforeCommit)) as conn:
        conn.execute('PRAGMA foreign_keys=ON')
        before = conn.execute('SELECT * FROM projector_state').fetchall()
        old_boundaries = index._boundaries
        if stage == 'fsync':
            original = os.fsync

            def fail(fd):
                if str(os.readlink(f'/proc/self/fd/{fd}')).endswith('contact-ids.jsonl'):
                    raise OSError('synthetic lineage fsync failure')
                original(fd)

            monkeypatch.setattr(os, 'fsync', fail)
        with pytest.raises(OSError):
            apply_committed(conn, index, witness.root, index.target(witness.root))
        assert conn.execute('SELECT * FROM projector_state').fetchall() == before
        assert conn.execute('SELECT count(*) FROM contacts').fetchone()[0] == 1
        assert index._boundaries == old_boundaries
    monkeypatch.undo()
    # A crash reservation is replayed from Layer 1, never duplicated or exposed early.
    rows = witness.lineage()
    project([witness.root], witness.db, publish_lineage_root=witness.root)
    assert witness.lineage() == rows and len(rows) == 2
    index = ProjectionIndex.from_prefix(witness.root, records.enumerate_committed(witness.root))
    with closing(sqlite3.connect(witness.db)) as conn:
        apply_committed(conn, index, witness.root, index.target(witness.root))
        assert conn.execute('SELECT count(*) FROM contacts').fetchone()[0] == 2


def test_lineage_incremental_new_id_keeps_existing_review_counts(witness):
    witness.append_same_owner_assertion((100, 300))
    project([witness.root], witness.db, publish_lineage_root=witness.root)
    witness.owner('identifier_ended', identifier=P, ended_ms=200)
    project([witness.root], witness.db, publish_lineage_root=witness.root)
    index = ProjectionIndex.from_prefix(witness.root, records.enumerate_committed(witness.root))
    assert index._report['review']['contact_id_lineage']
    witness.append(_raw('message', 'message', {'messageId': 'new', 'senderId': L, 'text': 'synthetic'}))
    with closing(sqlite3.connect(witness.db)) as conn:
        report = apply_committed(conn, index, witness.root, index.target(witness.root))
    oracle = witness.db.with_name('review-oracle.db')
    full = project([witness.root], oracle)
    assert table_digest(witness.db) == table_digest(oracle)
    assert report['review'] == full['review']


def test_offline_project_and_verify_do_not_write_raw_roots(witness, tmp_path, monkeypatch):
    import yeoman_gateway.history.project as projector

    witness.append(_raw('message', 'message', {'messageId': 'offline', 'senderId': L, 'text': 'synthetic'}))
    # Start without even the intake coordination lock, to catch new lock files.
    (witness.root / records.PURGE_DISPOSITION_LOCK).unlink()

    def snapshot():
        return {str(path.relative_to(witness.root)): (
            path.stat().st_size, path.stat().st_mtime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None)
            for path in witness.root.rglob('*')}

    def forbidden(*args, **kwargs):
        pytest.fail('offline projection entered the protected raw write path')

    monkeypatch.setattr(projector, 'publish_lineage', forbidden)
    monkeypatch.setattr(projector, 'enumerate_committed', forbidden)
    before = snapshot()
    report = project([witness.root], witness.db)
    assert report['unpublished_generated_ids'] == 1
    result = verify([witness.root], witness.db, scratch=tmp_path / 'scratch', frozen=True)
    assert result['deterministic'] and result['accounting_ok']
    assert snapshot() == before
    assert not (witness.root / 'derived/contact-ids.jsonl').exists()

    # Committed lineage is still applied without synchronizing or recovering the root.
    monkeypatch.undo()
    published = project([witness.root], witness.db, publish_lineage_root=witness.root)
    assert published['unpublished_generated_ids'] == 0
    monkeypatch.setattr(projector, 'publish_lineage', forbidden)
    monkeypatch.setattr(projector, 'enumerate_committed', forbidden)
    before = snapshot()
    replay = project([witness.root], witness.db)
    assert replay['unpublished_generated_ids'] == 0
    assert verify([witness.root], witness.db, scratch=tmp_path / 'scratch', frozen=True)['deterministic']
    assert snapshot() == before


@pytest.fixture
def lineage_batch(tmp_path):
    from yeoman_gateway.history.resolve import GeneratedContactId

    root = tmp_path / 'batch'
    source_files = [f'whatsapp/source-{i}.jsonl' for i in range(3)]
    refs = []
    counts = [0, 0, 0]
    contents = [[], [], []]
    for n in range(500 * 20):
        i = n % 3
        counts[i] += 1
        refs.append(f'{source_files[i]}#{counts[i]}')
        contents[i].append(records.dumps({'channel': 'whatsapp', 'received_ms': 10}) + '\n')
    for relative, content in zip(source_files, contents, strict=True):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(''.join(content))
    (root / 'AUDIT').write_text(records.dumps({'operator': 'synthetic'}) + '\n')
    generated = []
    for i in range(500):
        value = f'{880000000000 + i}@lid'
        generated.append(GeneratedContactId(str(uuid.uuid5(records._CONTACT_ID_NAMESPACE, value)),
                         value, value, None, None, tuple(sorted(refs[i * 20:(i + 1) * 20]))))
    return root, source_files, generated


def test_lineage_batch_publication_reads_each_source_once(lineage_batch, tmp_path, monkeypatch):
    from collections import Counter

    from yeoman_gateway.history.lineage import publish_lineage

    root, source_files, generated = lineage_batch
    opened = Counter()
    read_lines = Counter()
    original_open = Path.open
    original_fsync = os.fsync
    durable = []
    callbacks = []

    @contextmanager
    def counted_source(handle, relative):
        def physical_lines():
            for line in handle:
                read_lines[relative] += 1
                yield line

        with handle:
            yield physical_lines()

    def spy_open(path, *args, **kwargs):
        if path.parent == root / 'whatsapp' or path == root / 'AUDIT':
            opened[path.relative_to(root).as_posix()] += 1
        handle = original_open(path, *args, **kwargs)
        if path.parent == root / 'whatsapp':
            return counted_source(handle, path.relative_to(root).as_posix())
        return handle

    def synced(fd):
        original_fsync(fd)
        if str(os.readlink(f'/proc/self/fd/{fd}')).endswith('contact-ids.jsonl'):
            durable.append(True)

    def committed(receipt):
        assert durable
        callbacks.append(receipt)

    monkeypatch.setattr(Path, 'open', spy_open)
    monkeypatch.setattr(os, 'fsync', synced)
    receipts = publish_lineage(root, generated, first_published_ms=100, on_committed=committed)
    assert len(receipts) == len(callbacks) == 500
    assert all(opened[relative] <= 2 for relative in source_files)
    assert opened['AUDIT'] == 1
    assert sum(read_lines.values()) == 500 * 20
    assert len(durable) == 1
    assert [receipt.line_number for receipt in receipts] == list(range(1, 501))
    monkeypatch.undo()
    batch_bytes = (root / 'derived/contact-ids.jsonl').read_bytes()
    rows = [json.loads(line) for line in batch_bytes.splitlines()]
    single = tmp_path / 'single'
    for relative in [*source_files, 'AUDIT']:
        path = single / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((root / relative).read_bytes())
    assert [records.append_contact_id_record(single, row) for row in rows] == list(receipts)
    assert (single / 'derived/contact-ids.jsonl').read_bytes() == batch_bytes
    # A disposed physical ref suppresses even an already reserved ID.
    path = root / source_files[0]
    lines = path.read_bytes().splitlines(keepends=True)
    lines[0] = (records.dumps(records.TOMBSTONE) + '\n').encode()
    path.write_bytes(b''.join(lines))
    with pytest.raises(ValueError, match='disposed'):
        publish_lineage(root, generated, first_published_ms=200)
    assert (root / 'derived/contact-ids.jsonl').read_bytes() == batch_bytes


@pytest.mark.perf
def test_lineage_batch_publication_cost(lineage_batch):
    from yeoman_gateway.history.lineage import publish_lineage

    root, _, generated = lineage_batch
    started = time.monotonic()
    receipts = publish_lineage(root, generated, first_published_ms=100)
    elapsed = time.monotonic() - started
    assert len(receipts) == 500
    assert elapsed <= 5, f'500-ID publication took {elapsed:.3f}s'


def test_lineage_batch_fsync_failure_withholds_all_receipts(lineage_batch, monkeypatch):
    from yeoman_gateway.history.lineage import publish_lineage

    root, _, generated = lineage_batch
    original_fsync = os.fsync
    callbacks = []

    def fail(fd):
        if str(os.readlink(f'/proc/self/fd/{fd}')).endswith('contact-ids.jsonl'):
            assert not callbacks
            raise OSError('synthetic batch fsync failure')
        original_fsync(fd)

    monkeypatch.setattr(os, 'fsync', fail)
    with pytest.raises(OSError, match='batch fsync'):
        publish_lineage(root, generated, first_published_ms=100, on_committed=callbacks.append)
    assert not callbacks
    reserved = (root / 'derived/contact-ids.jsonl').read_bytes()
    assert len(reserved.splitlines()) == 500
    monkeypatch.undo()
    receipts = publish_lineage(root, generated, first_published_ms=200, on_committed=callbacks.append)
    assert len(receipts) == 500 and not callbacks  # Reservation reuse does not notify again.
    assert (root / 'derived/contact-ids.jsonl').read_bytes() == reserved

