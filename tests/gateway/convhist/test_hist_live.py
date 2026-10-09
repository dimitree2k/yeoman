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
    try:
        await p._automatic_rebuild_task
        assert p.health()['status'] == 'ready'
        await oracle_parity(p, root, tmp_path)
    finally:
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
        assert release.wait(30)  # upper bound only; generous for full-suite load
        return real(*args)
    if block_at == 'commit':
        monkeypatch.setattr(live, 'apply_committed', blocked)
    else:
        original_drain = archive.drain_spool
        def blocked_drain():
            reached.set()
            assert release.wait(30)  # upper bound only; generous for full-suite load
            return original_drain()
        monkeypatch.setattr(archive, 'drain_spool', blocked_drain)
    append_line(root / 'whatsapp/2026-10.jsonl', dumps(observation(native_id='M1')))
    turn = asyncio.create_task(p.read_turn())
    await asyncio.to_thread(reached.wait, 30)  # upper bound only
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
        assert release.wait(30)  # upper bound only; generous for full-suite load
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
    await asyncio.to_thread(reached.wait, 30)  # upper bound only
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
async def test_startup_incompatible_database_pauses_until_automatic_repair(tmp_path, kind):
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
    archive = RawArchive(root, spool=tmp_path / 'spool', status_path=tmp_path / 'status.json')
    p = HistoryProjector(root, db, archive)
    await start_ready(p)
    try:
        assert p.health()['status'] == 'rebuilding'
        with pytest.raises(HistoryPaused):
            await p.read_turn()
        await p._automatic_rebuild_task
        assert p.health()['status'] == 'ready'
        await oracle_parity(p, root, tmp_path)
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
            assert runtime.gateway_socket.history_control_handler.__self__ is runtime.history_projector
        else:
            assert runtime.history_projector is None
            assert runtime.gateway_socket.history_control_handler is None
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
        assert p.health()['reason'] == 'unpublished generated IDs require fenced repair'
        await p._automatic_rebuild_task
        assert p.health()['status'] == 'ready'
        await oracle_parity(p, root, tmp_path)
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
            assert release.wait(30)  # upper bound only; generous for full-suite load
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
            assert await asyncio.to_thread(reached.wait, 30)  # upper bound only
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
        assert release.wait(30)  # upper bound only; generous for full-suite load
        return real(raw_root, boundaries)
    monkeypatch.setattr(live.ProjectionIndex, 'from_prefix', slow)
    start = asyncio.create_task(p.start())
    try:
        assert await asyncio.to_thread(reached.wait, 30)  # upper bound only
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


async def oracle_parity(p, root, tmp_path):
    oracle = tmp_path / 'oracle.db'
    project([root], oracle)
    snapshot = await p.read_turn()
    try:
        from yeoman_gateway.history.verify import _table_digest
        assert _table_digest(snapshot.connection) == table_digest(oracle)
        with sqlite3.connect(oracle) as conn:
            assert snapshot.connection.execute('SELECT * FROM messages_current ORDER BY message_id').fetchall() == conn.execute('SELECT * FROM messages_current ORDER BY message_id').fetchall()
        assert snapshot.sources == enumerate_committed(root)
    finally:
        snapshot.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('signal', ['read_turn', 'catch_up', 'notification'])
