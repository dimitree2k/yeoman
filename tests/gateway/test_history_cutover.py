# ruff: noqa: F811
"""Synthetic whole-set cutover witnesses; host controls are never installed."""
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest
from yeoman_gateway.history.attestations import make
from yeoman_gateway.history.convert.run import prepare_import_manifest
from yeoman_gateway.history.layer1 import Origin, backfill_line
from yeoman_shared.raw_archive.records import import_backfill

from tests.gateway.convhist.consumer_fixtures import (  # noqa: F401
    capture_case,
    consumer_case,
    statement_case,
)


def procedure():
    spec = importlib.util.spec_from_file_location('cutover_operator', Path(__file__).parents[2] / 'scripts/history_cutover.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def record(tmp_path):
    home = tmp_path / 'isolated-home'
    home.mkdir()
    (home / 'knowledge.db').write_bytes(b'prior knowledge')
    (home / 'cron.json').write_text('{"jobs":[]}')
    (home / 'raw').mkdir()
    (home / 'raw/tail').write_text('old\n')
    (home / 'spool').mkdir()
    (home / 'outbox').write_text('old envelope')
    (home / 'disposition').write_text('old denial')
    (tmp_path / 'installed-text').write_text('prior owner text')
    items = [{'path': name, 'kind': 'file', 'restore': name not in ('outbox', 'disposition')}
             for name in ('knowledge.db', 'cron.json', 'outbox', 'disposition')]
    items += [{'path': name, 'kind': 'tree', 'restore': False} for name in ('raw', 'spool')]
    items.append(dict(path='external/installed-text', source=str(tmp_path/'installed-text'), kind='file', restore=True))
    value = dict(version=1, mode='rehearsal', approved=True, approval='synthetic-owner-gate', home=str(home),
                 candidate='synthetic-candidate', prior='synthetic-prior', inventory=dict(
                     members=items, raw_path='raw', bridge=dict(mode='stopped'),
                     gateway_jobs=0, expected_gateway_jobs=0, units=['overseer', 'gateway', 'bridge', 'a2a'], timers=['watch'],
                     host_crontab=dict(window_safe=True,timezone='UTC',window_start_ms=1791532800000,window_end_ms=1791534600000,danger_minutes=[240])), output=str(tmp_path / 'acquisition'),
                 receipts=str(tmp_path / 'receipts'), commands={})
    value['digest'] = procedure().record_digest(value)
    path = tmp_path / 'record.json'
    path.write_text(json.dumps(value))
    return path, home, value


class Controls:
    mode = 'rehearsal'
    def __init__(self, module, *, fail=None, lag=False):
        self.module, self.fail, self.lag = module, fail, lag
        self.calls = []
    def __call__(self, action, payload):
        self.calls.append(action)
        if action == self.fail:
            raise RuntimeError('synthetic failure')
        result = dict(ok=True, complete=True, fenced=True, suppressed=True, clean=True,
                      alert_fired=False, generation=1, reopened=True, raw_deferred=0,
                      bridge_pending=int(self.lag), bridge_inflight=0, capture_ready=True,
                      all_committed=not self.lag, current_denials=True, delta_applied=True,
                      no_duplicate_effects=True, unknown_effects_held=True, imports_verified=True, writers_absent=True, bridge_stopped=True, prior_pauses_preserved=True, sources=payload.get('sources', []))
        if action.startswith('smoke-reader-'):
            result.update(adapter=True, lease_closed=True, unselected_refused=True,
                          config_digest=self.module.record_digest(payload['selection']),
                          sources=payload.get('sources', []))
        return result


@pytest.mark.parametrize('failure', ['import', 'prepare-final-owner', 'publish-v3', 'configure-retirement', *(f'select-{f}' for f in ('knowledge','whatsapp','responder','tools','participation','secondary')), 'start-gateway'])
def test_cutover_sequence_blocks_restart_until_verified_set(tmp_path, failure):
    m = procedure()
    path, home, _ = record(tmp_path)
    c = Controls(m, fail=failure)
    with m.injected_controls(c):
        result = m.run_cutover(record=path, home=home, apply=True)
    assert not result['ok'] and result['fenced']
    if failure != 'start-gateway':
        assert not any(call.startswith('start-') for call in c.calls)
    assert 'release-fence' not in c.calls
    before = list(c.calls)
    with m.injected_controls(c), pytest.raises(ValueError, match='no_retry'):
        m.run_cutover(record=path, home=home, apply=True)
    assert c.calls == before
    assert c.calls.index('stop-overseer-clean') < c.calls.index('stop-gateway')
    assert c.calls.index('verify-restart-suppression') < c.calls.index('import')
    receipts = json.loads(Path(result['receipt']).read_text())
    assert receipts['failed_phase'] == failure
    assert sum(r['action'] == failure for r in receipts['phases']) == 1


def test_cutover_reader_order_and_all_committed_release(tmp_path):
    m = procedure()
    path, home, _ = record(tmp_path)
    assert m.run_cutover(record=path, home=home)['planned']
    assert not (tmp_path / 'receipts').exists()
    with pytest.raises(ValueError):
        m.run_cutover(record=path, home=home, apply=True)
    c = Controls(m, lag=True)
    with m.injected_controls(c):
        result = m.run_cutover(record=path, home=home, apply=True)
    assert not result['ok'] and result['fenced']
    assert 'release-fence' not in c.calls
    assert [x.removeprefix('select-') for x in c.calls if x.startswith('select-')] == list(m.READER_ORDER)
    assert c.calls.index('configure-retirement') < c.calls.index('deploy') < c.calls.index('start-gateway')
    # A separate successful attempt needs a fresh record/acquisition, never retries the failed one.
    second = tmp_path / 'second'
    second.mkdir()
    path, home, _ = record(second)
    c = Controls(m)
    with m.injected_controls(c):
        result = m.run_cutover(record=path, home=home, apply=True)
    assert result['ok'] and not result['fenced']
    assert c.calls.index('all-committed-barrier') < c.calls.index('release-fence') < c.calls.index('start-overseer') < c.calls.index('start-timers')


def test_cutover_whole_set_restore_preserves_layer1_and_revocations(tmp_path):
    m = procedure()
    path, home, value = record(tmp_path)
    prior = m.acquire_cutover_snapshot(home=home, output=Path(value['output']), inventory=value['inventory'])
    assert prior['ok']
    for name in ('knowledge.db','cron.json','outbox','disposition'):
        (home / name).write_text('new ' + name)
    (home / 'raw/tail').write_text('old\nnew\npurged\n')
    (home / 'spool/pending').write_text('new capture')
    (tmp_path/'installed-text').write_text('new text')
    c = Controls(m)
    with m.injected_controls(c):
        result = m.restore_prior_set(record=path, home=home, failed_snapshot=tmp_path / 'failed', apply=True)
    assert result['ok']
    assert (home / 'knowledge.db').read_bytes() == b'prior knowledge'
    assert (tmp_path/'installed-text').read_text() == 'prior owner text'
    assert (home / 'cron.json').read_text() == '{"jobs":[]}'
    assert (home / 'raw/tail').read_text() == 'old\nnew\npurged\n'
    assert (home / 'spool/pending').read_text() == 'new capture'
    assert (home / 'outbox').read_text() == 'new outbox'
    assert (home / 'disposition').read_text() == 'new disposition'
    assert c.calls.index('capture-suppression-delta') < c.calls.index('reapply-suppression-delta') < c.calls.index('verify-current-denials') < c.calls.index('start-gateway')
    assert c.calls.index('verify-effect-deduplication') < c.calls.index('release-fence')
    assert (tmp_path / 'failed/manifest.json').exists()
    # Missing safe delta authority keeps restored readers fenced.
    third = tmp_path / 'third'
    third.mkdir()
    path, home, value = record(third)
    m.acquire_cutover_snapshot(home=home, output=Path(value['output']), inventory=value['inventory'])
    c = Controls(m, fail='reapply-suppression-delta')
    with m.injected_controls(c):
        result = m.restore_prior_set(record=path, home=home, failed_snapshot=third / 'failed', apply=True)
    assert not result['ok'] and result['fenced']
    assert 'start-gateway' not in c.calls


def test_snapshot_sqlite_backup_counts_intervals_and_symlink_refusal(tmp_path):
    m = procedure()
    home = tmp_path / 'home'
    home.mkdir()
    with sqlite3.connect(home / 'copy.db') as db:
        db.execute('CREATE TABLE statement(value TEXT)')
        db.execute("INSERT INTO statement VALUES ('synthetic')")
    inventory = dict(members=[dict(path='copy.db', kind='sqlite', restore=True)], bridge=dict(mode='stopped'))
    receipt = m.acquire_cutover_snapshot(home=home, output=tmp_path / 'copy', inventory=inventory)
    item = receipt['members'][0]
    assert item['counts'] == {'statement': 1}
    assert item['started_ns'] <= item['ended_ns']
    assert item['integrity'] and item['sha256']
    (home / 'outside').symlink_to(tmp_path / 'other')
    inventory['members'].append(dict(path='outside', kind='file', restore=True))
    with pytest.raises(ValueError):
        m.acquire_cutover_snapshot(home=home, output=tmp_path / 'refused', inventory=inventory)
    assert not (tmp_path / 'refused').exists()


def test_final_owner_package_targets_only_native_segment_and_final_offsets(tmp_path):
    m = procedure()
    stage, raw = tmp_path / 'stage', tmp_path / 'raw'
    (stage / 'backfill').mkdir(parents=True)
    original = dict(id=12019, uuid='synthetic-row', content='synthetic original')
    rows = []
    for store in ('memory', 'preserved'):
        rows.append(backfill_line(channel='whatsapp', kind='message', provenance='verbatim_unverified',
            time_certainty='capture_time_approx', occurred_ms=123, direction='in', chat_id='synthetic@g.us',
            payload={'messageId':'native-owning','segments':[{'text': 'synthetic', 'senderId':'10002'} for _ in range(6)] + [{'text':'synthetic','senderId':'10002','messageId':'native-owning'}]},
            origin=Origin(store,'copy.db','memory2_nodes','12019'), original=original))
    (stage / 'backfill/memory.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in rows))
    (stage / 'derived').mkdir()
    (stage / 'derived/media-descriptions.jsonl').write_text(json.dumps(dict(kind='media_description',channel='whatsapp', chat_id='synthetic@g.us',native_message_id='native-owning',text='synthetic'))+'\n')
    manifest = prepare_import_manifest(stage)
    (raw / 'derived').mkdir(parents=True)
    (raw / 'derived/media-descriptions.jsonl').write_text(json.dumps(dict(kind='media_description',channel='whatsapp',chat_id='synthetic@g.us',native_message_id='other',text='synthetic old'))+'\n')
    manifest['import_receipt'] = import_backfill(raw, stage, manifest)
    row = manifest['files']['backfill/memory.jsonl']['rows'][0]
    import hashlib

    from tests.gateway.convhist.hist_fixtures import _raw, write_jsonl
    edit = _raw('edit','edit',dict(chatJid='synthetic@g.us',messageId='native-owning',senderId='10002@s.whatsapp.net',text='synthetic edited'))
    edit['chat_id'] = 'synthetic@g.us'
    write_jsonl(raw/'whatsapp/synthetic.jsonl',[edit])
    edit_hash = hashlib.sha256((raw/'whatsapp/synthetic.jsonl').read_bytes()).hexdigest()
    decisions = [dict(record=make('contact',1,'synthetic',identifiers=['10001@s.whatsapp.net'])),
        dict(record=make('author',2,'synthetic',source_ref='backfill/placeholder.jsonl#1',anchor='10001@s.whatsapp.net'),
             locator=dict(uuid='synthetic-row', original_row_sha256=row['original_row_sha256'], native_id='native-owning', segment=6, all_copies=True)),
        dict(record=make('author',3,'synthetic edit',source_ref='whatsapp/synthetic.jsonl#1',anchor='10001@s.whatsapp.net'), locator=dict(source_ref='whatsapp/synthetic.jsonl#1', sha256=edit_hash)),
        dict(case=12023, skipped=True), dict(case=12024, skipped=True)]
    output = tmp_path / 'owner.json'
    result = m.prepare_final_owner_package(conversion_manifest=manifest, reviewed_decisions=decisions, raw_root=raw, output=output)
    package = [json.loads(line) for line in output.read_text().splitlines()]
    targets = [r['source_ref'] for r in package if r['type']=='author']
    assert targets == ['backfill/memory.jsonl#1/6','backfill/memory.jsonl#2/6','whatsapp/synthetic.jsonl#1']
    assert manifest['import_receipt']['ref_map']['derived/media-descriptions.jsonl#1'] == 'derived/media-descriptions.jsonl#2'
    assert not any(t.endswith('/0') for t in targets)
    assert result['records'] == 4
    assert all(r.get('case') not in (12023,12024) for r in package)
    assert json.loads((raw/'backfill/memory.jsonl').read_text().splitlines()[0])['payload']['segments'][0]['senderId'] == '10002'
    decisions[1]['locator']['original_row_sha256'] = '0'*64
    with pytest.raises(ValueError):
        m.prepare_final_owner_package(conversion_manifest=manifest, reviewed_decisions=decisions, raw_root=raw, output=tmp_path/'bad.json')


@pytest.mark.perf
async def test_cutover_worker_lock_measurement_is_numeric():
    import asyncio
    import copy

    from tests.gateway.convhist import test_hist_consumer_perf as perf
    holds, waits = [], []
    lock = perf.MeasuredOperationLock(asyncio.Lock(), holds, waits)
    acquired, release = asyncio.Event(), asyncio.Event()
    async def worker():
        async with lock:
            acquired.set()
            await release.wait()
    async def reply():
        async with lock:
            pass
    task = asyncio.create_task(worker(), name='cutover-worker-measurement')
    await asyncio.wait_for(acquired.wait(), 30)
    other = asyncio.create_task(reply(), name='cutover-reply-measurement')
    await asyncio.sleep(0)
    assert not other.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(other, 30)
    assert len(holds) == len(waits) == 1 and holds[0] >= 0 and waits[0] >= 0
    assert not lock.locked()
    report = perf.empty_report()
    report['worker_snapshot_lock_hold_ms'] = perf.distribution(holds)
    report['reply_barrier_wait_ms'] = perf.distribution(waits)
    perf.validate_report(report)
    for key in ('worker_snapshot_lock_hold_ms','reply_barrier_wait_ms'):
        leaked = copy.deepcopy(report)
        leaked[key]['max'] = 'synthetic-private-id'
        with pytest.raises(ValueError):
            perf.validate_report(leaked)


def test_offline_smoke_uses_real_adapter_and_rejects_stale_vector(tmp_path):
    from types import SimpleNamespace

    from yeoman_gateway.adapters.reply_archive_history import HistoryReplyArchiveAdapter
    from yeoman_gateway.history.export import secondary_archive
    from yeoman_gateway.history.live import HistoryPaused
    from yeoman_gateway.history.project import project
    from yeoman_gateway.history.queries import HistoryQueries

    from tests.gateway.convhist.hist_fixtures import _raw, write_jsonl
    m = procedure()
    raw, db = tmp_path/'raw', tmp_path/'history.db'
    chat, phone = 'synthetic@g.us', '10001@s.whatsapp.net'
    now = 1_791_000_000_000
    rows = [_raw('membership_snapshot','membership_snapshot', dict(chatJid=chat,messageId='roster',timestamp=now-1000,participants=[phone],complete=True),received=now-1000),
            _raw('message','message',dict(chatJid=chat,messageId='native',senderId=phone,text='synthetic',timestamp=now),received=now)]
    for row in rows:
        row['chat_id'] = chat
    write_jsonl(raw/'whatsapp/synthetic.jsonl', rows)
    project([raw],db)
    selection = dict(legacyWritersDisabled=True,liveProjectionEnabled=True, readers=dict.fromkeys(m.READER_ORDER,False))
    selection['readers']['whatsapp'] = True
    leases = []
    def probe(snapshot):
        leases.append(snapshot)
        q = HistoryQueries(snapshot)
        adapter = HistoryReplyArchiveAdapter(q)
        message = adapter.lookup_message('whatsapp',chat,'native')
        assert message.text == 'synthetic'
        assert q.mention(phone,chat_id=chat,at_ms=now) == phone
        with pytest.raises(HistoryPaused):
            secondary_archive(None,SimpleNamespace(live_projection_enabled=True,legacy_writers_disabled=True,readers=SimpleNamespace(secondary=False)))
        return dict(reply_identity=True,canonical_mentions=True,unselected_refused=True)
    receipt = m.offline_reader_smoke(family='whatsapp',db_path=db,raw_root=raw,selection=selection,probe=probe)
    assert receipt['lease_closed'] and leases[0]._closed
    assert receipt['config_digest'] == m.record_digest(selection)
    with pytest.raises(ValueError):
        m.offline_reader_smoke(family='tools',db_path=db,raw_root=raw,selection=selection,probe=probe)
    with (raw/'whatsapp/synthetic.jsonl').open('a') as stream:
        stream.write(json.dumps(rows[-1])+'\n')
    with pytest.raises(ValueError,match='vector_unverified'):
        m.offline_reader_smoke(family='whatsapp',db_path=db,raw_root=raw,selection=selection,probe=probe)
    assert len(leases) == 1


@pytest.mark.perf
async def test_cutover_worker_lock_real_thread_cleanup(consumer_case):
    from tests.gateway.convhist import test_hist_consumer_perf as perf
    c = consumer_case
    holds, waits = [], []
    perf.instrument_operation_lock(c.projector, holds, waits)
    await perf.measure_concurrent_reply(c.projector)
    assert holds and waits and all(v >= 0 for v in (*holds,*waits))
    assert not c.projector._operation_lock.locked()
    c.assert_no_leases()


def test_preparation_controls_use_final_snapshot_and_exact_protected_commands(tmp_path):
    m = procedure()
    home = tmp_path / 'final-snapshot'
    home.mkdir()
    staged = tmp_path / 'staged'
    value = dict(output=str(home), inventory=dict(extra_bridge_dirs=[]),
                 python='/synthetic/python', layout=dict(staged=str(staged),
                 conversion_manifest=str(tmp_path/'conversion.json'), owner_package=str(tmp_path/'owner.jsonl')))
    def no_host(action, payload):
        raise AssertionError('conversion reached host controls')
    control = m.preparation_controls(host=no_host, probes={})
    receipt = control('convert',dict(record=value,receipts=[]))
    assert receipt['ok'] and receipt['complete']
    manifest = json.loads((tmp_path/'conversion.json').read_text())
    assert receipt['manifest_digest'] == manifest['package_digest']
    assert set(manifest['files']) >= {'backfill/journal.jsonl','backfill/knowledge.jsonl','derived/media-descriptions.jsonl'}
    assert m.command_for('import',value) == ['/synthetic/python','-m','yeoman_gateway','history','import-backfill','--staged',str(staged),'--manifest',str(tmp_path/'conversion.json'),'--confirm']
    assert m.command_for('owner-preview',value)[-3:] == ['--file',str(tmp_path/'owner.jsonl'),'--dry-run']


async def test_all_six_offline_smokes_exercise_selected_adapters(statement_case, tmp_path):
    import asyncio
    from dataclasses import replace
    from types import SimpleNamespace

    from yeoman_gateway.adapters.reply_archive_history import HistoryReplyArchiveAdapter
    from yeoman_gateway.agent.tools.recall_conversation import RecallConversationTool
    from yeoman_gateway.history.context import history_knowledge_scope, history_turn
    from yeoman_gateway.history.export import (
        read_history_turn,
        request_history_read,
        secondary_archive,
    )
    from yeoman_gateway.history.live import HistoryPaused
    from yeoman_gateway.history.queries import HistoryQueries
    from yeoman_gateway.knowledge.models import KnowledgeError, RecallQuery
    from yeoman_gateway.processing.participation import ParticipationOpportunity
    from yeoman_gateway.processing.participation_context import ParticipationContextBuilder
    from yeoman_gateway.processing.tool_context import (
        ToolInvocationContext,
        reset_tool_context,
        set_tool_context,
    )
    from yeoman_gateway.session.manager import SessionManager
    from yeoman_gateway.session.operational import OperationalSessions

    from tests.gateway.test_history_capture_continuity import GROUP, MS, PHONE
    from tests.gateway.test_participation_context import _inputs
    m, c = procedure(), statement_case
    await c.publish(old=True, author_only=False)
    await c.curate()
    c.append('message','smoke-media',ms=MS+1,text='Synthetic media',media={'type':'image'})
    await c.settle()
    before_extractions = list(c.seen)
    # These are explicit synthetic owner rights, separate from source authorship.
    c.policy.admin_principals = frozenset({c.author})
    await c.projector.stop()
    selection = dict(legacyWritersDisabled=True,liveProjectionEnabled=True,readers=dict.fromkeys(m.READER_ORDER,False))
    receipts = []
    for family in m.READER_ORDER:
        selection['readers'][family] = True
        def probe(snapshot):
            async def composition():
                async def borrowed():
                    return snapshot
                offline = SimpleNamespace(read_turn=borrowed,health=lambda:dict(status='ready',generation=snapshot.generation))
                async with history_turn(offline):
                    with history_knowledge_scope(snapshot,c.knowledge):
                        q = HistoryQueries(snapshot)
                        archive = HistoryReplyArchiveAdapter(q)
                        context = c.read_context(c.author)
                        result = dict(unselected_refused=True)
                        # Exercise real retirement refusals, not only configuration flags.
                        blocked = SimpleNamespace(live_projection_enabled=True,legacy_writers_disabled=True,readers=SimpleNamespace(secondary=False))
                        with pytest.raises(HistoryPaused):
                            secondary_archive(None,blocked)
                        if family == 'knowledge':
                            assert c.knowledge._authority.verify_source(c.source)
                            assert c.knowledge.recall(RecallQuery('Curated'),context=context).statement_ids == (c.statement_id,)
                            result.update(mapped_source=True,curated_disclosure=True)
                        elif family == 'whatsapp':
                            assert archive.lookup_message('whatsapp',GROUP,'statement-source') is not None
                            assert q.mention(PHONE,chat_id=GROUP,at_ms=MS+1000) == PHONE
                            result.update(reply_identity=True,canonical_mentions=True)
                        elif family == 'responder':
                            ops = OperationalSessions(tmp_path/'offline-sessions.db')
                            try:
                                sessions = SessionManager(tmp_path,sessions_dir=tmp_path/'frozen-sessions',history_selected=True,legacy_history_disabled=True,operational_store=ops)
                                assert sessions.recent_history(channel='whatsapp',chat_id=GROUP,snapshot=snapshot,limit=20)
                                session = sessions.get_or_create('whatsapp:'+GROUP,channel='whatsapp',chat_id=GROUP,history_snapshot=snapshot)
                                session.add_boundary()
                                assert not session.get_history()
                                assert not (tmp_path/'frozen-sessions').exists()
                            finally:
                                ops.close()
                            result.update(recent_window=True,operational_new=True)
                        elif family == 'tools':
                            tool = RecallConversationTool(None)
                            tool._history_selected = True
                            token = set_tool_context(ToolInvocationContext(channel='whatsapp',chat_id=GROUP,history_snapshot=snapshot))
                            try:
                                assert 'Found' in await tool.execute(query='Synthetic')
                                assert q.media(chat_id=GROUP,limit=10)
                                with pytest.raises(KnowledgeError):
                                    read_history_turn(snapshot,context=context,chat_ids=('unauthorized@g.us',),after_ms=0,limit=10)
                            finally:
                                reset_tool_context(token)
                            result.update(fts=True,media=True,unauthorized_denial=True)
                        elif family == 'participation':
                            builder = ParticipationContextBuilder(archive=None,policy=c.policy.engine,source_authorizer=lambda row:True)
                            builder._history_selected = True
                            opportunity = ParticipationOpportunity(opportunity_id='synthetic-smoke',channel='whatsapp',chat_id=GROUP,trigger='inbound',source_event_ids=('statement-source',),observed_revision=1,activation_epoch=1,created_at_ms=MS)
                            value = await builder.build(opportunity,inputs=_inputs(current_source_ids=('statement-source',)),now_ms=MS+1000)
                            assert value and q.audience(c.message_id).status == 'known'
                            result.update(ambient=True,audience_generation=True)
                        else:
                            value = read_history_turn(snapshot,context=replace(context,owner=True),chat_ids=(GROUP,),after_ms=0,limit=10)
                            assert value['count'] <= 10 and 'messages' not in value
                            with pytest.raises(HistoryPaused):
                                from yeoman_gateway.history.export import read_history_export
                                await read_history_export(offline,context=context,chat_ids=(GROUP,),after_ms=0,limit=10)
                            # request_history_read refuses before creating an AF_UNIX client.
                            import os
                            prior = os.environ.get('YEOMAN_HISTORY_TOOL_TURN')
                            os.environ['YEOMAN_HISTORY_TOOL_TURN'] = '1'
                            try:
                                with pytest.raises(HistoryPaused):
                                    await request_history_read(tmp_path/'must-not-exist.sock',{})
                            finally:
                                if prior is None:
                                    os.environ.pop('YEOMAN_HISTORY_TOOL_TURN')
                                else:
                                    os.environ['YEOMAN_HISTORY_TOOL_TURN'] = prior
                            result.update(bounded_owner_export=True,in_turn_subprocess_refusal=True)
                        return result
            return asyncio.run(composition())
        receipt = await asyncio.to_thread(m.offline_reader_smoke,family=family,db_path=c.path,raw_root=c.raw,selection=selection,probe=probe)
        receipts.append(receipt)
    assert len(receipts) == 6 and all(r['lease_closed'] for r in receipts)
    assert len({r['generation'] for r in receipts}) == 1
    assert c.seen == before_extractions


def test_cutover_window_refuses_host_session_killer_overlap(tmp_path):
    from datetime import UTC, datetime
    m = procedure()
    path, home, value = record(tmp_path)
    value['inventory']['host_crontab'] = dict(window_safe=True, timezone='Europe/Berlin',
        window_start_ms=int(datetime(2026,10,9,1,55,tzinfo=UTC).timestamp()*1000),
        window_end_ms=int(datetime(2026,10,9,2,10,tzinfo=UTC).timestamp()*1000), danger_minutes=[240])
    value['digest'] = m.record_digest(value)
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError,match='crontab_window'):
        m.run_cutover(record=path,home=home)
    assert not (tmp_path/'receipts').exists()


async def test_whole_restore_does_not_revive_purged_knowledge_source(statement_case, tmp_path):
    import asyncio
    from contextlib import closing

    from yeoman_gateway.history.context import history_knowledge_scope
    from yeoman_gateway.history.live import HistoryBoundary
    from yeoman_gateway.history.reader import HistoryReader
    from yeoman_gateway.knowledge.api import open_knowledge_store
    from yeoman_gateway.knowledge.models import RecallQuery
    from yeoman_shared.raw_archive.records import enumerate_committed
    m, c = procedure(), statement_case
    await c.publish(old=True,author_only=False)
    await c.curate()
    knowledge_path = Path(c.knowledge._store._conn.execute('PRAGMA database_list').fetchone()[2])
    legacy = c.knowledge._legacy_authority
    (tmp_path/'cutover').mkdir()
    path, home, value = record(tmp_path/'cutover')
    value['inventory']['members'] = [dict(path='knowledge.db',source=str(knowledge_path),kind='sqlite',restore=True),
        dict(path='raw',source=str(c.raw),kind='tree',restore=False,role='raw')]
    value['digest'] = m.record_digest(value)
    path.write_text(json.dumps(value))
    await c.projector.stop()
    prior = m.acquire_cutover_snapshot(home=home,output=Path(value['output']),inventory=value['inventory'])
    assert prior['sources']
    await c.projector.start()
    await c.projector._startup_task
    await c.dispose('purge')
    c.knowledge.history_source_ledger.revoke(*c.source.key,reason='synthetic authorized purge')
    await c.projector.stop()
    c.knowledge.close()
    raw_after_purge = {p.relative_to(c.raw).as_posix():p.read_bytes() for p in c.raw.rglob('*.jsonl')}
    control = Controls(m)
    def host(action,payload):
        if action == 'reapply-suppression-delta':
            c.knowledge = open_knowledge_store(knowledge_path,workspace_id='synthetic',source_authority=legacy,policy_authority=c.policy,history_mode=True)
        if action in ('reapply-suppression-delta','verify-current-denials'):
            with closing(sqlite3.connect(c.path.as_uri()+'?mode=ro',uri=True)) as db:
                generation = json.loads(db.execute("SELECT state_json FROM projector_state WHERE file='@runtime'").fetchone()[0])['generation']
            reader = HistoryReader(c.path)
            snapshot = reader.open_snapshot(HistoryBoundary(generation,enumerate_committed(c.raw)))
            try:
                with history_knowledge_scope(snapshot,c.knowledge):
                    if action == 'reapply-suppression-delta':
                        c.knowledge.invalidate_event_sources([c.source.event_id])
                    assert not c.knowledge._authority.verify_source(c.source)
                    assert c.knowledge.recall(RecallQuery('Curated'),context=c.read_context(c.author)).statement_ids == ()
            finally:
                reader.close()
        return control(action,payload)
    host.mode = 'rehearsal'
    with m.injected_controls(host):
        result = await asyncio.to_thread(m.restore_prior_set,record=path,home=home,failed_snapshot=tmp_path/'failed-purge',apply=True)
    assert result['ok'] and result['fence_verified']
    assert {p.relative_to(c.raw).as_posix():p.read_bytes() for p in c.raw.rglob('*.jsonl')} == raw_after_purge
