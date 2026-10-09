"""Observed host controls: subprocesses are always injected fakes."""
import importlib.util
import json
import sys
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from tests.gateway.test_history_cutover import Controls, procedure, record


def host_module():
    scripts = str(Path(__file__).parents[2] / 'scripts')
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    spec = importlib.util.spec_from_file_location('cutover_host', Path(scripts) / 'history_cutover_host.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Clock:
    now = 100.0
    def monotonic(self):
        return self.now
    def sleep(self, seconds):
        assert seconds >= 30
        self.now += seconds


class Runner:
    def __init__(self):
        self.calls = []
        self.states = {}
        self.ignore_stop = False
        self.ignore_mask = False
        self.alert = 0
    def __call__(self, argv):
        self.calls.append(argv)
        if argv[:3] == ['systemctl', '--user', 'show']:
            unit = argv[3]
            state = self.states.setdefault(unit, {'ActiveState': 'inactive', 'Result': 'success', 'UnitFileState': 'enabled', 'Restart': 'always', 'ActiveEnterTimestampMonotonic': '0', 'ExecMainStartTimestampMonotonic': '0', 'ExecMainExitTimestampMonotonic': '0'})
            if unit == 'yeoman-overseer-alert.service':
                state['ActiveEnterTimestampMonotonic'] = str(self.alert)
            return CompletedProcess(argv, 0, '\n'.join(f'{k}={v}' for k, v in state.items()), '')
        if argv[:2] == ['systemctl', '--user']:
            operation = argv[2]
            unit = argv[-1]
            state = self.states.setdefault(unit, {'ActiveState': 'inactive', 'Result': 'success', 'UnitFileState': 'enabled', 'Restart': 'always', 'ActiveEnterTimestampMonotonic': '0', 'ExecMainStartTimestampMonotonic': '0', 'ExecMainExitTimestampMonotonic': '0'})
            if operation == 'stop' and not self.ignore_stop:
                state['ActiveState'] = 'inactive'
            if operation == 'start':
                state['ActiveState'] = 'active'
            if operation == 'mask' and not self.ignore_mask:
                state['UnitFileState'] = 'masked-runtime'
            if operation == 'unmask':
                state['UnitFileState'] = 'enabled'
        return CompletedProcess(argv, 0, '{}', '')


def inventory():
    return dict(units=[dict(name=f'yeoman-{name}.service', restart='always', executable=f'/synthetic/{name}') for name in ('overseer', 'gateway', 'bridge', 'a2a')], timers=['watch.timer'], manual_routes=['manual.service'], gateway_unit='yeoman-gateway.service', bridge_unit='yeoman-bridge.service')


def payload(tmp_path):
    return dict(home=str(tmp_path), record=dict(receipts=str(tmp_path / 'receipts'), digest='synthetic', layout={}), sources=[], selection={})


@pytest.mark.parametrize(('action', 'units'), [
    ('stop-overseer-clean', ['yeoman-overseer.service']),
    ('stop-gateway', ['yeoman-gateway.service']),
    ('stop-bridge', ['yeoman-bridge.service']),
    ('stop-timers-and-manual-routes', ['watch.timer', 'manual.service', 'yeoman-a2a.service']),
    ('start-bridge', ['yeoman-bridge.service']),
    ('start-gateway', ['yeoman-gateway.service']),
    ('start-overseer', ['yeoman-overseer.service']),
    ('start-timers', ['watch.timer']),
    ('resume-vetted-manual-routes', ['manual.service', 'yeoman-a2a.service']),
])
def test_exact_service_argv(tmp_path, action, units):
    runner = Runner()
    control = host_module().live_host_controls(inventory=inventory(), runner=runner, clock=Clock())
    result = control(action, payload(tmp_path))
    assert result['ok'] and result['mode'] == 'live'
    mutations = [a for a in runner.calls if a[2] != 'show']
    expected = [['systemctl', '--user', 'stop', u] for u in units] if action.startswith('stop-') else [a for u in units for a in (['systemctl', '--user', 'unmask', '--runtime', u], ['systemctl', '--user', 'start', u])]
    assert mutations == expected


def test_success_exit_does_not_prove_stopped_or_masked(tmp_path):
    runner = Runner()
    control = host_module().live_host_controls(inventory=inventory(), runner=runner, clock=Clock())
    runner('systemctl --user start yeoman-gateway.service'.split())
    runner.ignore_stop = True
    assert not control('stop-gateway', payload(tmp_path))['ok']
    runner.ignore_mask = True
    assert not control('suppress-restarts', payload(tmp_path))['suppressed']
    assert not control('verify-deploy-suppression', payload(tmp_path))['ok']


def test_overseer_alert_is_observed_even_after_successful_stop(tmp_path):
    runner = Runner()
    runner.alert = 100_000_001
    result = host_module().live_host_controls(inventory=inventory(), runner=runner, clock=Clock())('stop-overseer-clean', payload(tmp_path))
    assert result['clean'] and result['alert_fired'] and not result['ok']


def test_restart_suppression_observes_thirty_seconds(tmp_path):
    runner, clock = Runner(), Clock()
    control = host_module().live_host_controls(inventory=inventory(), runner=runner, clock=clock)
    assert control('suppress-restarts', payload(tmp_path))['suppressed']
    masks = [a for a in runner.calls if a[2] == 'mask']
    assert masks == [['systemctl', '--user', 'mask', '--runtime', u['name']] for u in inventory()['units']]
    assert clock.now >= 130
    assert control('verify-restart-suppression', payload(tmp_path))['suppressed']
    assert clock.now >= 160


def test_rehearsal_refuses_runtime_before_content_and_never_calls_runner(tmp_path):
    runner = Runner()
    with pytest.raises(ValueError):
        host_module().rehearsal_host_controls(copy_home=Path('/home/dm/.yeoman'), inventory=inventory(), runner=runner)
    control = host_module().rehearsal_host_controls(copy_home=tmp_path, inventory=inventory(), runner=runner)
    result = control('start-timers', payload(tmp_path))
    assert result == dict(ok=True, mode='rehearsal', simulated=True, units=['watch.timer'])
    assert runner.calls == []
    with pytest.raises(ValueError, match='unknown'):
        control('invented', payload(tmp_path))


@pytest.mark.parametrize(('record_mode', 'control_mode'), [('live', 'rehearsal'), ('rehearsal', 'live')])
def test_control_mode_mismatch_refused(tmp_path, record_mode, control_mode):
    m = procedure()
    path, home, value = record(tmp_path)
    value['mode'] = record_mode
    value['digest'] = m.record_digest(value)
    path.write_text(json.dumps(value))
    controls = Controls(m)
    controls.mode = control_mode
    with m.injected_controls(controls), pytest.raises(ValueError, match='mode'):
        m.run_cutover(record=path, home=home, apply=True)


def test_live_confirmation_token_and_expected_job_count(tmp_path):
    m = procedure()
    path, home, value = record(tmp_path)
    value.update(mode='live', confirmation_token=str(tmp_path / 'token'))
    value['inventory'].update(gateway_jobs=2, expected_gateway_jobs=2)
    value['digest'] = m.record_digest(value)
    path.write_text(json.dumps(value))
    c = Controls(m)
    c.mode = 'live'
    with m.injected_controls(c), pytest.raises(ValueError, match='confirmation'):
        m.run_cutover(record=path, home=home, apply=True)
    (tmp_path / 'token').write_text(value['digest'])
    with m.injected_controls(c):
        assert m.run_cutover(record=path, home=home, apply=True)['ok']
    assert c.calls[-1] == 'start-timers'


@pytest.mark.parametrize('action', ['invented', 'select-invented', 'smoke-reader-invented'])
def test_unknown_live_action_refused(tmp_path, action):
    with pytest.raises(ValueError, match='unknown'):
        host_module().live_host_controls(inventory=inventory(), runner=Runner())(action, payload(tmp_path))


def data_home(tmp_path):
    import sqlite3
    home = tmp_path / 'copy'
    home.mkdir()
    (home / 'raw').mkdir()
    history = home / 'history.db'
    with sqlite3.connect(history) as db:
        db.execute('CREATE TABLE projector_state(file TEXT,state_json TEXT,lines INTEGER,end_offset INTEGER,sha256 TEXT)')
        db.execute('INSERT INTO projector_state(file,state_json) VALUES (?,?)', ('@runtime', json.dumps(dict(status='ready', generation=3))))
    knowledge = home / 'knowledge.db'
    with sqlite3.connect(knowledge) as db:
        db.execute('CREATE TABLE knowledge_meta(key TEXT,value TEXT)')
        db.execute("INSERT INTO knowledge_meta VALUES ('schema_version','3')")
        db.execute('CREATE TABLE knowledge_history_capture_state(key TEXT,value_json TEXT,version INTEGER)')
        db.execute("INSERT INTO knowledge_history_capture_state VALUES ('handover',?,1)", (json.dumps(dict(version=1, generation=3, sources=[])),))
    p = payload(home)
    p['record'].update(python='/synthetic/python', layout=dict(history=str(history), raw=str(home/'raw'), knowledge_live=str(knowledge)))
    return home, p


def test_live_barrier_uses_observed_stores_and_counters(tmp_path):
    home, p = data_home(tmp_path)
    runner = Runner()
    original = runner.__call__
    def run(argv):
        if argv[1:] == ['-m', 'yeoman_gateway', 'raw', 'status', '--json']:
            runner.calls.append(argv)
            return CompletedProcess(argv, 0, json.dumps(dict(writer=dict(state='ok', spooled=0, pending_in_memory=0))), '')
        return original(argv)
    from yeoman_shared.whatsapp_protocol import PROTOCOL_VERSION
    bridge = dict(outbox=dict(pending=0), queue=dict(inflight=0), whatsapp=dict(connected=True), protocolVersion=PROTOCOL_VERSION)
    requests = []
    def ipc(request):
        requests.append(request)
        return dict(status='ok', health=dict(status='ready', generation=3, lag_lines=0, lag_bytes=0))
    control = host_module().live_host_controls(inventory=inventory(), runner=run, ipc=ipc, bridge_probe=lambda: bridge)
    proof = control('all-committed-barrier', p)
    assert proof['ok'] and proof['reopened'] and proof['capture_ready']
    assert proof['generation'] == 3 and proof['sources'] == []
    assert requests == [dict(cmd='history_control', args=dict(operation='status'))]
    assert runner.calls == [['/synthetic/python', '-m', 'yeoman_gateway', 'raw', 'status', '--json']]
    bridge['outbox']['pending'] = 1
    assert not control('drain-durable-tails', p)['ok']


def test_rehearsal_barrier_reads_copy_and_refuses_external_layout(tmp_path):
    home, p = data_home(tmp_path)
    runner = Runner()
    inv = dict(inventory(), raw_status_path='raw-state.json', bridge_status_path='bridge-state.json')
    (home/'raw-state.json').write_text(json.dumps(dict(state='ok', spooled=0, pending_in_memory=0)))
    (home/'bridge-state.json').write_text(json.dumps(dict(outbox=dict(pending=0), queue=dict(inflight=0))))
    control = host_module().rehearsal_host_controls(copy_home=home, inventory=inv, runner=runner)
    assert control('all-committed-barrier', p)['ok']
    (home/'raw-state.json').write_text(json.dumps(dict(state='ok', spooled=1, pending_in_memory=0)))
    assert not control('all-committed-barrier', p)['ok']
    p['record']['layout']['raw'] = str(tmp_path/'outside')
    with pytest.raises(ValueError, match='outside_copy'):
        control('all-committed-barrier', p)
    assert runner.calls == []


def test_owner_ack_is_pinned_and_never_auto_success(tmp_path):
    p = payload(tmp_path)
    c = host_module().live_host_controls(inventory=inventory(), runner=Runner())
    with pytest.raises(FileNotFoundError):
        c('functional-smoke', p)
    root = Path(p['record']['receipts'])
    root.mkdir()
    file = root/'functional-smoke.owner_ack.json'
    file.write_text(json.dumps(dict(action='functional-smoke', record_digest='wrong', owner_ack=True)))
    with pytest.raises(ValueError, match='owner_ack'):
        c('functional-smoke', p)
    file.write_text(json.dumps(dict(action='functional-smoke', record_digest='synthetic', owner_ack=True)))
    assert c('functional-smoke', p)['ok']


def test_end_to_end_dry_plan_apply_fake_host_reaches_all_receipts(tmp_path):
    m = procedure()
    path, home, value = record(tmp_path)
    value['mode'] = 'live'
    value['confirmation_token'] = str(tmp_path/'confirmed')
    value['inventory'].update(inventory(), pause_path=str(home/'pauses.json'))
    (home/'pauses.json').write_text('{"synthetic":"owner pause"}')
    value['digest'] = m.record_digest(value)
    path.write_text(json.dumps(value))
    (tmp_path/'confirmed').write_text(value['digest'])
    assert m.run_cutover(record=path, home=home)['planned']
    runner = Runner()
    host = host_module().live_host_controls(inventory=value['inventory'], runner=runner, clock=Clock(), proc_root=tmp_path/'proc')
    (tmp_path/'proc').mkdir()
    data = Controls(m)
    services = host_module().SERVICE_ACTIONS - {'deploy'}
    def composed(action, p):
        return host(action, p) if action in services else data(action, p)
    composed.mode = 'live'
    with m.injected_controls(composed):
        result = m.run_cutover(record=path, home=home, apply=True)
    assert result['ok']
    journal = json.loads(Path(result['receipt']).read_bytes())
    assert [phase['action'] for phase in journal['phases']] == m._sequence(value)
    assert journal['phases'][-1]['action'] == 'start-timers'
    assert all('receipt' in p for p in journal['phases'])
    assert len(list(Path(value['receipts']).glob('cutover-*-started.json'))) == len(journal['phases'])
    assert ['systemctl', '--user', 'start', 'watch.timer'] in runner.calls
    assert (home/'pauses.json').read_text() == '{"synthetic":"owner pause"}'


def test_config_backup_and_retirement_selection(tmp_path):
    home, p = data_home(tmp_path)
    config = home/'synthetic-config.json'
    config.write_text('{"history":{},"owner_extension":"preserve"}')
    before = config.read_bytes()
    p['selection'] = dict(legacyWritersDisabled=True, liveProjectionEnabled=True, readers=dict(knowledge=True))
    inv = dict(inventory(), config_path=str(config))
    c = host_module().rehearsal_host_controls(copy_home=home, inventory=inv)
    result = c('configure-retirement', p)
    assert result['ok']
    assert Path(result['backup']).read_bytes() == before
    assert json.loads(config.read_bytes())['owner_extension'] == 'preserve'
    assert json.loads(config.read_bytes())['history'] == p['selection']


def test_quiescent_refuses_configured_executable_in_proc(tmp_path):
    proc = tmp_path/'proc'
    entry = proc/'42'
    entry.mkdir(parents=True)
    exe = tmp_path/'writer'
    exe.write_text('synthetic executable')
    (entry/'exe').symlink_to(exe)
    (entry/'cmdline').write_bytes(str(exe).encode()+b'\0')
    inv = inventory()
    inv['units'][0]['executable'] = str(exe)
    result = host_module().live_host_controls(inventory=inv, runner=Runner(), proc_root=proc)('verify-quiescent', payload(tmp_path))
    assert not result['ok'] and not result['writers_absent']


def test_deploy_exact_argv_and_observation(tmp_path):
    runner = Runner()
    inv = dict(inventory(), yeoman='/synthetic/yeoman')
    c = host_module().live_host_controls(inventory=inv, runner=runner, clock=Clock(), proc_root=tmp_path)
    p = payload(tmp_path)
    assert c('suppress-restarts', p)['ok']
    assert c('deploy', p)['ok']
    assert ['/synthetic/yeoman', 'deploy'] in runner.calls
    runner.states['yeoman-gateway.service']['ActiveState'] = 'active'
    assert not c('verify-no-writer-after-deploy', p)['ok']


def test_patch_manifest_preserves_owner_text_and_writes_undo(tmp_path):
    import difflib
    import hashlib
    home = tmp_path/'copy'
    target = home/'runbooks/one.md'
    target.parent.mkdir(parents=True)
    before = 'Owner setting: keep\nOld procedure\n'
    after = 'Owner setting: keep\nPrepared procedure\n'
    target.write_text(before)
    manifest = home/'prepared.json'
    manifest.write_text(json.dumps(dict(actions=[dict(path='/synthetic-original/runbooks/one.md', current_sha256=hashlib.sha256(before.encode()).hexdigest(), proposed_sha256=hashlib.sha256(after.encode()).hexdigest(), apply_diff=''.join(difflib.unified_diff(before.splitlines(True), after.splitlines(True), fromfile='before', tofile='after')))])))
    inv = dict(inventory(), original_home='/synthetic-original', prepared_text_manifest=str(manifest), prepared_text_manifest_sha256=hashlib.sha256(manifest.read_bytes()).hexdigest())
    p = payload(home)
    c = host_module().rehearsal_host_controls(copy_home=home, inventory=inv, runner=Runner())
    assert c('apply-prepared-texts', p)['ok']
    assert target.read_text() == after
    assert (Path(p['record']['receipts'])/'text-undo-0.txt').read_text() == before
    with pytest.raises(ValueError, match='hash_drift'):
        c('apply-prepared-texts', p)


def test_suppression_delta_is_monotonic_and_verified(tmp_path):
    import sqlite3
    home = tmp_path/'copy'
    home.mkdir()
    db_path = home/'knowledge.db'
    with sqlite3.connect(db_path) as db:
        db.execute('CREATE TABLE knowledge_history_sources(event_id TEXT,revision INTEGER,reason TEXT,revoked INTEGER)')
        db.execute("INSERT INTO knowledge_history_sources VALUES ('synthetic-source',1,'purged',1)")
        db.execute('CREATE TABLE knowledge_statements(statement_id TEXT,status TEXT,revoked_at_ms INTEGER)')
        db.execute("INSERT INTO knowledge_statements VALUES ('synthetic-statement','revoked',12)")
    c = host_module().rehearsal_host_controls(copy_home=home, inventory=dict(inventory(), knowledge_db=str(db_path)))
    p = payload(home)
    assert c('capture-suppression-delta', p)['rows'] == 2
    with sqlite3.connect(db_path) as db:
        db.execute('UPDATE knowledge_history_sources SET revoked=0')
        db.execute("UPDATE knowledge_statements SET status='confirmed',revoked_at_ms=NULL")
    assert not c('verify-current-denials', p)['current_denials']
    assert c('reapply-suppression-delta', p)['delta_applied']
    assert c('verify-current-denials', p)['current_denials']
    with sqlite3.connect(db_path) as db:
        assert db.execute('SELECT revoked FROM knowledge_history_sources').fetchone() == (1,)
        assert db.execute('SELECT status,revoked_at_ms FROM knowledge_statements').fetchone() == ('revoked',12)


def test_cli_installs_explicit_factory_and_refuses_unselected_apply(tmp_path, monkeypatch, capsys):
    m = procedure()
    path, home, value = record(tmp_path)
    monkeypatch.setattr(sys, 'argv', ['history_cutover.py', 'cutover', '--record', str(path), '--home', str(home), '--apply'])
    assert m.main() == 1
    assert json.loads(capsys.readouterr().out)['error'] == 'cutover_refused'
    seen = []
    def compose(*, host):
        seen.append(host.mode)
        return Controls(m)
    monkeypatch.setattr(m, 'preparation_controls', compose)
    monkeypatch.setattr(sys, 'argv', ['history_cutover.py', 'cutover', '--record', str(path), '--home', str(home), '--apply', '--controls', 'rehearsal'])
    assert m.main() == 0
    assert seen == ['rehearsal']
    assert json.loads(capsys.readouterr().out)['ok']


def test_raw_health_and_capture_check_exact_argv(tmp_path):
    from yeoman_shared.whatsapp_protocol import PROTOCOL_VERSION
    _, p = data_home(tmp_path)
    calls = []
    def runner(argv):
        calls.append(argv)
        output = json.dumps(dict(writer=dict(state='ok'))) if argv[-2:] == ['status','--json'] else 'status=ok'
        return CompletedProcess(argv, 0, output, '')
    bridge = dict(whatsapp=dict(connected=True), protocolVersion=PROTOCOL_VERSION, outbox=dict(pending=0), queue=dict(inflight=0))
    c = host_module().live_host_controls(inventory=inventory(), runner=runner, bridge_probe=lambda: bridge)
    assert c('health', p)['ok']
    assert c('validate-capture-handover', p)['ok']
    assert calls == [['/synthetic/python','-m','yeoman_gateway','raw','status','--json'], ['/synthetic/python','-m','yeoman_gateway','raw','check-capture']]
    bridge['whatsapp']['connected'] = False
    assert not c('health', p)['ok']



def test_tool_environment_origin_check_does_not_inherit_worktree_pythonpath(tmp_path, monkeypatch):
    host = host_module()
    p = payload(tmp_path)
    p['record']['python'] = '/synthetic/source-python'
    inv = dict(inventory(), source_dir=str(tmp_path), tool_python='/synthetic/tool-python')
    observed = []
    def fake_run(argv, **kwargs):
        observed.append((argv, kwargs))
        return CompletedProcess(argv, 0, json.dumps([str(tmp_path/'packages'/name/'__init__.py') for name in ('gateway','shared','overseer')]), '')
    monkeypatch.setattr(host.subprocess, 'run', fake_run)
    monkeypatch.setenv('PYTHONPATH', '/synthetic/stale-checkout')
    result = host.live_host_controls(inventory=inv)('verify-import-origins', p)
    assert result['imports_verified']
    argv, options = observed[0]
    assert argv[:2] == ['/synthetic/tool-python','-c']
    assert 'PYTHONPATH' not in options['env']
    assert options['env']['YEOMAN_SOURCE_DIR'] == str(tmp_path)
    assert options['env']['YEOMAN_HOME'] == p['home']
    assert options['cwd'] == str(tmp_path)


def test_delta_retains_effects_and_revokes_prior_v2_authority(tmp_path):
    import sqlite3

    from yeoman_gateway.processing.store import ProcessingStore
    home = tmp_path/'copy'
    home.mkdir()
    knowledge, processing = home/'knowledge.db', home/'processing.db'
    store = ProcessingStore(processing)
    store.close()
    with sqlite3.connect(processing) as db:
        db.execute("INSERT INTO event_source_authority(event_id,revision,author_principal,source_channel,source_chat_id,occurred_at_ms,audience_status,created_ms,updated_ms) VALUES ('synthetic-source',1,'synthetic-owner','whatsapp','synthetic-chat',1,'unknown',1,1)")
        db.execute('CREATE TABLE synthetic_effect_receipt(key TEXT,value TEXT)')
        db.execute("INSERT INTO synthetic_effect_receipt VALUES ('synthetic-effect','sent')")
    with sqlite3.connect(knowledge) as db:
        db.execute('CREATE TABLE knowledge_history_sources(event_id TEXT,revision INTEGER,reason TEXT,revoked INTEGER)')
        db.execute("INSERT INTO knowledge_history_sources VALUES ('synthetic-source',1,'purged',1)")
        db.execute('CREATE TABLE knowledge_statements(statement_id TEXT,status TEXT,revoked_at_ms INTEGER)')
        db.execute("INSERT INTO knowledge_statements VALUES ('synthetic-statement','revoked',12)")
    c = host_module().rehearsal_host_controls(copy_home=home, inventory=dict(inventory(), knowledge_db=str(knowledge), processing_db=str(processing)))
    p = payload(home)
    assert c('capture-suppression-delta', p)['ok']
    with sqlite3.connect(knowledge) as db:
        db.execute('DROP TABLE knowledge_history_sources')
        db.execute('CREATE TABLE knowledge_statement_sources(statement_id TEXT,event_id TEXT,revision INTEGER,status TEXT)')
        db.execute("INSERT INTO knowledge_statement_sources VALUES ('synthetic-statement','synthetic-source',1,'active')")
        db.execute("UPDATE knowledge_statements SET status='confirmed',revoked_at_ms=NULL")
    with sqlite3.connect(processing) as db:
        db.execute('DELETE FROM synthetic_effect_receipt')
    assert not c('verify-current-denials', p)['ok']
    assert c('reapply-suppression-delta', p)['ok']
    assert c('verify-current-denials', p)['ok']
    with sqlite3.connect(processing) as db:
        assert db.execute('SELECT value FROM synthetic_effect_receipt').fetchone() == ('sent',)
        assert db.execute('SELECT revoked_at_ms FROM event_source_authority').fetchone()[0] is not None
    with sqlite3.connect(knowledge) as db:
        assert db.execute('SELECT status FROM knowledge_statement_sources').fetchone() == ('revoked',)
        assert db.execute('SELECT status FROM knowledge_statements').fetchone() == ('revoked',)



def test_alert_oneshot_that_never_enters_active_is_still_observed(tmp_path):
    runner = Runner()
    def run(argv):
        result = runner(argv)
        if argv == ['systemctl','--user','stop','yeoman-overseer.service']:
            runner.states['yeoman-overseer-alert.service']['ExecMainStartTimestampMonotonic'] = '100000001'
        return result
    result = host_module().live_host_controls(inventory=inventory(), runner=run, clock=Clock())('stop-overseer-clean', payload(tmp_path))
    assert result['clean'] and result['alert_fired'] and not result['ok']


def test_timer_observation_does_not_require_service_only_properties(tmp_path):
    runner = Runner()
    def run(argv):
        result = runner(argv)
        if argv[:3] == ['systemctl','--user','show'] and argv[3].endswith('.timer'):
            result.stdout = '\n'.join(line for line in result.stdout.splitlines() if not line.startswith(('Result=', 'Restart=')))
        return result
    assert host_module().live_host_controls(inventory=inventory(), runner=run)('stop-timers-and-manual-routes', payload(tmp_path))['ok']



def test_rehearsal_record_refuses_runtime_acquisition_output(tmp_path):
    m = procedure()
    path, home, value = record(tmp_path)
    value['output'] = '/home/dm/.yeoman/data/forbidden-rehearsal'
    value['digest'] = m.record_digest(value)
    path.write_text(json.dumps(value))
    c = Controls(m)
    with m.injected_controls(c), pytest.raises(ValueError, match='runtime'):
        m.run_cutover(record=path, home=home, apply=True)
    assert c.calls == []


def test_rehearsal_text_symlink_refused_before_read(tmp_path):
    import hashlib
    home = tmp_path/'copy'
    home.mkdir()
    outside = tmp_path/'synthetic-outside'
    outside.write_text('outside text')
    (home/'runbook.md').symlink_to(outside)
    manifest = home/'manifest.json'
    manifest.write_text(json.dumps(dict(actions=[dict(path='/synthetic-original/runbook.md', current_sha256=hashlib.sha256(outside.read_bytes()).hexdigest())])))
    inv = dict(inventory(), original_home='/synthetic-original', prepared_text_manifest=str(manifest), prepared_text_manifest_sha256=hashlib.sha256(manifest.read_bytes()).hexdigest())
    c = host_module().rehearsal_host_controls(copy_home=home, inventory=inv)
    with pytest.raises(ValueError, match='unsymlinked'):
        c('apply-prepared-texts', payload(home))
    assert outside.read_text() == 'outside text'



def test_rehearsal_restore_refuses_runtime_failed_snapshot(tmp_path):
    m = procedure()
    path, home, _ = record(tmp_path)
    c = Controls(m)
    with m.injected_controls(c), pytest.raises(ValueError, match='runtime'):
        m.restore_prior_set(record=path, home=home, failed_snapshot=Path('/home/dm/.yeoman/data/forbidden-rehearsal'), apply=True)
    assert c.calls == []


def test_proc_scan_matches_configured_executable_symlink(tmp_path):
    target = tmp_path/'real-executable'
    target.write_text('synthetic executable')
    configured = tmp_path/'configured-executable'
    configured.symlink_to(target)
    proc = tmp_path/'proc'
    entry = proc/'43'
    entry.mkdir(parents=True)
    (entry/'exe').symlink_to(target)
    (entry/'cmdline').write_bytes(str(configured).encode()+b'\0')
    inv = inventory()
    inv['units'][0]['executable'] = str(configured)
    proof = host_module().live_host_controls(inventory=inv, runner=Runner(), proc_root=proc)('verify-quiescent', payload(tmp_path))
    assert not proof['ok'] and not proof['writers_absent']