async def test_rebuild_required_triggers_one_automatic_fenced_rebuild(tmp_path, monkeypatch, signal):
    import yeoman_gateway.history.live as live
    from test_hist_incremental import PN
    from yeoman_gateway.history.attestations import make
    root, db, archive, p = projector_fixture(tmp_path)
    await start_ready(p)
    generation = p.health()['generation']
    reached, release = threading.Event(), threading.Event()
    verified = []
    real = live.verify_rebuild_candidate

    def verify(*args, **kwargs):
        result = real(*args, **kwargs)
        verified.append(result)
        append_line(root / 'whatsapp/2026-10.jsonl', dumps(observation(native_id='REPAIR-TAIL')))
        reached.set()
        assert release.wait(30)  # upper bound only; generous for full-suite load
        return result

    monkeypatch.setattr(live, 'verify_rebuild_candidate', verify)
    append_line(root / 'owner/attestations.jsonl', dumps(make('name', T0, 'synthetic repair', anchor=PN, name='Synthetic name')))
    try:
        if signal == 'notification':
            assert archive.append_durable(RawEvent(channel='whatsapp', kind='message', direction='in', native=observation(native_id='NOTIFICATION')['native'], received_ms=T0))
        else:
            with pytest.raises(live.HistoryPaused):
                if signal == 'catch_up':
                    await p.catch_up(enumerate_committed(root))
                else:
                    await p.read_turn()
        assert await asyncio.to_thread(reached.wait, 30)  # upper bound only
        assert p.health()['status'] == 'rebuilding'
        assert p.health()['reason'] == 'owner or identity decision requires fenced rebuild'
        with pytest.raises(live.HistoryPaused):
            await p.read_turn()
        release.set()
        await p._automatic_rebuild_task
        assert len(verified) == 1
        assert p.health()['status'] == 'ready'
        assert p.health()['generation'] == generation + 1
        assert p._index._boundaries == enumerate_committed(root)
        await oracle_parity(p, root, tmp_path)
    finally:
        release.set()
        await p.stop()


@pytest.mark.asyncio
async def test_failed_automatic_rebuild_does_not_retry(tmp_path, monkeypatch):
    import yeoman_gateway.history.live as live
    from yeoman_gateway.history.incremental import RebuildRequired
    root, db, archive, p = projector_fixture(tmp_path)
    await start_ready(p)
    attempts = []
    real = live.verify_rebuild_candidate

    def fail(*args, **kwargs):
        attempts.append(True)
        raise ValueError('synthetic verification failure')

    monkeypatch.setattr(live, 'verify_rebuild_candidate', fail)
    try:
        p._failed(RebuildRequired('synthetic repair category'))
        assert p._automatic_rebuild_task is not None
        await p._automatic_rebuild_task
        assert p.health()['status'] == 'failed'
        assert p.health()['reason'] == 'rebuild_failed'
        for _ in range(3):
            p._failed(RebuildRequired('synthetic later signal'))
            assert archive.append_durable(RawEvent(channel='whatsapp', kind='message', direction='in', native=observation(native_id=f'AFTER-FAIL-{_}')['native'], received_ms=T0))
            with pytest.raises(live.HistoryPaused):
                await p.read_turn()
        await asyncio.sleep(0)
        assert attempts == [True]
        assert p.health()['status'] == 'failed'
        assert p.health()['reason'] == 'rebuild_failed'
        assert p.health()['lag_lines'] >= 3
        monkeypatch.setattr(live, 'verify_rebuild_candidate', real)
        await p.rebuild(reason='owner_repair')
        assert p.health()['status'] == 'ready'
        assert not p._automatic_rebuild_failed
        await oracle_parity(p, root, tmp_path)
    finally:
        await p.stop()


@pytest.mark.asyncio
async def test_automatic_rebuild_coalesces_scheduled_and_running_signals(tmp_path, monkeypatch):
    from yeoman_gateway.history.incremental import RebuildRequired
    root, db, archive, p = projector_fixture(tmp_path)
    await start_ready(p)
    reached, release = threading.Event(), threading.Event()
    attempts = []
    real = p._build_replace_release

    def build():
        attempts.append(True)
        reached.set()
        assert release.wait(30)  # upper bound only; generous for full-suite load
        return real()

    monkeypatch.setattr(p, '_build_replace_release', build)
    try:
        p._failed(RebuildRequired('first category'))
        task = p._automatic_rebuild_task
        assert task is not None
        for _ in range(3):
            p._failed(RebuildRequired('scheduled category'))
            assert p._automatic_rebuild_task is task
        assert await asyncio.to_thread(reached.wait, 30)  # upper bound only
        for _ in range(3):
            p._failed(RebuildRequired('running category'))
            assert p._automatic_rebuild_task is task
        assert p.health()['reason'] == 'first category'
        release.set()
        await task
        assert attempts == [True]
        assert p.health()['status'] == 'ready'
    finally:
        release.set()
        await p.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['scheduled', 'reader_lease', 'building'])
