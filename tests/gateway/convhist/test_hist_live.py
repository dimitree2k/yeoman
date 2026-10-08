"""Isolated dormant lifecycle, ownership and all-destination turn barriers."""
import asyncio
import json
import os
import sqlite3
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest
from hist_fixtures import _bf, write_jsonl
from test_hist_incremental import T0, G, observation
from yeoman_gateway.history.project import project
from yeoman_gateway.history.verify import table_digest
from yeoman_shared.config.loader import load_config
from yeoman_shared.config.schema import Config
from yeoman_shared.raw_archive.records import append_line, dumps, enumerate_committed
from yeoman_shared.raw_archive.writer import RawArchive, RawEvent


def fixture(tmp_path, count=1):
    root, db = tmp_path / 'raw', tmp_path / 'history.db'
    write_jsonl(root / 'whatsapp/2026-10.jsonl', [observation(native_id=f'M{i}') for i in range(count)])
    project([root], db, publish_lineage_root=root)
    archive = RawArchive(root, spool=tmp_path / 'spool', status_path=tmp_path / 'status.json')
    return root, db, archive


def projector_fixture(tmp_path):
    from yeoman_gateway.history.live import HistoryProjector
    root, db, archive = fixture(tmp_path)
    return root, db, archive, HistoryProjector(root, db, archive)


async def start_ready(projector):
    await projector.start()
    if projector._startup_task is not None:
        await projector._startup_task


def test_history_projector_disabled_by_default_and_explicit_false(tmp_path, monkeypatch):
    from yeoman_gateway.app.bootstrap import build_history_projector
    monkeypatch.setenv('YEOMAN_HOME', str(tmp_path))
    for config in (Config(), Config(history={'liveProjectionEnabled': False})):
        channels = SimpleNamespace(raw_archive=None)
        assert build_history_projector(config, channels) is None
    assert not (tmp_path / 'data/history').exists()


@pytest.mark.parametrize('key', ['YEOMAN_HISTORY__LIVE_PROJECTION_ENABLED', 'YEOMAN_HISTORY__liveProjectionEnabled', 'YEOMAN_HISTORY'])
@pytest.mark.parametrize('payload', [{}, {'tools': {'exec': {'restrictToWorkspace': True}}}, {'history': {'liveProjectionEnabled': False}}, None])
def test_history_environment_cannot_activate_loader_or_bootstrap(tmp_path, monkeypatch, key, payload):
    from yeoman_gateway.app.bootstrap import build_history_projector
    monkeypatch.setenv('YEOMAN_HOME', str(tmp_path))
    monkeypatch.setenv(key, '{"liveProjectionEnabled":true}' if key == 'YEOMAN_HISTORY' else 'true')
    path = tmp_path / 'settings.json'
    if payload is not None:
        path.write_text(json.dumps(payload))
    channels = SimpleNamespace(raw_archive=None)
    for _ in range(2):
        assert build_history_projector(load_config(path), channels) is None
    assert not (tmp_path / 'data/history').exists()
    assert list(tmp_path.rglob('*.lock')) == []
    assert not [t for t in threading.enumerate() if t.name.startswith('history-projector')]


def test_history_file_true_is_honored_without_environment_override(tmp_path, monkeypatch):
    from yeoman_gateway.app.bootstrap import build_history_projector
    monkeypatch.setenv('YEOMAN_HOME', str(tmp_path))
    monkeypatch.setenv('YEOMAN_HISTORY', '{"liveProjectionEnabled":false}')
    path = tmp_path / 'settings.json'
    path.write_text(json.dumps({'history': {'liveProjectionEnabled': True}}))
    root, db, archive = fixture(tmp_path)
    p = build_history_projector(load_config(path), SimpleNamespace(raw_archive=archive))
    assert p is not None and p.archive is archive
    assert not (tmp_path / 'data/history').exists()
    with pytest.raises(ValueError):
        build_history_projector(load_config(path), SimpleNamespace(raw_archive=None))


