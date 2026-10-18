"""Synthetic persistence, refusal and crash-safety witnesses."""
import json
import os
import shutil
import sqlite3
from contextlib import closing
from unittest.mock import patch

import pytest
from test_hist_incremental import observation
from test_hist_index_memory import db_rows, materialize, snapshot
from test_hist_live import fixture, start_ready
from yeoman_gateway.history.incremental import ProjectionIndex, apply_committed
from yeoman_gateway.history.live import HistoryProjector
from yeoman_gateway.history.snapshot import load_snapshot, save_snapshot, snapshot_path
from yeoman_shared.raw_archive.records import append_line, enumerate_committed


def test_roundtrip_and_incremental_tail(tmp_path):
    root, db, _ = fixture(tmp_path, count=20)
    boundaries = enumerate_committed(root)
    built = ProjectionIndex.from_prefix(root, boundaries)
    path = snapshot_path(db)
    save_snapshot(path, built, 1)
    assert path.stat().st_mode & 0o777 == 0o600
    loaded = load_snapshot(path, root, boundaries, 1)
    assert loaded is not None
    assert snapshot(built) == snapshot(loaded)
    assert loaded.target(root) == built.target(root)
    other = tmp_path / 'other.db'
    shutil.copyfile(db, other)
    for n in range(20, 24):
        append_line(root / 'whatsapp/2026-10.jsonl', json.dumps(observation(native_id=f'M{n}')))
    for index, dest in ((built, db), (loaded, other)):
        with closing(sqlite3.connect(dest)) as conn:
            conn.execute('PRAGMA foreign_keys=ON')
            apply_committed(conn, index, root, index.target(root))
    assert snapshot(built) == snapshot(loaded)
    assert db_rows(db) == db_rows(other)
    materialize(built, root, tmp_path / 'a.db')
    materialize(loaded, root, tmp_path / 'b.db')
    assert (tmp_path / 'a.db').read_bytes() == (tmp_path / 'b.db').read_bytes()


def test_staged_and_interrupted_write_preserve_previous(tmp_path):
    root, db, _ = fixture(tmp_path)
    index = ProjectionIndex.from_prefix(root, enumerate_committed(root))
    path = snapshot_path(db)
    save_snapshot(path, index, 1)
    previous = path.read_bytes()
    index.plan([])
    with pytest.raises(ValueError, match='staged'):
        save_snapshot(path, index, 1)
    assert path.read_bytes() == previous
    index._staged = None
    with patch('yeoman_gateway.history.snapshot.os.replace', side_effect=OSError('interrupted')):
        with pytest.raises(OSError):
            save_snapshot(path, index, 1)
    assert path.read_bytes() == previous
    assert not list(tmp_path.glob('.projection-index-*'))


def rewrite(path, *, change_header=None, payload=None):
    import hashlib
    with path.open('rb') as source:
        header = json.loads(source.readline())
        old_payload = source.read()
    if change_header:
        change_header(header)
    if payload is None:
        payload = old_payload
    header['checksum'] = hashlib.sha256(payload).hexdigest()
    path.write_bytes(json.dumps(header, sort_keys=True).encode() + b'\n' + payload)