async def test_stop_settles_automatic_rebuild_without_retry(tmp_path, monkeypatch, phase):
    from yeoman_gateway.history.incremental import RebuildRequired
    root, db, archive, p = projector_fixture(tmp_path)
    await start_ready(p)
    snapshot = await p.read_turn() if phase == 'reader_lease' else None
    reached, release = threading.Event(), threading.Event()
    real = p._build_replace_release
    attempts = []

    def build():
        attempts.append(True)
        reached.set()
        assert release.wait(30)  # upper bound only; generous for full-suite load
        return real()

    monkeypatch.setattr(p, '_build_replace_release', build)
    try:
        p._failed(RebuildRequired('synthetic shutdown category'))
        task = p._automatic_rebuild_task
        assert task is not None
        if phase == 'building':
            assert await asyncio.to_thread(reached.wait, 30)  # upper bound only
        elif phase == 'reader_lease':
            await asyncio.sleep(0)
        stop = asyncio.create_task(p.stop())
        if phase == 'building':
            await asyncio.sleep(0.01)
            assert not stop.done()
        release.set()
        await asyncio.wait_for(stop, 30)  # upper bound only
        assert task.done()
        assert p.health()['status'] == 'disabled'
        assert p._executor is None and p._connection is None
        p._failed(RebuildRequired('while stopping'))
        assert p.health()['status'] == 'disabled'
    finally:
        release.set()
        if snapshot is not None:
            snapshot.close()
        await p.stop()


@pytest.mark.asyncio
async def test_rebuild_fence_catches_tail_before_release(tmp_path, monkeypatch):
    from yeoman_gateway.history import live
    root, db, archive, p = projector_fixture(tmp_path)
    await start_ready(p)
    old = await p.read_turn()
    generation = old.generation
    copied, release = threading.Event(), threading.Event()
    original = live.copy_committed
    def block(*args):
        original(*args)
        copied.set()
        assert release.wait(20)
    monkeypatch.setattr(live, 'copy_committed', block)
    task = asyncio.create_task(p.rebuild(reason='owner_request'))
    try:
        await asyncio.sleep(.02)
        assert p.health()['status'] == 'rebuilding'
        assert not copied.is_set()  # Existing lease must drain before any candidate work.
        from yeoman_gateway.history.live import HistoryPaused
        with pytest.raises(HistoryPaused):
            await p.read_turn()
        old.close()
        assert await asyncio.to_thread(copied.wait, 10)
        # Native/derived appends remain available while build has no raw lock.
        from test_hist_incremental import paired
        tail_time = T0 + 31 * 86400000
        assert archive.append_durable(RawEvent(channel='whatsapp', kind='message', direction='in', chat_id=G, account='default', native=observation(native_id='TAIL')['native'], received_ms=tail_time))
        assert archive.append_media_transcript({'kind': 'media_transcript', 'channel': 'whatsapp', 'native_message_id': 'TAIL', 'chat_id': G, 'text': 'synthetic transcript', 'generated_ms': tail_time})
        append_line(root / 'whatsapp/2026-10.jsonl', dumps(paired('outbound_request')))
        append_line(root / 'whatsapp/2026-11.jsonl', dumps(paired('outbound_result')))
        release.set()
        await task
        assert p.health()['generation'] > generation
        snapshot = await p.read_turn()
        try:
            assert json.loads(snapshot.connection.execute("SELECT media_json FROM messages WHERE native_message_id='TAIL'").fetchone()[0])['transcript']['text'] == 'synthetic transcript'
            assert snapshot.connection.execute("SELECT count(*) FROM messages WHERE native_message_id='S' AND direction='out'").fetchone() == (1,)
        finally:
            snapshot.close()
        await oracle_parity(p, root, tmp_path)
    finally:
        old.close()
        release.set()
        await p.stop()