@pytest.mark.asyncio
async def test_history_single_writer_rejects_offline_project_and_second_gateway(tmp_path):
    from yeoman_gateway.history.live import HistoryProjector
    root, db, archive, p = projector_fixture(tmp_path)
    await start_ready(p)
    before = table_digest(db)
    try:
        for destination in (db, db.parent / '.' / db.name):
            with pytest.raises((PermissionError, BlockingIOError)):
                project([root], destination)
        alias = tmp_path / 'alias.db'
        alias.symlink_to(db)
        with pytest.raises((PermissionError, ValueError, BlockingIOError)):
            project([root], alias)
        for destination in (db, tmp_path / 'different.db'):
            other = HistoryProjector(root, destination, archive)
            await other.start()
            assert other.health()['status'] == 'failed'
            assert other.health()['reason'] == ('writer_lock_held' if destination == db else 'projection_owner_held')
            await other.stop()
        result = subprocess.run([os.sys.executable, '-c', 'from pathlib import Path; from yeoman_gateway.history.live import acquire_history_writer; acquire_history_writer(Path(__import__("sys").argv[1]))', str(db)], capture_output=True)
        assert result.returncode != 0
        assert table_digest(db) == before
        assert not db.with_name(db.name + '.building').exists()
    finally:
        await p.stop()
    project([root], db)


@pytest.mark.asyncio
async def test_active_projection_refuses_unfenced_owner_mutations(tmp_path):
    from test_hist_protected_import import package, prepare_import_manifest
    from yeoman_shared.raw_archive.purge import PurgeSelector, purge
    from yeoman_shared.raw_archive.records import (
        acquire_projection_owner,
        append_owner_record,
        append_owner_record_locked,
        import_backfill,
        owner_mutation_guard,
    )
    root, db, archive = fixture(tmp_path)
    fd = acquire_projection_owner(root)
    record = {'attestation_version': 2, 'type': 'synthetic'}
    before = enumerate_committed(root)
    staged = package(tmp_path)
    manifest = prepare_import_manifest(staged)
    try:
        for append in (append_owner_record, append_owner_record_locked):
            with pytest.raises(PermissionError):
                append(root, record)
        with pytest.raises(PermissionError):
            import_backfill(root, staged, manifest)
        with pytest.raises(PermissionError):
            purge(root, PurgeSelector('whatsapp', native_id='M0'), operator='synthetic')
        wrong_fd = os.open(db, os.O_RDONLY)
        try:
            with pytest.raises(PermissionError):
                with owner_mutation_guard(root, projection_owner_fd=wrong_fd):
                    pass
        finally:
            os.close(wrong_fd)
        assert enumerate_committed(root) == before
        assert append_owner_record(root, record, projection_owner_fd=fd) is not None
        assert import_backfill(root, staged, manifest, projection_owner_fd=fd)['status'] == 'complete'
        purge(root, PurgeSelector('whatsapp', native_id='M0'), operator='synthetic', projection_owner_fd=fd)
        assert append_line(root / 'whatsapp/2026-10.jsonl', dumps(observation(native_id='M1')))
    finally:
        os.close(fd)


@pytest.mark.asyncio
async def test_startup_pending_purge_cannot_publish_ready_before_repair(tmp_path, monkeypatch):
    import yeoman_shared.raw_archive.purge as purge_module
    from yeoman_shared.raw_archive.purge import PurgeSelector, purge
    root, db, archive, p = projector_fixture(tmp_path)
    original = purge_module._publish_pending
    monkeypatch.setattr(purge_module, '_publish_pending', lambda *a, **kw: (_ for _ in ()).throw(OSError('synthetic failure')))
    with pytest.raises(OSError):
        purge(root, PurgeSelector('whatsapp', native_id='M0'), operator='synthetic')
    monkeypatch.setattr(purge_module, '_publish_pending', original)
    await start_ready(p)
    assert p.health()['status'] == 'rebuilding'
    assert p.health()['reason'] == 'rebuild_required'
    await p.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize('block_at', ['commit', 'drain'])
