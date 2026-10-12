"""Synthetic retained-index cost and full/incremental equivalence witnesses."""
from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
import resource
import sqlite3
import sys
import threading
import time
import tracemalloc
from contextlib import closing, contextmanager
from dataclasses import fields, is_dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from yeoman_gateway.history.attestations import make, write_seed
from yeoman_gateway.history.extract import extract
from yeoman_gateway.history.incremental import ProjectionIndex, apply_committed
from yeoman_gateway.history.layer1 import canonical_json, iter_layer1, layer1_files
from yeoman_gateway.history.project import ProjectionRows, _write, build_rows, project
from yeoman_shared.raw_archive.records import append_line, enumerate_committed

from tests.gateway.convhist.hist_fixtures import T0, _raw, sample_layer1


def scale_fixture(root: Path, lines: int = 180_000) -> None:
    """17 sources; 1500 people, 3300 identifiers, ~250k native links at full scale."""
    write_seed(root)
    sources = [root / f'whatsapp/synthetic-{i:02}.jsonl' for i in range(15)]
    for path in sources:
        path.parent.mkdir(parents=True, exist_ok=True)
    handles = [path.open('w') for path in sources]
    try:
        for n in range(lines - 4):
            message = n < lines * 7 // 9
            number = n // 4 if message else n
            person = number % 1500
            lid = f'{97000000 + person}@lid'
            pn = f'{4915550000000 + person}@s.whatsapp.net'
            payload = {'chatJid': f'synthetic-{number % 164}@g.us',
                       'timestamp': T0 + number * 3000}
            if message:
                payload.update(messageId=f'M{number}', participantJid=lid,
                               senderPhoneJid=pn, senderName=f'Person {person}',
                               text=f'Synthetic message {number % 15855}')
                if person < 300:
                    payload['senderId'] = f'{4915560000000 + person}@s.whatsapp.net'
                row = _raw('message', 'message', payload, received=T0 + number * 3000)
                if n % 97 == 0:
                    row['media'] = {'kind': 'image', 'mimetype': 'image/png'}
            else:
                payload.update(complete=True, participants=[
                    {'lid': f'{97000000 + (person + i) % 1500}@lid',
                     'phoneJid': f'{4915550000000 + (person + i) % 1500}@s.whatsapp.net'}
                    for i in range(2)])
                row = _raw('membership_snapshot', 'membership_snapshot', payload,
                           received=T0 + number * 3000)
            row['synthetic_padding'] = 'x' * 1500
            handles[n % 15].write(canonical_json(row) + '\n')
    finally:
        for handle in handles:
            handle.close()
    # Seed is two lines; include both non-empty author/name and media evidence.
    with (root / 'owner/attestations.jsonl').open('a') as handle:
        handle.write(canonical_json(make('name', T0, 'synthetic fixture',
                                        anchor='97000000@lid', name='Person 0')) + '\n')
    with sources[0].open('a') as handle:
        handle.write(canonical_json({'kind': 'media_description', 'native_message_id': 'M0',
                                     'chat_id': 'synthetic-0@g.us', 'text': 'synthetic image'}) + '\n')


def rss() -> int:
    return int(Path('/proc/self/statm').read_text().split()[1]) * resource.getpagesize()