@pytest.mark.asyncio
async def test_rebuild_missing_candidate_row_blocks_replace_and_reply_release(tmp_path, monkeypatch):
    from yeoman_gateway.history import live
    from yeoman_gateway.history.live import HistoryPaused, HistoryProjector
    root, db, archive = fixture(tmp_path, 2)
    p = HistoryProjector(root, db, archive)
    await start_ready(p)
    before, generation = db.read_bytes(), p.health()['generation']
    original = live.verify_rebuild_candidate
    def corrupt(roots, candidate, **kwargs):
        with sqlite3.connect(candidate) as conn:
            conn.execute('DELETE FROM messages WHERE message_id=(SELECT message_id FROM messages LIMIT 1)')
            assert conn.execute('PRAGMA integrity_check').fetchall() == [('ok',)]
            assert conn.execute('PRAGMA foreign_key_check').fetchall() == []
        return original(roots, candidate, **kwargs)
    monkeypatch.setattr(live, 'verify_rebuild_candidate', corrupt)
    try:
        with pytest.raises(ValueError, match='semantic_digest_mismatch'):
            await p.rebuild(reason='owner_request')
        assert db.read_bytes() == before
        assert p.health()['generation'] == generation
        assert p.health()['status'] == 'failed'
        with pytest.raises(HistoryPaused):
            await p.read_turn()
    finally:
        await p.stop()


@pytest.mark.asyncio
async def test_purge_fence_failure_never_serves_erased_projection(tmp_path, monkeypatch):
    from yeoman_gateway.history import live
    from yeoman_gateway.history.live import HistoryPaused
    from yeoman_shared.raw_archive.purge import PurgeSelector, purge
    root, db, archive, p = projector_fixture(tmp_path)
    row = observation(native_id='M0')
    row['chat_id'] = G
    write_jsonl(root / 'whatsapp/2026-10.jsonl', [row])
    from test_hist_incremental import OTHER, PN
    write_jsonl(root / 'backfill/mixed.jsonl', [_bf('memory', 'message', {'messageId': 'KEEP', 'segments': [
        {'senderId': PN, 'text': 'erase synthetic', 'messageId': 'M0'},
        {'senderId': OTHER, 'text': 'keep synthetic', 'messageId': 'KEEP'}]}, chat=G)])
    project([root], db, publish_lineage_root=root)
    await start_ready(p)
    original = live.verify_rebuild_candidate
    def fail(*args, **kwargs):
        raise ValueError('synthetic_verify_failure')
    monkeypatch.setattr(live, 'verify_rebuild_candidate', fail)
    try:
        with pytest.raises(ValueError):
            await p.rebuild(reason='purge', mutation=lambda fd: purge(root, PurgeSelector(channel='whatsapp', native_id='M0'), operator='synthetic', projection_owner_fd=fd))
        with pytest.raises(HistoryPaused):
            await p.read_turn()
        append_line(root / 'whatsapp/2026-11.jsonl', dumps(observation(native_id='AFTER')))
        monkeypatch.setattr(live, 'verify_rebuild_candidate', original)
        await p.rebuild(reason='recovery')
        s = await p.read_turn()
        try:
            assert s.connection.execute('SELECT native_message_id FROM messages').fetchall() == [('AFTER',), ('KEEP',)]
        finally:
            s.close()
        batch = json.loads((root / 'backfill/mixed.jsonl').read_text())
        assert batch['payload']['segments'][0] == {'purged_version': 1}
        assert any(json.loads(line) == {'purged_version': 1} for line in (root / 'derived/contact-ids.jsonl').read_text().splitlines())
        await oracle_parity(p, root, tmp_path)
    finally:
        await p.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['before_close', 'after_close', 'after_replace', 'after_directory_fsync', 'after_generation_publish', 'after_tail_commit'])