async def test_projector_shutdown_settles_commit_and_releases_ownership(tmp_path, monkeypatch, block_at):
    import yeoman_gateway.history.live as live
    root, db, archive, p = projector_fixture(tmp_path)
    descriptors_before = len(list(__import__('pathlib').Path('/proc/self/fd').iterdir()))
    await start_ready(p)
    first_generation = p.health()['generation']
    reached, release = threading.Event(), threading.Event()
    real = live.apply_committed
    def blocked(*args):
        reached.set()
        assert release.wait(5)
        return real(*args)
    if block_at == 'commit':
        monkeypatch.setattr(live, 'apply_committed', blocked)
    else:
        original_drain = archive.drain_spool
        def blocked_drain():
            reached.set()
            assert release.wait(5)
            return original_drain()
        monkeypatch.setattr(archive, 'drain_spool', blocked_drain)
    append_line(root / 'whatsapp/2026-10.jsonl', dumps(observation(native_id='M1')))
    turn = asyncio.create_task(p.read_turn())
    await asyncio.to_thread(reached.wait, 5)
    turn.cancel()
    stop = asyncio.create_task(p.stop())
    await asyncio.sleep(0.02)
    assert not stop.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await turn
    await stop
    monkeypatch.setattr(live, 'apply_committed', real)
    if block_at == 'drain':
        monkeypatch.setattr(archive, 'drain_spool', original_drain)
    await start_ready(p)
    assert p.health()['generation'] > first_generation
    snapshot = await p.read_turn()
    assert snapshot.connection.execute('SELECT count(*) FROM messages').fetchone()[0] == 2
    snapshot.close()
    await p.stop()
    oracle = tmp_path / 'oracle.db'
    project([root], oracle)
    assert table_digest(db) == table_digest(oracle)
    assert len(list(__import__('pathlib').Path('/proc/self/fd').iterdir())) <= descriptors_before
    assert not [t for t in threading.enumerate() if t.name.startswith('history-projector')]


@pytest.mark.asyncio
async def test_barrier_waits_for_all_prior_committed_destinations(tmp_path, monkeypatch):
    import yeoman_gateway.history.live as live
    root, db, archive, p = projector_fixture(tmp_path)
    append_line(root / 'owner/attestations.jsonl', dumps({'purged_version': 1}))
    project([root], db, publish_lineage_root=root)
    await start_ready(p)
    reached, release = threading.Event(), threading.Event()
    real = live.apply_committed
    def blocked(conn, index, root, target):
        assert {'whatsapp/2026-10.jsonl', 'derived/media-transcripts.jsonl', 'owner/attestations.jsonl'} <= {b.relative_path for b in target}
        reached.set()
        assert release.wait(5)
        return real(conn, index, root, target)
    monkeypatch.setattr(live, 'apply_committed', blocked)
    append_line(root / 'whatsapp/2026-10.jsonl', dumps(observation(native_id='M1')))
    append_line(root / 'derived/media-transcripts.jsonl', dumps({'kind': 'media_transcript', 'channel': 'whatsapp', 'native_message_id': 'M1', 'chat_id': G, 'text': 'synthetic transcript', 'generated_ms': T0}))
    expected = enumerate_committed(root)
    calls = []
    async def consumer():
        snapshot = await p.read_turn()
        calls.append('context')
        return snapshot
    task = asyncio.create_task(consumer())
    await asyncio.to_thread(reached.wait, 5)
    assert not task.done() and not calls
    release.set()
    try:
        snapshot = await task
        assert snapshot.sources == expected
        assert snapshot.generation == p.health()['generation']
        assert snapshot.connection.execute('SELECT count(*) FROM messages').fetchone()[0] == 2
        attrs = snapshot.connection.execute("SELECT media_json FROM messages WHERE native_message_id='M1'").fetchone()[0]
        assert json.loads(attrs)['transcript']['text'] == 'synthetic transcript'
        snapshot.close()
    finally:
        await p.stop()


@pytest.mark.asyncio
async def test_projection_failure_archives_but_pauses_history_turn(tmp_path, monkeypatch):
    import yeoman_gateway.history.live as live
    root, db, archive, p = projector_fixture(tmp_path)
    await start_ready(p)
    monkeypatch.setattr(live, 'apply_committed', lambda *a: (_ for _ in ()).throw(sqlite3.OperationalError('synthetic failure')))
    event = RawEvent(channel='whatsapp', kind='message', direction='in', native=observation(native_id='M1')['native'], received_ms=1791000000000)
    assert archive.append_durable(event)
    calls = []
    with pytest.raises(live.HistoryPaused):
        snapshot = await p.read_turn()
        calls.append('context')
        snapshot.close()
    assert calls == []
    assert b'M1' in (root / 'whatsapp/2026-10.jsonl').read_bytes()
    assert p.health()['status'] == 'failed' and p.health()['lag_lines'] > 0
    await p.stop()