@pytest.mark.parametrize('reason', [
    'missing', 'format', 'projector_version', 'schema_version', 'fingerprint',
    'boundaries', 'generation', 'checksum', 'unpickle', 'payload',
    'unsafe_symlink', 'unsafe_owner', 'unsafe_mode', 'unsafe_type', 'prefix',
    'io_error', 'load_error', 'code_changed',
])
async def test_refusal_rebuilds_and_reaches_ready(tmp_path, monkeypatch, reason):
    import yeoman_gateway.history.snapshot as cache
    root, db, archive = fixture(tmp_path)
    index = ProjectionIndex.from_prefix(root, enumerate_committed(root))
    path = snapshot_path(db)
    save_snapshot(path, index, 1)
    if reason == 'missing':
        path.unlink()
    elif reason == 'format':
        path.write_bytes(b'{truncated')
    elif reason in ('projector_version', 'schema_version', 'fingerprint', 'boundaries', 'generation'):
        rewrite(path, change_header=lambda h: h.update({reason: 'wrong'}))
    elif reason == 'checksum':
        with path.open('ab') as output:
            output.write(b'corrupt')
    elif reason == 'unpickle':
        rewrite(path, payload=b'not a pickle')
    elif reason == 'payload':
        import pickle
        rewrite(path, payload=pickle.dumps(({}, {})))
    elif reason == 'unsafe_symlink':
        target = path.with_suffix('.previous')
        path.rename(target)
        path.symlink_to(target)
    elif reason == 'unsafe_owner':
        uid = os.getuid()
        monkeypatch.setattr(cache.os, 'getuid', lambda: uid + 1)
    elif reason == 'unsafe_mode':
        path.chmod(0o622)
    elif reason == 'unsafe_type':
        path.unlink()
        os.mkfifo(path)
    elif reason == 'prefix':
        source = root / 'whatsapp/2026-10.jsonl'
        moved = source.with_suffix('.old')
        source.rename(moved)
        source.write_bytes(moved.read_bytes())  # Same bytes, different inode.
    elif reason == 'io_error':
        original = cache.os.open
        def denied(p, *args, **kwargs):
            if p == path:
                raise PermissionError('synthetic denial')
            return original(p, *args, **kwargs)
        monkeypatch.setattr(cache.os, 'open', denied)
    elif reason == 'code_changed':
        monkeypatch.setattr(cache, 'fingerprint', lambda: 'new source version')
    elif reason == 'load_error':
        monkeypatch.setattr(cache, 'fingerprint', lambda: (_ for _ in ()).throw(RuntimeError()))
    p = HistoryProjector(root, db, archive)
    with patch.object(cache.logger, 'info') as log, patch.object(
            ProjectionIndex, 'from_prefix', wraps=ProjectionIndex.from_prefix) as build:
        try:
            await start_ready(p)
            assert p.health()['status'] == 'ready', p.health()
            assert build.call_count == 1
            assert log.call_count == 1
            assert log.call_args.args[-1] == ('fingerprint' if reason == 'code_changed' else reason)
        finally:
            await p.stop()


async def test_clean_restart_uses_snapshot_and_catches_tail(tmp_path):
    root, db, archive = fixture(tmp_path)
    first = HistoryProjector(root, db, archive)
    await start_ready(first)
    await first.stop()
    append_line(root / 'whatsapp/2026-10.jsonl', json.dumps(observation(native_id='TAIL')))
    second = HistoryProjector(root, db, archive)
    with patch.object(ProjectionIndex, 'from_prefix', side_effect=AssertionError('unexpected rebuild')):
        try:
            await start_ready(second)
            assert second.health()['status'] == 'ready', second.health()
            with closing(sqlite3.connect(db)) as conn:
                assert conn.execute('SELECT count(*) FROM messages').fetchone()[0] == 2
        finally:
            await second.stop()


async def test_stop_after_live_catch_up_saves_a_snapshot_the_next_start_loads(tmp_path):
    # Catch-up stores boundaries in raw-subdir order (whatsapp before backfill), while
    # projector_state reads back sorted by path; the stop-time save must not refuse that.
    root, db, archive = fixture(tmp_path)
    (root / 'backfill').mkdir(exist_ok=True)
    append_line(root / 'backfill/2026-09.jsonl', json.dumps(observation(native_id='OLD')))
    first = HistoryProjector(root, db, archive)
    await start_ready(first)
    append_line(root / 'whatsapp/2026-10.jsonl', json.dumps(observation(native_id='LIVE')))
    await first.catch_up(first._index.target(root))
    await first.stop()
    with closing(sqlite3.connect(db)) as conn:
        stored = conn.execute("SELECT file, lines FROM projector_state WHERE file <> '@runtime' "
                              "ORDER BY file").fetchall()
    header = json.loads(snapshot_path(db).read_bytes().split(b'\n', 1)[0])
    assert [row[0] for row in stored] == [
        'backfill/2026-09.jsonl', 'derived/contact-ids.jsonl', 'whatsapp/2026-10.jsonl']
    assert [[b[0], b[1]] for b in header['boundaries']] == [list(row) for row in stored]
    second = HistoryProjector(root, db, archive)
    with patch.object(ProjectionIndex, 'from_prefix', side_effect=AssertionError('unexpected rebuild')):
        try:
            await start_ready(second)
            assert second.health()['status'] == 'ready', second.health()
        finally:
            await second.stop()