async def test_rebuild_wal_replace_crash_and_verify_failure(tmp_path, monkeypatch, stage):
    from yeoman_gateway.history.live import HistoryPaused, HistoryProjector
    root, db, archive = fixture(tmp_path)
    script = tmp_path / 'crash.py'
    script.write_text("""
import asyncio, os, sys
from pathlib import Path
from yeoman_gateway.history.live import HistoryProjector
from yeoman_shared.raw_archive.writer import RawArchive
from yeoman_shared.raw_archive.records import append_line
root, db, stage = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
archive = RawArchive(root, spool=root.parent / 'child-spool', status_path=root.parent / 'child-status.json')
async def crash():
    p = HistoryProjector(root, db, archive)
    await p.start()
    await p._startup_task
    assert p.health()['status'] == 'ready'
    method = {'before_close': '_checkpoint_close', 'after_close': '_checkpoint_close',
              'after_replace': '_replace_candidate', 'after_directory_fsync': '_sync_directory',
              'after_generation_publish': '_publish_generation', 'after_tail_commit': '_release_tail'}[stage]
    original = getattr(p, method)
    def exit_at_stage(*args):
        if stage not in ('before_close', 'after_tail_commit'):
            original(*args)
        os._exit(70)
    setattr(p, method, exit_at_stage)
    if stage == 'after_tail_commit':
        replace = p._replace_candidate
        def tail(candidate):
            replace(candidate)
            append_line(root / 'whatsapp/2026-11.jsonl', sys.argv[4])
        p._replace_candidate = tail
    await p.rebuild(reason='owner_request')
    raise AssertionError('crash point was not reached')
asyncio.run(crash())
""")
    result = await asyncio.to_thread(subprocess.run, [os.sys.executable, str(script), str(root), str(db), stage, dumps(observation(native_id='CRASH-TAIL'))], capture_output=True, timeout=330)
    assert result.returncode == 70, result.stderr.decode()
    p = HistoryProjector(root, db, archive)
    await start_ready(p)
    try:
        # No orderly shutdown: persisted admission and WAL recovery must carry the fence.
        assert p.health()['status'] != 'ready'
        with pytest.raises(HistoryPaused):
            await p.read_turn()
        other = HistoryProjector(root, db, archive)
        await start_ready(other)
        assert other.health()['status'] == 'failed'
        await other.stop()
        await p._automatic_rebuild_task
        assert p.health()['generation'] > 2
        await oracle_parity(p, root, tmp_path)
    finally:
        await p.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['window', 'merge', 'unmerge'])
async def test_identity_fence_window_merge_unmerge_matches_full(tmp_path, case):
    from test_hist_incremental import OTHER, PN
    from yeoman_gateway.history.attestations import make
    from yeoman_shared.raw_archive.records import append_owner_record
    root, db, archive, p = projector_fixture(tmp_path)
    append_line(root / 'whatsapp/2026-10.jsonl', dumps(observation(native_id='OTHER', sender=OTHER)))
    await start_ready(p)
    generation = p.health()['generation']
    try:
        row = (make('identifier', T0, 'synthetic', anchor=PN, identifier=OTHER, valid_from_ms=T0, valid_until_ms=T0 + 1000)
               if case == 'window' else make(case, T0, 'synthetic', a=PN, b=OTHER))
        await p.rebuild(reason='identity', mutation=lambda fd: append_owner_record(root, row, projection_owner_fd=fd, on_committed=p.notify_committed))
        assert p.health()['generation'] > generation
        await oracle_parity(p, root, tmp_path)
    finally:
        await p.stop()