@pytest.mark.asyncio
async def test_barrier_accounts_skips_and_rechecks_missed_callbacks(tmp_path):
    root, db, archive, p = projector_fixture(tmp_path)
    await start_ready(p)
    append_line(root / 'derived/media-descriptions.jsonl', dumps({'purged_version': 1}))
    append_line(root / 'backfill/telegram.jsonl', dumps(_bf('journal', 'message', {'text': 'skip'}, channel='telegram')))
    append_line(root / 'whatsapp/2026-10.jsonl', '')
    append_line(root / 'whatsapp/2026-10.jsonl', dumps({'purged_version': 1}))
    snapshot = await p.read_turn()
    assert snapshot.sources == enumerate_committed(root)
    assert snapshot.connection.execute('SELECT count(*) FROM messages').fetchone()[0] == 1
    snapshot.close()
    await p.stop()


@pytest.mark.asyncio
async def test_barrier_backlog_preserves_archive_and_expires_turn(tmp_path, monkeypatch):
    from yeoman_gateway.history.live import HistoryPaused
    root, db, archive, p = projector_fixture(tmp_path)
    await start_ready(p)
    original_append = archive._append_archive_line
    monkeypatch.setattr(archive, '_append_archive_line', lambda *a, **kw: (_ for _ in ()).throw(OSError('synthetic disk failure')))
    event = RawEvent(channel='whatsapp', kind='message', direction='in', native=observation(native_id='RETAINED')['native'], received_ms=T0)
    assert archive.append(event) is False
    assert archive.status().spooled == 1
    retained = next(archive.spool.glob('*.json')).read_bytes()
    with pytest.raises(HistoryPaused):
        await p.read_turn()
    assert p.health()['status'] == 'backlog'
    assert next(archive.spool.glob('*.json')).read_bytes() == retained
    calls = []
    async def expired():
        async with asyncio.timeout(0.01):
            async with p._operation_lock:
                calls.append('context')
    async with p._operation_lock:
        with pytest.raises(TimeoutError):
            await expired()
    assert not calls
    monkeypatch.setattr(archive, '_append_archive_line', original_append)
    snapshot = await p.read_turn()
    snapshot.close()
    assert archive.status().spooled == 0 and not calls
    await p.stop()


@pytest.mark.perf
@pytest.mark.asyncio
async def test_barrier_30k_no_backlog_p95(tmp_path):
    from yeoman_gateway.history.live import HistoryProjector
    root, db, archive = fixture(tmp_path, count=30000)
    p = HistoryProjector(root, db, archive)
    await start_ready(p)
    samples = []
    for _ in range(30):
        start = time.perf_counter()
        snapshot = await p.read_turn()
        samples.append((time.perf_counter() - start) * 1000)
        snapshot.close()
    await p.stop()
    p95 = sorted(samples)[28]
    print(f'barrier_samples=30 p95_ms={p95:.3f} max_ms={max(samples):.3f}')
    assert p95 <= 100


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['absent', 'schema2'])
async def test_startup_incompatible_database_stays_paused_and_untouched(tmp_path, kind):
    from yeoman_gateway.history.live import HistoryPaused, HistoryProjector
    root = tmp_path / 'raw'
    write_jsonl(root / 'whatsapp/2026-10.jsonl', [{'purged_version': 1}])
    db = tmp_path / 'history.db'
    if kind == 'schema2':
        conn = sqlite3.connect(db)
        conn.execute('PRAGMA user_version=2')
        conn.execute('CREATE TABLE marker(value TEXT)')
        conn.commit()
        conn.close()
    before = db.read_bytes() if db.exists() else None
    archive = RawArchive(root, spool=tmp_path / 'spool', status_path=tmp_path / 'status.json')
    p = HistoryProjector(root, db, archive)
    await start_ready(p)
    try:
        assert p.health()['status'] == 'rebuilding'
        with pytest.raises(HistoryPaused):
            await p.read_turn()
        assert (db.read_bytes() if db.exists() else None) == before
        assert not db.with_name(db.name + '-wal').exists()
    finally:
        await p.stop()