async def test_projector_does_not_save_uncommitted_or_staged_state(tmp_path):
    root, db, archive = fixture(tmp_path)
    p = HistoryProjector(root, db, archive)
    await start_ready(p)
    path = snapshot_path(db)
    previous = path.read_bytes()
    def check():
        p._index.plan([])
        p._save_snapshot()
        assert path.read_bytes() == previous
        p._index._staged = None
        p._connection.execute('BEGIN')
        p._save_snapshot()
        assert path.read_bytes() == previous
        p._connection.rollback()
        p._index._boundaries = ()
        p._save_snapshot()
        assert path.read_bytes() == previous
    try:
        await p._submit(check)
    finally:
        await p.stop()


async def test_rebuild_invalidates_before_mutation_and_writes_replacement(tmp_path):
    root, db, archive = fixture(tmp_path)
    p = HistoryProjector(root, db, archive)
    await start_ready(p)
    path = snapshot_path(db)
    assert path.exists()
    def mutation(_):
        assert not path.exists()
    try:
        await p.rebuild(reason='synthetic', mutation=mutation)
        assert p.health()['status'] == 'ready'
        assert path.exists()
        assert load_snapshot(path, root, p._index._boundaries, p._generation) is not None
    finally:
        await p.stop()


def test_offline_rebuild_invalidates(tmp_path):
    from yeoman_gateway.history.project import project
    root, db, _ = fixture(tmp_path)
    index = ProjectionIndex.from_prefix(root, enumerate_committed(root))
    path = snapshot_path(db)
    save_snapshot(path, index, 1)
    project([root], db, publish_lineage_root=root)
    assert not path.exists()


def test_roundtrip_with_snapshots_pairs_and_identity_metadata(tmp_path):
    from hist_fixtures import T0, _raw
    from test_hist_index_memory import scale_fixture
    from yeoman_gateway.history.project import project
    root = tmp_path / 'raw'
    scale_fixture(root, lines=600)
    db = tmp_path / 'history.db'
    project([root], db, publish_lineage_root=root)
    boundaries = enumerate_committed(root)
    built = ProjectionIndex.from_prefix(root, boundaries)
    path = snapshot_path(db)
    save_snapshot(path, built, 1)
    loaded = load_snapshot(path, root, boundaries, 1)
    assert loaded is not None
    other = tmp_path / 'other.db'
    shutil.copyfile(db, other)
    additions = [
        _raw('membership_snapshot', 'membership_snapshot', {
            'chatJid': 'synthetic-0@g.us', 'timestamp': T0, 'complete': True,
            'participants': [{'lid': '97000000@lid', 'phoneJid': '4915550000000@s.whatsapp.net'}]}),
        _raw('message', 'message', {
            'chatJid': 'synthetic-0@g.us', 'messageId': 'M0', 'participantJid': '97000000@lid',
            'senderPhoneJid': '4915550000000@s.whatsapp.net', 'senderName': 'Updated person',
            'text': 'Synthetic message 0', 'timestamp': T0 + 9000}),
        _raw('outbound_request', 'send_text', {'to': 'synthetic-0@g.us', 'text': 'sent'},
             direction='out', corr='snapshot-pair'),
        dict(_raw('outbound_result', 'send_text', {}, direction='out', corr='snapshot-pair'),
             native={'result': {'ok': True, 'sent': {'providerMessageId': 'SENT'}}}),
    ]
    for row in additions:
        append_line(root / 'whatsapp/synthetic-00.jsonl', json.dumps(row))
        for index, dest in ((built, db), (loaded, other)):
            with closing(sqlite3.connect(dest)) as conn:
                conn.execute('PRAGMA foreign_keys=ON')
                apply_committed(conn, index, root, index.target(root))
        assert snapshot(built) == snapshot(loaded)
        assert db_rows(db) == db_rows(other)
    materialize(built, root, tmp_path / 'a.db')
    materialize(loaded, root, tmp_path / 'b.db')
    assert (tmp_path / 'a.db').read_bytes() == (tmp_path / 'b.db').read_bytes()