@pytest.mark.asyncio
async def test_rebuild_release_reselects_after_tail_commit(tmp_path, monkeypatch):
    root, db, archive, p = projector_fixture(tmp_path)
    await start_ready(p)
    original = p._catch_up
    appended = []
    def append_after_commit(target):
        original(target)
        if not appended:
            appended.append(True)
            append_line(root / 'whatsapp/2026-11.jsonl', dumps(observation(native_id='LATE-TAIL')))
    monkeypatch.setattr(p, '_catch_up', append_after_commit)
    try:
        await p.rebuild(reason='owner_request')
        await oracle_parity(p, root, tmp_path)
        assert p.health()['lag_lines'] == 0
    finally:
        await p.stop()


@pytest.mark.asyncio
async def test_rebuild_mutation_is_not_repeated_after_verify_failure(tmp_path, monkeypatch):
    from yeoman_gateway.history import live
    root, db, archive, p = projector_fixture(tmp_path)
    await start_ready(p)
    calls = []
    original = live.verify_rebuild_candidate
    def fail(*args, **kwargs):
        raise ValueError('synthetic failure')
    monkeypatch.setattr(live, 'verify_rebuild_candidate', fail)
    try:
        with pytest.raises(ValueError):
            await p.rebuild(reason='owner_request', mutation=lambda fd: calls.append(fd))
        assert len(calls) == 1
        monkeypatch.setattr(live, 'verify_rebuild_candidate', original)
        await p.rebuild(reason='recovery')
        assert len(calls) == 1
    finally:
        await p.stop()


@pytest.mark.asyncio
async def test_purge_pending_recovery_append_forces_live_repair(tmp_path, monkeypatch):
    from yeoman_gateway.history.live import HistoryPaused
    from yeoman_shared.raw_archive import purge as purge_module
    from yeoman_shared.raw_archive.purge import PurgeSelector, purge
    root, db, archive, p = projector_fixture(tmp_path)
    row = observation(native_id='M0')
    row['chat_id'] = G
    write_jsonl(root / 'whatsapp/2026-10.jsonl', [row])
    project([root], db, publish_lineage_root=root)
    await start_ready(p)
    original = purge_module._fsync_directory
    replaced = []
    original_replace = purge_module.os.replace
    def replace(source, target):
        original_replace(source, target)
        if target == root / 'whatsapp/2026-10.jsonl':
            replaced.append(True)
    def fail(directory):
        if replaced:
            raise OSError('synthetic purge interruption')
        return original(directory)
    monkeypatch.setattr(purge_module.os, 'replace', replace)
    monkeypatch.setattr(purge_module, '_fsync_directory', fail)
    try:
        with pytest.raises(OSError):
            await p.rebuild(reason='purge', mutation=lambda fd: purge(root, PurgeSelector(channel='whatsapp', native_id='M0'), operator='synthetic', projection_owner_fd=fd))
        monkeypatch.setattr(purge_module, '_fsync_directory', original)
        assert archive.append_durable(RawEvent(channel='whatsapp', kind='message', direction='in', chat_id=G, account='default', native=observation(native_id='RECOVERED')['native'], received_ms=T0))
        with pytest.raises(HistoryPaused):
            await p.read_turn()
        await p.rebuild(reason='recovery')
        s = await p.read_turn()
        try:
            assert s.connection.execute('SELECT native_message_id FROM messages').fetchall() == [('RECOVERED',)]
        finally:
            s.close()
        await oracle_parity(p, root, tmp_path)
    finally:
        await p.stop()


@pytest.mark.asyncio
async def test_rebuild_repairs_absent_database_without_enabling_consumers(tmp_path):
    from yeoman_gateway.history.live import HistoryPaused, HistoryProjector
    root, db, archive = fixture(tmp_path)
    db.unlink()
    p = HistoryProjector(root, db, archive)
    await start_ready(p)
    try:
        with pytest.raises(HistoryPaused):
            await p.read_turn()
        await p.rebuild(reason='owner_request')
        assert p.health()['status'] == 'ready'
        await oracle_parity(p, root, tmp_path)
    finally:
        await p.stop()