@pytest.mark.parametrize('enabled', [False, True])
def test_real_bootstrap_keeps_existing_consumers_and_raw_instance(tmp_path, monkeypatch, enabled):
    from yeoman_gateway.app.bootstrap import build_gateway_runtime
    from yeoman_gateway.bus.queue import MessageBus
    from yeoman_gateway.policy.engine import PolicyEngine
    from yeoman_gateway.policy.schema import PolicyConfig

    class Provider:
        async def chat(self, *args, **kwargs):
            raise AssertionError('no model calls')

        def get_default_model(self):
            return 'test/provider'

    monkeypatch.setenv('YEOMAN_HOME', str(tmp_path))
    config = Config.model_validate({'history': {'liveProjectionEnabled': enabled}, 'personaEvolution': {'enabled': False}, 'security': {'enabled': False}})
    runtime = build_gateway_runtime(config=config, provider=Provider(), policy_engine=PolicyEngine(PolicyConfig(), workspace=tmp_path), policy_path=None, workspace=tmp_path / 'workspace', bus=MessageBus())
    try:
        if enabled:
            assert runtime.history_projector.archive is runtime.channels.raw_archive
        else:
            assert runtime.history_projector is None
        assert not (tmp_path / 'data/history').exists()
        assert not list(tmp_path.rglob('*writer.lock'))
        assert not list(tmp_path.rglob('.history-projection-owner.lock'))
        assert runtime.channels.inbound_archive is runtime.inbound_archive
        assert runtime.speakup_log is None
        assert runtime.opportunity_scheduler is None
    finally:
        runtime.inbound_archive.close()
        runtime.chat_registry.close()
        runtime.contacts.close()
        runtime.memory.close()


@pytest.mark.asyncio
async def test_notifications_coalesce_and_ignore_telegram(tmp_path, monkeypatch):
    from yeoman_gateway.history.live import HistoryProjector
    from yeoman_shared.raw_archive.records import CommittedLine
    root, db, archive = fixture(tmp_path)
    p = HistoryProjector(root, db, archive)
    scheduled = []
    p._loop = SimpleNamespace(call_soon_threadsafe=lambda callback: scheduled.append(callback))
    for number in range(1000):
        p.notify_committed(CommittedLine('whatsapp/2026-10.jsonl', number, number))
        p.notify_committed(CommittedLine('telegram/2026-10.jsonl', number, number))
    assert len(scheduled) == 1
    assert len(p._high_water) == 1
    assert p._high_water['whatsapp/2026-10.jsonl'].line_number == 999


@pytest.mark.asyncio
async def test_barrier_reserves_new_lineage_and_pins_only_final_vector(tmp_path):
    root, db, archive, p = projector_fixture(tmp_path)
    await start_ready(p)
    append_line(root / 'whatsapp/2026-10.jsonl', dumps(observation(native_id='NEW', sender='4915550000099@s.whatsapp.net')))
    snapshot = await p.read_turn()
    assert snapshot.sources == enumerate_committed(root)
    assert snapshot.connection.execute('SELECT count(*) FROM messages').fetchone()[0] == 2
    assert next(b for b in snapshot.sources if b.relative_path == 'derived/contact-ids.jsonl').line_number == 2
    snapshot.close()
    await p.stop()


def test_invalid_owner_operations_create_no_raw_destination(tmp_path):
    from yeoman_shared.raw_archive.purge import PurgeSelector, purge
    from yeoman_shared.raw_archive.records import append_owner_record, import_backfill
    root = tmp_path / 'raw'
    for operation in (lambda: append_owner_record(root, {}), lambda: import_backfill(root, tmp_path / 'missing', {}), lambda: purge(root, PurgeSelector('', native_id='M'), operator='synthetic')):
        with pytest.raises((ValueError, FileNotFoundError)):
            operation()
        assert not root.exists()