@contextmanager
def resource_watch():
    stopped = threading.Event()

    def monitor():
        next_log = time.monotonic()
        while not stopped.wait(0.1):
            held = rss()
            if held > 1_900_000_000:
                print(json.dumps({'resource_budget_stop': 'RSS exceeds 1.9 GB',
                                  'rss_bytes': held}), flush=True)
                os._exit(75)
            if time.monotonic() >= next_log:
                print(json.dumps({'heartbeat_rss_bytes': held}), flush=True)
                next_log = time.monotonic() + 15

    thread = threading.Thread(target=monitor, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join()


def breakdown(index: ProjectionIndex) -> dict[str, int]:
    """Exclusive reachable Python bytes, shared objects charged to first attribute."""
    seen: set[int] = set()

    def size(value):
        if id(value) in seen:
            return 0
        seen.add(id(value))
        total = sys.getsizeof(value)
        if isinstance(value, dict):
            return total + sum(size(k) + size(v) for k, v in value.items())
        if isinstance(value, (tuple, list, set, frozenset)):
            return total + sum(size(v) for v in value)
        if is_dataclass(value) and not isinstance(value, type):
            total += sum(size(getattr(value, f.name)) for f in fields(value))
            if hasattr(value, '__dict__'):
                total += size(value.__dict__)
        return total

    result = {}
    for name, value in sorted(vars(index).items()):
        result[name] = size(value)
        print(json.dumps({'structure': name, 'bytes': result[name]}), flush=True)
    return result


def snapshot(index: ProjectionIndex) -> bytes:
    res = index._resolution
    from dataclasses import asdict
    return canonical_json({
        'contacts': [asdict(c) for c in sorted(res.contacts, key=lambda c: c.contact_id)],
        'identifiers': [asdict(i) for i in sorted(res.identifiers,
                        key=lambda i: (i.contact_id, i.kind, i.value, str(i.valid_from_ms)))],
        'canonical': {k: asdict(v) for k, v in res.canonical.items()},
        'roles': res.role_contact, 'nodes': res.node_contact,
        'messages': sorted(index._messages_rows.values(), key=lambda r: r['message_id']),
        'events': sorted(index._events_rows.values(), key=lambda r: r['event_id']),
        'report': index._report, 'pending': index._pending,
    }).encode()


def materialize(index: ProjectionIndex, root: Path, db: Path) -> None:
    _write(db, ProjectionRows(index._resolution,
        sorted(index._messages_rows.values(), key=lambda r: r['message_id']),
        sorted(index._events_rows.values(), key=lambda r: r['event_id']), index._report),
        layer1_files([root]), index._pending)


def db_rows(db: Path) -> bytes:
    with closing(sqlite3.connect(db)) as conn:
        tables = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' AND (name NOT LIKE 'messages_fts_%') ORDER BY name")]
        digests = {}
        for table in tables:
            digest = hashlib.sha256()
            for row in sorted(conn.execute(f'SELECT * FROM "{table}"').fetchall(), key=canonical_json):
                digest.update(canonical_json(row).encode() + b'\n')
            digests[table] = digest.hexdigest()
        return canonical_json(digests).encode()



def equivalence(index: ProjectionIndex, root: Path, out: Path, conn=None) -> dict:
    full = out / 'full.db'
    extracted = extract(iter_layer1([root]))
    rows = build_rows(extracted)
    _write(full, rows, layer1_files([root]), extracted.pending_pairs)
    rebuilt = SimpleNamespace(_resolution=rows.resolution,
        _messages_rows={r['message_id']: r for r in rows.messages},
        _events_rows={r['event_id']: r for r in rows.events},
        _report=rows.report, _pending=extracted.pending_pairs)
    del extracted
    # Persist both paths in the same stable order to test literal SQLite bytes.
    a, b = out / 'index.db', out / 'rebuilt-index.db'
    materialize(index, root, a)
    materialize(rebuilt, root, b)
    index_equal = snapshot(index) == snapshot(rebuilt)
    assert index_equal
    byte_equal = a.read_bytes() == b.read_bytes()
    assert byte_equal
    assert db_rows(a) == db_rows(full)
    incremental_bytes = None
    if conn is not None:
        assert db_rows(out / 'incremental.db') == db_rows(full)
        incremental_bytes = (out / 'incremental.db').read_bytes() == full.read_bytes()
    return {'index_equal': index_equal, 'canonical_db_byte_equal': byte_equal,
            'full_rows_equal': True, 'incremental_physical_db_byte_equal': incremental_bytes,
            'db_sha256': hashlib.sha256(a.read_bytes()).hexdigest(),
            'index_sha256': hashlib.sha256(snapshot(index)).hexdigest()}


def benchmark(out: Path, *, lines=180_000, trace=False) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    root = out / 'raw'
    scale_fixture(root, lines)
    print('fixture ready', flush=True)
    with patch('time.time', return_value=T0 / 1000):
        project([root], out / 'incremental.db', publish_lineage_root=root)
    print('initial DB ready', flush=True)
    boundaries = enumerate_committed(root)
    gc.collect()
    baseline = rss()
    if trace:
        tracemalloc.start()
    began = time.perf_counter()
    index = ProjectionIndex.from_prefix(root, boundaries)
    seconds = time.perf_counter() - began
    gc.collect()
    held = rss()
    traced = tracemalloc.get_traced_memory() if trace else None
    if trace:
        tracemalloc.stop()
    result = {'lines': sum(b.line_number for b in boundaries), 'sources': len(boundaries),
              'raw_bytes': sum(b.end_offset for b in boundaries),
              'contacts': len([c for c in index._resolution.contacts if not c.merged_into]),
              'identifier_values': len({i.value for i in index._resolution.identifiers if i.kind != 'push_name'}),
              'links': len(index._input.links), 'seconds': seconds, 'rss_before_bytes': baseline,
              'rss_held_bytes': held, 'rss_delta_bytes': held - baseline,
              'rss_peak_bytes': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
              'tracemalloc_current_peak': traced}
    print(json.dumps(result), flush=True)
    (out / 'measurement.json').write_text(json.dumps(result, indent=2) + '\n')
    result['structures'] = breakdown(index)
    (out / 'measurement.json').write_text(json.dumps(result, indent=2) + '\n')
    if not trace or lines < 180_000:
        with closing(sqlite3.connect(out / 'incremental.db')) as conn:
            base = out / 'base'
            base.mkdir(exist_ok=True)
            result['base_equivalence'] = equivalence(index, root, base)
            # Exact late duplicate, media evidence, and successful request/result across files.
            additions = [
                ('whatsapp/synthetic-00.jsonl', _raw('message', 'message', {
                    'chatJid': 'synthetic-0@g.us', 'messageId': 'M0',
                    'participantJid': '97000000@lid', 'senderPhoneJid': '4915550000000@s.whatsapp.net',
                    'senderId': '4915560000000@s.whatsapp.net', 'senderName': 'Person 0',
                    'text': 'Synthetic message 0', 'timestamp': T0 + 9000})),
                ('derived/media-descriptions.jsonl', {'kind': 'media_description',
                    'native_message_id': 'M0', 'chat_id': 'synthetic-0@g.us', 'text': 'new description'}),
                ('whatsapp/synthetic-01.jsonl', _raw('outbound_request', 'send_text', {
                    'to': 'synthetic-0@g.us', 'text': 'sent'}, direction='out', corr='scale-pair')),
                ('whatsapp/synthetic-02.jsonl', dict(_raw('outbound_result', 'send_text', {},
                    direction='out', corr='scale-pair'), native={'result': {'ok': True,
                        'sent': {'providerMessageId': 'SCALE-SENT'}}})),
            ]
            result['apply_seconds'] = []
            for rel, row in additions:
                path = root / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                append_line(path, canonical_json(row))
                target = enumerate_committed(root)
                began = time.perf_counter()
                apply_committed(conn, index, root, target)
                result['apply_seconds'].append(time.perf_counter() - began)
            result['tail_equivalence'] = equivalence(index, root, out, conn)
    result['process_peak_bytes'] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
    (out / 'measurement.json').write_text(json.dumps(result, indent=2) + '\n')
    return result


def test_scale_equivalence_small(tmp_path):
    result = benchmark(tmp_path, lines=360)
    assert result['tail_equivalence']['canonical_db_byte_equal']


def test_existing_fixture_index_equivalence(tmp_path):
    live, dev = sample_layer1(tmp_path)
    # These fixture roots have disjoint filenames; copy into one committed prefix.
    import shutil
    root = tmp_path / 'raw'
    for source in (live, dev):
        for path in source.rglob('*.jsonl'):
            dest = root / path.relative_to(source)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, dest)
    index = ProjectionIndex.from_prefix(root, enumerate_committed(root))
    equivalence(index, root, tmp_path)