@pytest.mark.perf
def test_snapshot_startup_cost_and_memory(tmp_path):
    """Independent processes keep build and load peak RSS measurements comparable."""
    import subprocess
    import sys
    from pathlib import Path

    from test_hist_index_memory import scale_fixture
    from yeoman_gateway.history.project import project
    root = tmp_path / 'raw'
    scale_fixture(root, lines=30_000)
    db = tmp_path / 'history.db'
    project([root], db, publish_lineage_root=root)
    results = {}
    env = dict(os.environ)
    env['PYTHONPATH'] = str(Path(__file__).resolve().parents[3]) + os.pathsep + env.get('PYTHONPATH', '')
    for mode in ('build', 'load'):
        run = subprocess.run([sys.executable, __file__, mode, str(root), str(db)],
                             capture_output=True, text=True, timeout=300, env=env)
        assert run.returncode == 0, run.stderr
        results[mode] = json.loads(run.stdout)
    print(json.dumps(results), flush=True)
    assert results['load']['index_seconds'] <= results['build']['index_seconds'] * 1.25
    assert results['load']['startup_seconds'] <= results['build']['startup_seconds'] * 1.25
    assert results['load']['peak_rss_bytes'] <= results['build']['peak_rss_bytes'] * 1.15


def test_source_changed_after_import_cannot_relabel_old_index(tmp_path, monkeypatch):
    import yeoman_gateway.history.snapshot as cache
    root, db, _ = fixture(tmp_path)
    index = ProjectionIndex.from_prefix(root, enumerate_committed(root))
    path = snapshot_path(db)
    save_snapshot(path, index, 1)
    previous = path.read_bytes()
    monkeypatch.setattr(cache, 'fingerprint', lambda: 'new source version')
    with pytest.raises(ValueError, match='fingerprint'):
        save_snapshot(path, index, 1)
    assert path.read_bytes() == previous

@pytest.mark.parametrize('change', ['bytes', 'truncate', 'symlink'])
def test_raw_prefix_validation(tmp_path, change):
    root, db, _ = fixture(tmp_path)
    boundaries = enumerate_committed(root)
    index = ProjectionIndex.from_prefix(root, boundaries)
    path = snapshot_path(db)
    save_snapshot(path, index, 1)
    source = root / 'whatsapp/2026-10.jsonl'
    before = source.stat()
    data = source.read_bytes()
    if change == 'bytes':
        source.write_bytes(data.replace(b'original', b'modified'))
        os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
    elif change == 'truncate':
        source.write_bytes(data[:-1])
    else:
        original = source.with_suffix('.original')
        source.rename(original)
        source.symlink_to(original)
    assert load_snapshot(path, root, boundaries, 1) is None


def test_versioned_format_is_refused(tmp_path):
    root, db, _ = fixture(tmp_path)
    boundaries = enumerate_committed(root)
    index = ProjectionIndex.from_prefix(root, boundaries)
    path = snapshot_path(db)
    save_snapshot(path, index, 1)
    rewrite(path, change_header=lambda h: h.update(format=999))
    assert load_snapshot(path, root, boundaries, 1) is None


async def test_snapshot_write_failure_does_not_fail_start_or_stop(tmp_path):
    root, db, archive = fixture(tmp_path)
    p = HistoryProjector(root, db, archive)
    with patch('yeoman_gateway.history.live.save_snapshot', side_effect=OSError('disk full')):
        try:
            await start_ready(p)
            assert p.health()['status'] == 'ready'
        finally:
            await p.stop()
    assert p.health()['status'] == 'disabled'


if __name__ == '__main__':
    import resource
    import sys
    import time
    from pathlib import Path

    from yeoman_shared.raw_archive.writer import RawArchive
    mode, root, db = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
    p = HistoryProjector(root, db, RawArchive(root, spool=root.parent / 'spool',
                                            status_path=root.parent / 'status.json'))
    method = ProjectionIndex.from_prefix if mode == 'build' else load_snapshot
    times = []
    def measured(*args, **kwargs):
        began = time.perf_counter()
        index = method(*args, **kwargs)
        times.append(time.perf_counter() - began)
        return index
    with patch('yeoman_gateway.history.live.ProjectionIndex.from_prefix', measured) if mode == 'build' else patch('yeoman_gateway.history.live.load_snapshot', measured):
        began = time.perf_counter()
        p._startup()
        seconds = time.perf_counter() - began
    assert p._index is not None
    result = {'index_seconds': times[-1], 'startup_seconds': seconds,
              'peak_rss_bytes': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
              'raw_bytes': sum(b.end_offset for b in p._index._boundaries)}
    p._close_writer()
    print(json.dumps(result))