@pytest.mark.asyncio
async def test_rebuild_identity_tail_stays_in_one_fence_until_second_verified_prefix(tmp_path, monkeypatch):
    from test_hist_incremental import PN
    from yeoman_gateway.history import live
    from yeoman_gateway.history.attestations import make
    root, db, archive, p = projector_fixture(tmp_path)
    await start_ready(p)
    original = live.ProjectionIndex.write_candidate
    builds = []
    def owner_tail(index, prefix, candidate, raw_root):
        result = original(index, prefix, candidate, raw_root)
        builds.append(True)
        assert p.health()['status'] == 'rebuilding'
        if len(builds) == 1:
            append_line(root / 'owner/attestations.jsonl', dumps(make('name', T0, 'synthetic tail', anchor=PN, name='Tail identity')))
        return result
    monkeypatch.setattr(live.ProjectionIndex, 'write_candidate', owner_tail)
    generation = p.health()['generation']
    try:
        await p.rebuild(reason='owner_request')
        assert len(builds) == 2
        assert p.health()['generation'] == generation + 2
        await oracle_parity(p, root, tmp_path)
    finally:
        await p.stop()


@pytest.mark.asyncio
async def test_rebuild_reuses_candidate_index_after_replace(tmp_path, monkeypatch):
    from yeoman_gateway.history import live
    root, db, archive, p = projector_fixture(tmp_path)
    await start_ready(p)
    original_build = live.ProjectionIndex.from_prefix
    original_replace = p._replace_candidate
    replaced, builds = [], []
    def build(source, boundaries):
        assert not replaced, 'full index build after replace'
        builds.append(source)
        return original_build(source, boundaries)
    def replace(candidate):
        original_replace(candidate)
        replaced.append(True)
    monkeypatch.setattr(live.ProjectionIndex, 'from_prefix', build)
    monkeypatch.setattr(p, '_replace_candidate', replace)
    try:
        await p.rebuild(reason='synthetic')
        assert len(builds) == 1 and builds[0] != root
        await oracle_parity(p, root, tmp_path)
    finally:
        await p.stop()


@pytest.mark.parametrize('mutation', ['rewrite', 'replace', 'truncate', 'rewrite_grow'])
def test_index_rebind_refuses_changed_prefix(tmp_path, mutation):
    from yeoman_gateway.history.incremental import ProjectionIndex, RebuildRequired
    from yeoman_shared.raw_archive.records import copy_committed
    root, db, archive = fixture(tmp_path)
    boundaries = enumerate_committed(root)
    expected = {b.relative_path: (root / b.relative_path).stat() for b in boundaries}
    prefix = tmp_path / 'prefix'
    copy_committed(root, boundaries, prefix)
    index = ProjectionIndex.from_prefix(prefix, boundaries)
    path = root / next(b.relative_path for b in boundaries if b.relative_path.startswith('whatsapp/'))
    data = path.read_bytes()
    assert b'M0' in data
    if mutation == 'replace':
        replacement = path.with_suffix('.replacement')
        replacement.write_bytes(data)
        os.replace(replacement, path)
    elif mutation == 'truncate':
        path.write_bytes(data[:10])
    else:
        path.write_bytes(data.replace(b'M0', b'M9') + (b'{}\n' if mutation == 'rewrite_grow' else b''))
    with pytest.raises(RebuildRequired, match='changed'):
        index.rebind(root, expected)