@pytest.mark.asyncio
async def test_failed_projector_health_accounts_continuing_capture(tmp_path, monkeypatch):
    import yeoman_gateway.history.live as live
    root, db, archive, p = projector_fixture(tmp_path)
    await start_ready(p)
    monkeypatch.setattr(live, 'apply_committed', lambda *a: (_ for _ in ()).throw(sqlite3.OperationalError('synthetic failure')))
    for native_id in ('M1', 'M2'):
        assert archive.append_durable(RawEvent(channel='whatsapp', kind='message', direction='in', native=observation(native_id=native_id)['native'], received_ms=T0))
        with pytest.raises(live.HistoryPaused):
            await p.read_turn()
    await asyncio.sleep(0.01)
    assert p.health()['status'] == 'failed'
    assert p.health()['lag_lines'] == 2
    assert 'M2' not in json.dumps(p.health())
    await p.stop()


@pytest.mark.asyncio
async def test_startup_unreserved_generated_ids_require_repair(tmp_path):
    from yeoman_gateway.history.live import HistoryPaused, HistoryProjector
    root, db, archive = fixture(tmp_path)
    # Simulate an offline default projection that never published reservations.
    (root / 'derived/contact-ids.jsonl').unlink()
    project([root], db)
    p = HistoryProjector(root, db, archive)
    await start_ready(p)
    try:
        with pytest.raises(HistoryPaused):
            await p.read_turn()
        assert p.health()['reason'] == 'rebuild_required'
    finally:
        await p.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize('slow_index', [False, True])
async def test_gateway_history_lifecycle_starts_before_producers_and_stops_last(tmp_path, monkeypatch, slow_index):
    from yeoman_gateway.app import bootstrap
    events = []
    channels_started = asyncio.Event()
    history = None
    release = threading.Event()
    if slow_index:
        import yeoman_gateway.history.live as live
        root, db, archive, p = projector_fixture(tmp_path)
        reached = threading.Event()
        real = live.ProjectionIndex.from_prefix
        def slow(raw_root, boundaries):
            reached.set()
            assert release.wait(5)
            return real(raw_root, boundaries)
        monkeypatch.setattr(live.ProjectionIndex, 'from_prefix', slow)
        async def history_start():
            await p.start()
            events.append('history.start')
        async def history_stop():
            release.set()
            await p.stop()
            events.append('history.stop')
        history = SimpleNamespace(start=history_start, stop=history_stop)
    async def channels_start():
        if slow_index:
            assert await asyncio.to_thread(reached.wait, 3)
            assert p.health()['status'] == 'starting'
            with pytest.raises(live.HistoryPaused, match='starting'):
                await p.read_turn()
        events.append('channels.start')
        channels_started.set()
    async def record(name):
        events.append(name)
    async def run():
        await channels_started.wait()
        raise RuntimeError('synthetic shutdown')
    def service(name):
        return SimpleNamespace(start=lambda: record(name + '.start'), stop=lambda: record(name + '.stop'), close=lambda: events.append(name + '.close'))
    monkeypatch.setattr(bootstrap.tracing, 'init', lambda: None)
    monkeypatch.setattr(bootstrap.tracing, 'shutdown', lambda: record('tracing.stop'))
    reconciliation = service('recovery')
    reconciliation.recover_once = lambda: record('recovery.once')
    runtime = bootstrap.GatewayRuntime(
        orchestrator=SimpleNamespace(run=run, stop=lambda: events.append('orchestrator.stop')),
        channels=SimpleNamespace(start_all=channels_start, stop_all=lambda: record('channels.stop')),
        cron=SimpleNamespace(start=lambda: record('cron.start'), stop=lambda: events.append('cron.stop')),
        heartbeat=SimpleNamespace(start=lambda: record('heartbeat.start'), stop=lambda: events.append('heartbeat.stop')),
        inbound_archive=service('inbound'), responder=SimpleNamespace(tools={}, aclose=lambda: record('responder.close')),
        memory=service('memory'), contacts=service('contacts'), chat_registry=service('registry'),
        reconciliation=reconciliation, processing=service('processing'), history_projector=history or service('history'),
    )
    with pytest.raises(RuntimeError, match='synthetic shutdown'):
        await runtime.run()
    assert events.index('recovery.once') < events.index('history.start') < events.index('cron.start')
    assert events.index('history.start') < events.index('channels.start')
    assert events.index('channels.stop') < events.index('history.stop')
    assert events.index('responder.close') < events.index('history.stop')
    assert events.index('processing.close') < events.index('history.stop')