def test_units_share_empty_review_and_sender_metadata(tmp_path):
    from yeoman_gateway.history.incremental import RebuildRequired
    from yeoman_gateway.history.layer1 import Layer1Line

    root = tmp_path / 'raw'
    path = root / 'whatsapp/test.jsonl'
    path.parent.mkdir(parents=True)
    record = _raw('message', 'message', {'chatJid': 'test@g.us',
        'senderId': '97001@lid', 'text': 'hello', 'messageId': 'M'})
    path.write_text(canonical_json(record) + '\n' + canonical_json(record) + '\n')
    index = ProjectionIndex.from_prefix(root, enumerate_committed(root))
    a, b = index._extracted.values()
    assert not index._groups
    assert a.review is b.review
    assert a.events is b.events
    assert a.messages[0].sender is b.messages[0].sender
    assert a.messages[0].sender_raw is b.messages[0].sender_raw
    assert a.messages[0].chat_id is b.messages[0].chat_id
    with pytest.raises(RebuildRequired, match='source ref replayed'):
        index.plan([Layer1Line('whatsapp/test.jsonl#1', record)])


@pytest.mark.perf
def test_projection_index_scale_cost(tmp_path):
    with resource_watch():
        result = benchmark(tmp_path)
    assert result['rss_delta_bytes'] <= 600_000_000
    assert sum(result['structures'].values()) <= 600_000_000
    assert result['seconds'] <= 99
    assert result['process_peak_bytes'] < 2_000_000_000


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('out', type=Path)
    parser.add_argument('--lines', type=int, default=180_000)
    parser.add_argument('--trace', action='store_true')
    args = parser.parse_args()
    with resource_watch():
        benchmark(args.out, lines=args.lines, trace=args.trace)