@pytest.mark.perf
@pytest.mark.asyncio
async def test_rebuild_two_build_cost_at_30k(tmp_path, monkeypatch):
    from yeoman_gateway.history import live
    root, db = tmp_path / 'raw', tmp_path / 'history.db'
    write_jsonl(root / 'whatsapp/2026-10.jsonl', [
        observation(native_id=f'M{i}', sender=f'{4915554000000 + i % 1500}@s.whatsapp.net',
                    chat=f'chat-{i % 200}@g.us', text=f'Synthetic body {i}', ms=T0 + i)
        for i in range(30000)])
    started = time.perf_counter()
    project([root], db, publish_lineage_root=root)
    full_build = time.perf_counter() - started
    boundaries = enumerate_committed(root)
    started = time.perf_counter()
    live.verify_rebuild_candidate([root], db, boundaries=boundaries)
    independent_verify = time.perf_counter() - started
    original = live.ProjectionIndex.from_prefix
    index_times = []
    def measured(source, pinned):
        started = time.perf_counter()
        result = original(source, pinned)
        index_times.append(time.perf_counter() - started)
        return result
    monkeypatch.setattr(live.ProjectionIndex, 'from_prefix', measured)
    archive = RawArchive(root, spool=tmp_path / 'spool', status_path=tmp_path / 'status.json')
    p = live.HistoryProjector(root, db, archive)
    await start_ready(p)
    try:
        baseline = full_build + independent_verify + index_times[0]
        started = time.perf_counter()
        await p.rebuild(reason='synthetic perf')
        elapsed = time.perf_counter() - started
        print(f'rebuild full_build={full_build:.3f} independent_verify={independent_verify:.3f} '
              f'full_index={index_times[0]:.3f} three_build_seconds={baseline:.3f} '
              f'two_build_seconds={elapsed:.3f} saved_fraction={1 - elapsed / baseline:.3f}', flush=True)
        assert len(index_times) == 2
        # Regression guard: synthetic saving measured 13 %; real-volume rebuild 110.6 s -> 85.8 s (-22 %).
        assert elapsed <= baseline * 0.90
        await oracle_parity(p, root, tmp_path)
    finally:
        await p.stop()


@pytest.mark.asyncio
async def test_worker_snapshot_owns_thread_connection_and_reopens_after_rebuild(tmp_path):
    root, db, archive, p = projector_fixture(tmp_path)
    await start_ready(p)
    main_thread = threading.get_ident()
    snapshots = []
    def read(snapshot):
        assert threading.get_ident() != main_thread
        assert p._operation_lock.locked()
        assert not p._reader._snapshots
        snapshots.append(snapshot)
        return snapshot.generation, snapshot.connection.execute(
            "SELECT count(*) FROM messages").fetchone()[0]
    try:
        generation, count = await p.worker_snapshot(read)
        assert count == 1 and snapshots[-1]._closed
        await p.rebuild(reason="synthetic worker replacement")
        newer, count = await p.worker_snapshot(read)
        assert newer > generation and count == 1 and snapshots[-1]._closed
        def fail(snapshot):
            snapshots.append(snapshot)
            raise RuntimeError("bounded callback failure")
        with pytest.raises(RuntimeError, match="bounded callback"):
            await p.worker_snapshot(fail)
        assert snapshots[-1]._closed and not p._operation_lock.locked()
        p._status = "failed"
        with pytest.raises(Exception, match="failed"):
            await p.worker_snapshot(read)
    finally:
        await p.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_count", [1, 2])
async def test_worker_snapshot_cancellation_waits_for_callback_cleanup_and_repair(tmp_path, cancel_count):
    _, _, _, p = projector_fixture(tmp_path)
    await start_ready(p)
    entered, release = threading.Event(), threading.Event()
    snapshots = []
    def blocked(snapshot):
        snapshots.append(snapshot)
        entered.set()
        assert release.wait(30)
        return snapshot.generation
    try:
        task = asyncio.create_task(p.worker_snapshot(blocked))
        assert await asyncio.to_thread(entered.wait, 30)
        task.cancel()
        # Repair cannot replace while a worker callback owns its independent lease.
        repair = asyncio.create_task(p.rebuild(reason="synthetic worker cancellation"))
        await asyncio.sleep(0)
        assert p._operation_lock.locked() and not task.done() and not repair.done()
        if cancel_count == 2:
            task.cancel()
            await asyncio.sleep(0)
            assert p._operation_lock.locked() and not task.done() and not repair.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(repair, 30)
        assert snapshots[0]._closed
        assert not p._reader._snapshots and not p._operation_lock.locked()
    finally:
        release.set()
        await p.stop()