@pytest.mark.asyncio
async def test_barrier_reselects_all_destinations_after_lineage_reservation(tmp_path, monkeypatch):
    import yeoman_gateway.history.incremental as incremental
    root, db, archive, p = projector_fixture(tmp_path)
    await start_ready(p)
    real = incremental.publish_lineage
    def publish(*args, **kwargs):
        receipts = real(*args, **kwargs)
        append_line(root / 'whatsapp/2026-10.jsonl', dumps(observation(native_id='DURING_RESERVATION')))
        return receipts
    monkeypatch.setattr(incremental, 'publish_lineage', publish)
    append_line(root / 'whatsapp/2026-10.jsonl', dumps(observation(native_id='NEW', sender='4915550000099@s.whatsapp.net')))
    try:
        snapshot = await p.read_turn()
        assert snapshot.sources == enumerate_committed(root)
        assert snapshot.connection.execute('SELECT count(*) FROM messages').fetchone()[0] == 3
        snapshot.close()
    finally:
        await p.stop()


@pytest.mark.asyncio
async def test_empty_barrier_uses_one_target_and_no_full_enumeration(tmp_path, monkeypatch):
    import yeoman_gateway.history.live as live
    root, db, archive, p = projector_fixture(tmp_path)
    await start_ready(p)
    real = live.ProjectionIndex.target
    calls = []
    def target(index, raw_root):
        calls.append('target')
        return real(index, raw_root)
    monkeypatch.setattr(live.ProjectionIndex, 'target', target)
    def unexpected(*args, **kwargs):
        raise AssertionError('idle barrier must not enumerate or project again')
    monkeypatch.setattr(live, 'enumerate_committed', unexpected)
    monkeypatch.setattr(live, 'apply_committed', unexpected)
    try:
        snapshot = await p.read_turn()
        assert calls == ['target']
        snapshot.close()
    finally:
        await p.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize('stop_early', [False, True])
async def test_projector_start_returns_before_index_build(tmp_path, monkeypatch, stop_early):
    import yeoman_gateway.history.live as live
    root, db, archive, p = projector_fixture(tmp_path)
    reached, release = threading.Event(), threading.Event()
    real = live.ProjectionIndex.from_prefix
    def slow(raw_root, boundaries):
        reached.set()
        assert release.wait(5)
        return real(raw_root, boundaries)
    monkeypatch.setattr(live.ProjectionIndex, 'from_prefix', slow)
    start = asyncio.create_task(p.start())
    try:
        assert await asyncio.to_thread(reached.wait, 3)
        assert start.done(), 'start must return while index build is blocked'
        await start
        assert p.health()['status'] == 'starting'
        with pytest.raises(live.HistoryPaused, match='starting'):
            await asyncio.wait_for(p.read_turn(), 0.2)
        with pytest.raises(live.HistoryPaused, match='starting'):
            await asyncio.wait_for(p.barrier(), 0.2)
        assert archive.append_durable(RawEvent(channel='whatsapp', kind='message', direction='in', chat_id=G, account='default', native=observation(native_id='DURING_STARTUP')['native'], received_ms=T0))
        assert p._high_water
        if stop_early:
            stop = asyncio.create_task(p.stop())
            await asyncio.sleep(0.01)
            assert not stop.done()
            release.set()
            await stop
            assert p.health()['status'] == 'disabled'
            assert p._executor is None and p._connection is None
        else:
            release.set()
            await p._startup_task
            snapshot = await p.read_turn()
            assert snapshot.connection.execute("SELECT count(*) FROM messages WHERE native_message_id='DURING_STARTUP'").fetchone()[0] == 1
            snapshot.close()
    finally:
        release.set()
        await start
        await p.stop()
    for acquire, path in ((live.acquire_history_writer, db), (live.acquire_projection_owner, root)):
        os.close(acquire(path))
    assert not [t for t in threading.enumerate() if t.name.startswith('history-projector')]


@pytest.mark.asyncio
@pytest.mark.parametrize('lock_kind', ['writer', 'owner'])
async def test_projector_lock_contention_keeps_gateway_running(tmp_path, monkeypatch, lock_kind):
    from yeoman_gateway.app import bootstrap
    from yeoman_gateway.bus.queue import MessageBus
    from yeoman_gateway.history.live import HistoryPaused, history_db_path
    from yeoman_gateway.policy.engine import PolicyEngine
    from yeoman_gateway.policy.schema import PolicyConfig

    class Provider:
        async def chat(self, *args, **kwargs):
            raise AssertionError('no model calls')
        def get_default_model(self):
            return 'test/provider'

    monkeypatch.setenv('YEOMAN_HOME', str(tmp_path))
    config = Config.model_validate({'history': {'liveProjectionEnabled': True}, 'personaEvolution': {'enabled': False}, 'security': {'enabled': False}})
    runtime = bootstrap.build_gateway_runtime(config=config, provider=Provider(), policy_engine=PolicyEngine(PolicyConfig(), workspace=tmp_path), policy_path=None, workspace=tmp_path / 'workspace', bus=MessageBus())
    p = runtime.history_projector
    root, db = runtime.channels.raw_archive.root, history_db_path(create=True)
    write_jsonl(root / 'whatsapp/2026-10.jsonl', [observation()])
    project([root], db, publish_lineage_root=root)
    before = db.read_bytes()
    function = 'acquire_history_writer' if lock_kind == 'writer' else 'acquire_projection_owner'
    import_path = 'yeoman_gateway.history.live' if lock_kind == 'writer' else 'yeoman_shared.raw_archive.records'
    process = subprocess.Popen([os.sys.executable, '-u', '-c', f'from pathlib import Path; from {import_path} import {function}; fd={function}(Path(__import__("sys").argv[1])); print("locked", flush=True); __import__("sys").stdin.read(1)', str(db if lock_kind == 'writer' else root)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    events = []
    async def record(name):
        events.append(name)
    monkeypatch.setattr(bootstrap.tracing, 'init', lambda: None)
    monkeypatch.setattr(bootstrap.tracing, 'shutdown', lambda: record('tracing.stop'))
    monkeypatch.setattr(runtime.cron, 'start', lambda: record('cron.start'))
    monkeypatch.setattr(runtime.heartbeat, 'start', lambda: record('heartbeat.start'))
    monkeypatch.setattr(runtime.orchestrator, 'run', lambda: record('orchestrator.start'))
    expected = 'writer_lock_held' if lock_kind == 'writer' else 'projection_owner_held'
    async def channels_start():
        events.append('channels.start')
        assert p.health()['status'] == 'failed' and p.health()['reason'] == expected
        assert p.health()['retry_policy'] == 'none'
        with pytest.raises(HistoryPaused):
            await p.read_turn()
        assert runtime.channels.raw_archive.append_durable(RawEvent(channel='whatsapp', kind='message', direction='in', native=observation(native_id='CAPTURE_CONTINUES')['native'], received_ms=T0))
    monkeypatch.setattr(runtime.channels, 'start_all', channels_start)
    runtime.bus = None
    runtime.gateway_socket = None
    runtime.startup_hook = None
    try:
        assert await asyncio.to_thread(process.stdout.readline) == 'locked\n'
        await runtime.run()
        assert 'channels.start' in events
        assert b'CAPTURE_CONTINUES' in (root / 'whatsapp/2026-10.jsonl').read_bytes()
        assert db.read_bytes() == before
    finally:
        await p.stop()
        process.communicate('x', timeout=5)


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['lock_setup', 'index'])
async def test_projector_startup_exception_pauses_without_retry(tmp_path, monkeypatch, stage):
    import yeoman_gateway.history.live as live
    root, db, archive, p = projector_fixture(tmp_path)
    before = db.read_bytes()
    calls = []
    def failed(*args, **kwargs):
        calls.append(stage)
        raise OSError('synthetic startup failure')
    if stage == 'lock_setup':
        monkeypatch.setattr(live, 'acquire_history_writer', failed)
    else:
        monkeypatch.setattr(live.ProjectionIndex, 'from_prefix', failed)
    await start_ready(p)
    try:
        assert p.health()['status'] == 'failed' and p.health()['reason'] == 'startup_failed'
        await p.start()
        assert calls == [stage]
        with pytest.raises(live.HistoryPaused):
            await p.read_turn()
        assert archive.append_durable(RawEvent(channel='whatsapp', kind='message', direction='in', native=observation(native_id='AFTER_STARTUP_FAILURE')['native'], received_ms=T0))
        assert db.read_bytes() == before
    finally:
        await p.stop()
