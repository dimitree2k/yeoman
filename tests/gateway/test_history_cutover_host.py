"""Observed host controls: subprocesses are always injected fakes."""
import importlib.util
import json
import sys
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from tests.gateway.test_history_cutover import PAUSE_CANONICAL, Controls, procedure, record


def host_module():
    scripts = str(Path(__file__).parents[2] / 'scripts')
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    spec = importlib.util.spec_from_file_location('cutover_host', Path(scripts) / 'history_cutover_host.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def synthetic_runtime(tmp_path,monkeypatch):
    original=host_module
    def module():
        result=original()
        factory=result.live_host_controls
        def controls(**kwargs):
            inv=dict(kwargs['inventory'])
            inv.setdefault('systemd_runtime_dir',str(tmp_path/'systemd/user'))
            inv.setdefault('owner_uid',__import__('os').getuid())
            kwargs['inventory']=inv
            kwargs.setdefault('clock',Clock())
            runner=kwargs.get('runner')
            if isinstance(runner,Runner):
                runner.runtime=Path(inv['systemd_runtime_dir'])
            return factory(**kwargs)
        result.live_host_controls=controls
        return result
    monkeypatch.setattr(__import__(__name__,fromlist=['host_module']),'host_module',module)


class Clock:
    now = 100.0
    def monotonic(self):
        return self.now
    def sleep(self, seconds):
        assert seconds > 0
        self.now += seconds


class Runner:
    def __init__(self):
        self.calls = []
        self.states = {}
        self.ignore_stop = False
        self.ignore_mask = False
        self.alert = 0
        self.runtime = None
        # Units whose files changed since the last daemon-reload the manager observed.
        self.pending_reload = set()
        self.reloads = 0
        self.ignore_reload_after = None
        self.deploy_leaves_pending = False
    def mark_pending_reload(self, *units):
        self.pending_reload.update(units)
    def condition_blocked(self, unit):
        """A unit is suppressed while any installed guard drop-in still has its marker."""
        if self.runtime is None:
            return False
        for path in sorted((self.runtime/(unit+'.d')).glob('zz-yeoman-cutover-*.conf')):
            digest = path.name[len('zz-yeoman-cutover-'):-len('.conf')]
            if (self.runtime/f'.yeoman-cutover-{digest}.hold').exists():
                return True
        return False
    def __call__(self, argv):
        self.calls.append(argv)
        if argv[0]=='busctl':
            markers = [] if self.ignore_mask or self.runtime is None else sorted(self.runtime.glob('.yeoman-cutover-*.hold'))
            data=[['ConditionPathExists',False,True,str(marker),0] for marker in markers]
            return CompletedProcess(argv,0,json.dumps(dict(type='a(sbbsi)',data=data)),'')
        if argv[:3] == ['systemctl', '--user', 'show']:
            unit = argv[3]
            state = self.states.setdefault(unit, {'ActiveState': 'inactive', 'Result': 'success', 'UnitFileState': 'enabled', 'Restart': 'always', 'ActiveEnterTimestampMonotonic': '0', 'ExecMainStartTimestampMonotonic': '0', 'ExecMainExitTimestampMonotonic': '0'})
            if unit == 'yeoman-overseer-alert.service':
                state['ActiveEnterTimestampMonotonic'] = str(self.alert)
            if self.runtime:
                state.update(LoadState='loaded',
                             NeedDaemonReload='yes' if unit in self.pending_reload else 'no',
                             DropInPaths=' '.join(map(str,(self.runtime/(unit+'.d')).glob('*.conf'))) if not self.ignore_mask else '')
            return CompletedProcess(argv, 0, '\n'.join(f'{k}={v}' for k, v in state.items()), '')
        if argv[:3] == ['systemctl', '--user', 'daemon-reload']:
            self.reloads += 1
            if self.ignore_reload_after is None or self.reloads <= self.ignore_reload_after:
                self.pending_reload.clear()
            return CompletedProcess(argv, 0, '', '')
        if argv[:2] == ['systemctl', '--user']:
            operation = argv[2]
            unit = argv[-1]
            state = self.states.setdefault(unit, {'ActiveState': 'inactive', 'Result': 'success', 'UnitFileState': 'enabled', 'Restart': 'always', 'ActiveEnterTimestampMonotonic': '0', 'ExecMainStartTimestampMonotonic': '0', 'ExecMainExitTimestampMonotonic': '0'})
            if operation == 'stop' and not self.ignore_stop:
                state['ActiveState'] = 'inactive'
            if operation == 'start':
                if self.condition_blocked(unit):
                    return CompletedProcess(argv, 1, '', f'{unit}: start condition failed')
                state['ActiveState'] = 'active'
            if operation == 'mask' and not self.ignore_mask:
                state['UnitFileState'] = 'masked-runtime'
            if operation == 'unmask':
                state['UnitFileState'] = 'enabled'
        if argv[-1:] == ['deploy']:
            # A deploy rewrites unit files and reloads the manager itself; a deploy that
            # leaves the manager stale is modelled only when a witness asks for it.
            if self.deploy_leaves_pending:
                self.pending_reload.update(self.states)
        return CompletedProcess(argv, 0, '{}', '')


def inventory():
    return dict(units=[dict(name=f'yeoman-{name}.service', restart='always', executable=f'/synthetic/{name}') for name in ('overseer', 'gateway', 'bridge', 'a2a')], timers=['watch.timer'], timer_services={'watch.timer': ['watch.service']}, manual_routes=['manual.service'], gateway_unit='yeoman-gateway.service', bridge_unit='yeoman-bridge.service')


def payload(tmp_path):
    return dict(home=str(tmp_path), record=dict(receipts=str(tmp_path / 'receipts'), digest='a'*64, layout={}), sources=[], selection={})


@pytest.mark.parametrize(('action', 'units'), [
    ('stop-overseer-clean', ['yeoman-overseer.service']),
    ('stop-gateway', ['yeoman-gateway.service']),
    ('stop-bridge', ['yeoman-bridge.service']),
    ('stop-timers-and-manual-routes', ['watch.timer', 'manual.service', 'yeoman-a2a.service', 'watch.service']),
    ('start-bridge', ['yeoman-bridge.service']),
    ('start-gateway', ['yeoman-gateway.service']),
    ('start-overseer', ['yeoman-overseer.service']),
    ('start-timers', ['watch.timer']),
    ('resume-vetted-manual-routes', ['manual.service', 'yeoman-a2a.service']),
])
def test_exact_service_argv(tmp_path, action, units):
    runner = Runner()
    control = host_module().live_host_controls(inventory=inventory(), runner=runner, clock=Clock())
    if action.startswith('start-') or action=='resume-vetted-manual-routes':
        control('suppress-restarts',payload(tmp_path))
        runner.calls.clear()
    result = control(action, payload(tmp_path))
    assert result['ok'] and result['mode'] == 'live'
    mutations = [a for a in runner.calls if a[0]=='systemctl' and a[2] in ('start','stop')]
    expected = [['systemctl', '--user', 'stop', u] for u in units] if action.startswith('stop-') else [['systemctl','--user','start',u] for u in units]
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
    assert not any(a[0]=='systemctl' and a[2]=='mask' for a in runner.calls)
    assert len(list(runner.runtime.glob('*/*.conf')))==7
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


def raw_status(*, spooled=0, state='ok'):
    return dict(files=0, lines=0, root='/synthetic/raw', started_ms=1,
        writer=dict(state=state, spooled=spooled, pending_in_memory=0, last_error='', updated_ms=1))


@pytest.mark.parametrize('mode', ['live', 'rehearsal'])
@pytest.mark.parametrize('action', ['health', 'drain-durable-tails', 'all-committed-barrier'])
@pytest.mark.parametrize('malformed', [False, 'flat', 'counter', 'missing'])
def test_raw_status_real_shape_required(tmp_path, mode, action, malformed):
    from yeoman_shared.whatsapp_protocol import PROTOCOL_VERSION
    home, p = data_home(tmp_path)
    raw = raw_status()
    if malformed == 'flat':
        raw = raw['writer']
    elif malformed == 'counter':
        raw['writer']['spooled'] = True
    elif malformed == 'missing':
        del raw['writer']['pending_in_memory']
    bridge = dict(outbox=dict(pending=0), queue=dict(inflight=0), whatsapp=dict(connected=True),
        protocolVersion=PROTOCOL_VERSION, persistenceFailure=False)
    calls = []
    def runner(argv):
        calls.append(argv)
        return CompletedProcess(argv, 0, json.dumps(raw), '')
    module = host_module()
    if mode == 'live':
        control = module.live_host_controls(inventory=inventory(), runner=runner,
            ipc=lambda _: dict(status='ok',health=dict(status='ready',generation=3,lag_lines=0,lag_bytes=0)),
            bridge_probe=lambda: bridge)
    else:
        (home/'raw.json').write_text(json.dumps(raw))
        (home/'bridge.json').write_text(json.dumps(bridge))
        control = module.rehearsal_host_controls(copy_home=home, runner=runner,
            inventory=dict(inventory(),raw_status_path='raw.json',bridge_status_path='bridge.json'))
    if malformed:
        with pytest.raises(ValueError, match='invalid_raw_status'):
            control(action, p)
    else:
        assert control(action, p)['ok']
    if mode == 'rehearsal':
        assert calls == []


def test_live_barrier_uses_observed_stores_and_counters(tmp_path):
    home, p = data_home(tmp_path)
    runner = Runner()
    original = runner.__call__
    def run(argv):
        if argv[1:] == ['-m', 'yeoman_gateway', 'raw', 'status', '--json']:
            runner.calls.append(argv)
            return CompletedProcess(argv, 0, json.dumps(raw_status()), '')
        return original(argv)
    from yeoman_shared.whatsapp_protocol import PROTOCOL_VERSION
    bridge = dict(outbox=dict(pending=0), queue=dict(inflight=0), whatsapp=dict(connected=True), protocolVersion=PROTOCOL_VERSION,persistenceFailure=False)
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
    with pytest.raises(ValueError,match='readiness_timeout'):
        control('drain-durable-tails',p)


def test_rehearsal_barrier_reads_copy_and_refuses_external_layout(tmp_path):
    home, p = data_home(tmp_path)
    runner = Runner()
    inv = dict(inventory(), raw_status_path='raw-state.json', bridge_status_path='bridge-state.json')
    (home/'raw-state.json').write_text(json.dumps(raw_status()))
    (home/'bridge-state.json').write_text(json.dumps(dict(outbox=dict(pending=0), queue=dict(inflight=0))))
    control = host_module().rehearsal_host_controls(copy_home=home, inventory=inv, runner=runner)
    assert control('all-committed-barrier', p)['ok']
    (home/'raw-state.json').write_text(json.dumps(raw_status(spooled=1)))
    assert not control('all-committed-barrier', p)['ok']
    p['record']['layout']['raw'] = str(tmp_path/'outside')
    with pytest.raises(ValueError, match='outside_root'):
        control('all-committed-barrier', p)
    assert runner.calls == []


def test_owner_ack_is_pinned_and_never_auto_success(tmp_path):
    from tests.gateway.test_history_cutover_smoke import FakeClock, publish, wait_payload
    p = wait_payload(tmp_path)
    c = host_module().live_host_controls(inventory=inventory(), runner=Runner(), clock=FakeClock())
    with pytest.raises(ValueError, match='owner_ack_timeout'):
        c('functional-smoke', p)
    publish(p, record_digest='wrong')
    with pytest.raises(ValueError, match='owner_ack'):
        c('functional-smoke', p)
    publish(p)
    assert c('functional-smoke', p)['ok']



def test_end_to_end_dry_plan_apply_fake_host_reaches_all_receipts(tmp_path):
    m = procedure()
    path, home, value = record(tmp_path)
    value['mode'] = 'live'
    value['confirmation_token'] = str(tmp_path/'confirmed')
    value['inventory'].update(inventory(), pause_path=str(home/'data/ops/response-pauses.json'))
    (home/'data/ops/response-pauses.json').write_text(PAUSE_CANONICAL)
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
    assert (home/'data/ops/response-pauses.json').read_text() == PAUSE_CANONICAL


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
    def compose(*, host, probes, decode):
        seen.append(host.mode)
        return Controls(m)
    from yeoman_gateway.history.convert import bridge_refs
    monkeypatch.setattr(bridge_refs,'node_batch_decoder',lambda _: lambda items: {})
    from scripts import history_cutover_probes
    monkeypatch.setattr(history_cutover_probes,'build_probes',lambda **_: {})
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
        output = json.dumps(raw_status()) if argv[-2:] == ['status','--json'] else 'status=ok'
        return CompletedProcess(argv, 0, output, '')
    bridge = dict(whatsapp=dict(connected=True), protocolVersion=PROTOCOL_VERSION, persistenceFailure=False, outbox=dict(pending=0), queue=dict(inflight=0))
    c = host_module().live_host_controls(inventory=inventory(), runner=runner, bridge_probe=lambda: bridge)
    assert c('health', p)['ok']
    assert c('validate-capture-handover', p)['ok']
    assert calls == [['/synthetic/python','-m','yeoman_gateway','raw','status','--json'], ['/synthetic/python','-m','yeoman_gateway','raw','check-capture']]
    bridge['whatsapp']['connected'] = False
    with pytest.raises(ValueError,match='readiness_timeout'):
        c('health',p)



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


@pytest.mark.parametrize('mode',['live','rehearsal'])
def test_configure_uses_loader_migration_preserving_raw_profile_keys(tmp_path,mode):
    from yeoman_shared.config.loader import load_config
    home,p = data_home(tmp_path)
    config = home/'camel-config.json'
    original = dict(ownerExtension={'untouchedKey':'synthetic'},models=dict(
        profiles={'syntheticFast':{'kind':'chat','model':'synthetic','maxTokens':123}},
        routes={'assistant.reply':'syntheticFast'}),ipc={'gatewaySocketPath':str(home/'run/synthetic.sock')},
        history={},channels={'whatsapp':{'replyContextWindowLimit':7}})
    config.write_text(json.dumps(original,indent=2))
    before = config.read_bytes()
    p['selection'] = dict(legacyWritersDisabled=True,liveProjectionEnabled=True,readers=dict(knowledge=True))
    inv = dict(inventory(),config_path=str(config))
    runner = Runner()
    h = host_module()
    control = (h.live_host_controls(inventory=inv,runner=runner) if mode=='live'
        else h.rehearsal_host_controls(copy_home=home,inventory=inv))
    result = control('configure-retirement',p)
    assert result['ok'] and runner.calls==[]
    assert Path(result['backup']).read_bytes()==before
    after = json.loads(config.read_bytes())
    assert list(after)==list(original)
    assert {k:v for k,v in after.items() if k!='history'}=={k:v for k,v in original.items() if k!='history'}
    assert after['history']==p['selection']
    loaded = load_config(config)
    assert loaded.history.legacy_writers_disabled and loaded.history.live_projection_enabled
    assert loaded.history.readers.knowledge
    assert loaded.models.routes['assistant.reply']=='synthetic_fast'


@pytest.mark.parametrize('mode', ['live', 'rehearsal'])
@pytest.mark.parametrize('action', ['health', 'drain-durable-tails', 'all-committed-barrier'])
@pytest.mark.parametrize('drift', [None, 'schema', 'selection', 'bridge', 'raw'])
def test_restore_health_observes_prior_dormant_set(tmp_path, mode, action, drift):
    import sqlite3

    from yeoman_shared.whatsapp_protocol import PROTOCOL_VERSION
    m = procedure()
    home, p = data_home(tmp_path)
    knowledge = Path(p['record']['layout']['knowledge_live'])
    with sqlite3.connect(knowledge) as db:
        db.execute("UPDATE knowledge_meta SET value='2' WHERE key='schema_version'")
        db.execute('DROP TABLE knowledge_history_capture_state')
    config = home/'config.json'
    prior_selection = dict(legacyWritersDisabled=False,liveProjectionEnabled=False,readers={})
    config.write_text(json.dumps(dict(history=prior_selection)))
    inv = dict(inventory(),knowledge_db=str(knowledge),config_path=str(config),
        members=[dict(path='knowledge.db',kind='sqlite',restore=True),dict(path='config.json',kind='file',restore=True)],
        bridge=dict(mode='stopped'),raw_status_path='raw.json',bridge_status_path='bridge.json')
    snapshot = tmp_path/'prior'
    m.acquire_cutover_snapshot(home=home,output=snapshot,inventory=inv)
    p['record'].update(output=str(snapshot),inventory=inv)
    p['operation'] = 'restore'
    # Dormant health must not open any history/capture tables.
    Path(p['record']['layout']['history']).unlink()
    if drift == 'schema':
        with sqlite3.connect(knowledge) as db:
            db.execute("UPDATE knowledge_meta SET value='3' WHERE key='schema_version'")
    elif drift == 'selection':
        config.write_text(json.dumps(dict(history=dict(legacyWritersDisabled=True))))
    raw = raw_status(spooled=1 if drift == 'raw' else 0)
    bridge = dict(outbox=dict(pending=1 if drift == 'bridge' else 0),queue=dict(inflight=0),
        whatsapp=dict(connected=True),protocolVersion=PROTOCOL_VERSION,persistenceFailure=False)
    calls = []
    def runner(argv):
        calls.append(argv)
        return CompletedProcess(argv,0,json.dumps(raw),'')
    def ipc(_):
        raise AssertionError('dormant restore must not request history_control')
    module = host_module()
    if mode == 'live':
        control = module.live_host_controls(inventory=inv,runner=runner,ipc=ipc,bridge_probe=lambda: bridge)
    else:
        (home/'raw.json').write_text(json.dumps(raw))
        (home/'bridge.json').write_text(json.dumps(bridge))
        control = module.rehearsal_host_controls(copy_home=home,rehearsal_root=tmp_path,inventory=inv,runner=runner)
    if mode=='live' and drift is not None:
        with pytest.raises(ValueError,match='readiness_timeout' if drift in ('raw','bridge') else 'prior_readiness_mismatch'):
            control(action,p)
        return
    proof = control(action,p)
    assert proof['ok'] is (drift is None)
    assert proof['prior_schema_version'] == '2'
    assert proof['knowledge_schema_version'] == ('3' if drift == 'schema' else '2')
    assert proof['integrity_ok']
    assert proof['prior_selection_matches'] is (drift != 'selection')
    assert 'capture_ready' not in proof
    if mode == 'rehearsal':
        assert calls == []


@pytest.mark.parametrize('leave_v3', [False, True])
def test_whole_restore_reaches_timers_with_prior_health(tmp_path, leave_v3):
    import sqlite3

    from yeoman_shared.whatsapp_protocol import PROTOCOL_VERSION
    m = procedure()
    path, home, value = record(tmp_path)
    knowledge = home/'knowledge.db'
    knowledge.unlink()
    with sqlite3.connect(knowledge) as db:
        db.executescript("CREATE TABLE knowledge_meta(key TEXT,value TEXT); INSERT INTO knowledge_meta VALUES ('schema_version','2'); "
            "CREATE TABLE knowledge_statements(statement_id TEXT,status TEXT,revoked_at_ms INTEGER); "
            "CREATE TABLE knowledge_statement_sources(event_id TEXT,revision INTEGER,status TEXT);")
    processing = home/'processing.db'
    from yeoman_gateway.processing.store import ProcessingStore
    ProcessingStore(processing).close()
    prior_selection = dict(legacyWritersDisabled=False,liveProjectionEnabled=False,readers={})
    config = home/'config.json'
    config.write_text(json.dumps(dict(history=prior_selection)))
    value['layout'] = dict(knowledge_live=str(knowledge),raw=str(home/'raw'),history=str(home/'no-history.db'))
    for entry in value['inventory']['members']:
        if entry['path'] == 'knowledge.db':
            entry['kind'] = 'sqlite'
    value['inventory']['members'].extend([dict(path='config.json',kind='file',restore=True),dict(path='processing.db',kind='sqlite',restore=True)])
    value['digest'] = m.record_digest(value)
    path.write_text(json.dumps(value))
    m.acquire_cutover_snapshot(home=home,output=Path(value['output']),inventory=value['inventory'])
    with sqlite3.connect(knowledge) as db:
        db.execute("UPDATE knowledge_meta SET value='3' WHERE key='schema_version'")
        db.execute('CREATE TABLE knowledge_history_sources(event_id TEXT,revision INTEGER,reason TEXT,revoked INTEGER)')
    config.write_text(json.dumps(dict(history=dict(legacyWritersDisabled=True,liveProjectionEnabled=True))))
    (home/'raw-status.json').write_text(json.dumps(raw_status()))
    (home/'bridge-status.json').write_text(json.dumps(dict(outbox=dict(pending=0),queue=dict(inflight=0),
        whatsapp=dict(connected=True),protocolVersion=PROTOCOL_VERSION,persistenceFailure=False)))
    runner_calls = []
    def runner(argv):
        runner_calls.append(argv)
        raise AssertionError('host subprocess forbidden')
    control = host_module().rehearsal_host_controls(copy_home=home,rehearsal_root=tmp_path,inventory=value['inventory'],runner=runner)
    def execute(action,payload):
        assert payload['operation'] == 'restore'
        if leave_v3 and action == 'health':
            with sqlite3.connect(knowledge) as db:
                db.execute("UPDATE knowledge_meta SET value='3' WHERE key='schema_version'")
        return control(action,payload)
    execute.mode = 'rehearsal'
    with m.injected_controls(execute):
        result = m.restore_prior_set(record=path,home=home,failed_snapshot=tmp_path/'failed',apply=True)
    receipt = json.loads(Path(result['receipt']).read_text())
    assert result['ok'] is (not leave_v3)
    if leave_v3:
        assert receipt['failed_phase'] == 'health'
        assert receipt['error_code'] == 'phase_proof_failed'
    else:
        assert receipt['phases'][-1]['action'] == 'start-timers'
        health = next(p['receipt'] for p in receipt['phases'] if p['action'] == 'health')
        assert health['prior_ready'] and health['knowledge_schema_version'] == '2'
        assert health['prior_selection_matches'] and 'capture_ready' not in health
    assert runner_calls == []


def command_layout(tmp_path):
    staged = tmp_path / 'staged'
    staged.mkdir()
    manifest = tmp_path / 'conversion.json'
    manifest.write_text(json.dumps(dict(package_digest='a' * 64, files={})))
    owner = tmp_path / 'owners.jsonl'
    owner.write_text('{}\n{}\n')
    raw = tmp_path / 'data/raw'
    raw.mkdir(parents=True)
    return dict(staged=str(staged), conversion_manifest=str(manifest), owner_package=str(owner), raw=str(raw))


def live_command_payload(tmp_path, action, value):
    argv = procedure().command_for(action, value)
    return dict(home=str(tmp_path / 'live-home'), record=value, argv=argv, receipts=[])


@pytest.mark.parametrize(('returncode', 'stdout', 'stderr', 'code', 'diagnostic'), [
    (2, '', 'Error: import package validation or publication failed\n', 'command_failed_exit_2', 'stderr'),
    (0, '', '', 'command_empty_output', None),
    (0, 'not json at all', '', 'command_output_not_json', 'stdout'),
    (0, '[]', '', 'command_output_not_object', 'stdout'),
])
def test_live_cli_failure_surface_is_bounded(tmp_path, monkeypatch, returncode, stdout, stderr, code, diagnostic):
    import re
    module = host_module()
    value = dict(python=sys.executable, receipts=str(tmp_path / 'receipts'), layout=command_layout(tmp_path))
    payload = live_command_payload(tmp_path, 'import-preview', value)
    monkeypatch.setattr(module.subprocess, 'run',
                        lambda argv, **options: CompletedProcess(argv, returncode, stdout, stderr))
    control = module.live_host_controls(inventory=dict(inventory(), source_dir=str(tmp_path)), clock=Clock())
    with pytest.raises(ValueError) as failure:
        control('import-preview', payload)
    assert str(failure.value) == code
    assert re.fullmatch(r'[a-z0-9_]+', str(failure.value))
    assert 'JSONDecode' not in str(failure.value)
    receipts = tmp_path / 'receipts'
    if diagnostic is None:
        assert not list(receipts.glob('command-*'))
    else:
        path = receipts / f'command-import-preview.{diagnostic}.txt'
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.read_text() == (stderr if diagnostic == 'stderr' else stdout)


def test_live_cli_failure_is_journalled_as_a_bounded_code_and_keeps_stderr(tmp_path, monkeypatch):
    m, module = procedure(), host_module()
    path, home, value = record(tmp_path)
    value.update(mode='live', python=sys.executable, layout=command_layout(tmp_path))
    value['digest'] = m.record_digest(value)
    path.write_text(json.dumps(value))
    monkeypatch.setattr(module.subprocess, 'run', lambda argv, **options: CompletedProcess(
        argv, 2, '', 'Error: import package validation or publication failed\n'))
    control = module.live_host_controls(inventory=value['inventory'], clock=Clock())
    with m.injected_controls(control):
        result = m._run(value, home, ['import-preview'], record_dir=path.parent)
    assert not result['ok'] and result['failed_phase'] == 'import-preview'
    journal = json.loads((Path(value['receipts']) / 'cutover.json').read_text())
    assert journal['error_code'] == 'command_failed_exit_2'
    diagnostic = Path(value['receipts']) / 'command-import-preview.stderr.txt'
    assert diagnostic.stat().st_mode & 0o777 == 0o600
    assert 'import package validation or publication failed' in diagnostic.read_text()


@pytest.mark.parametrize(('action', 'response'), [
    ('import-preview', dict(status='dry-run', files=1, records=2, suppressed=0)),
    ('import', dict(status='complete', files=1, records=2, suppressed=0)),
    ('owner-preview', dict(validated=2, committed=0, suppressed=0)),
    ('owner-append', dict(validated=2, committed=2, suppressed=0)),
])
def test_rehearsal_and_live_execute_the_same_command_argv(tmp_path, monkeypatch, action, response):
    module = host_module()
    root, copy = tmp_path / 'rehearsal', tmp_path / 'rehearsal/copy'
    copy.mkdir(parents=True)
    source = tmp_path / 'source'
    (source / 'packages/gateway').mkdir(parents=True)
    value = dict(python=sys.executable, mode='rehearsal', rehearsal_root=str(root), home=str(copy),
                 receipts=str(root / 'receipts'), output=str(root / 'acquisition'), layout=command_layout(root))
    argv = procedure().command_for(action, value)
    calls = []
    def runner(executed, **options):
        calls.append((executed, options))
        return CompletedProcess(executed, 0, json.dumps(response), '')
    monkeypatch.setattr(module.subprocess, 'run', runner)
    inv = dict(inventory(), source_dir=str(source))
    live = module.live_host_controls(inventory=inv, clock=Clock())
    rehearsal = module.rehearsal_host_controls(copy_home=copy, rehearsal_root=root, inventory=inv)
    live_result = live(action, live_command_payload(tmp_path, action, value))
    rehearsal_result = rehearsal(action, dict(home=str(copy), record=value, argv=argv, receipts=[]))
    assert [call[0] for call in calls] == [argv, argv]
    assert Path(calls[0][1]['cwd']) == Path(calls[1][1]['cwd']) == source
    assert calls[1][1]['env']['YEOMAN_HOME'] == str(copy)
    assert calls[0][1]['env']['YEOMAN_HOME'] == str(tmp_path / 'live-home')
    assert calls[1][1]['env']['YEOMAN_SOURCE_DIR'] == str(source)
    assert str(source / 'packages/gateway') in calls[1][1]['env']['PYTHONPATH']
    assert {k: v for k, v in live_result.items() if k != 'mode'} == \
           {k: v for k, v in rehearsal_result.items() if k != 'mode'}


def test_rehearsal_command_never_addresses_the_live_home_or_leaves_the_root(tmp_path, monkeypatch):
    module = host_module()
    root, copy = tmp_path / 'rehearsal', tmp_path / 'rehearsal/copy'
    copy.mkdir(parents=True)
    source = tmp_path / 'source'
    source.mkdir()
    value = dict(python=sys.executable, rehearsal_root=str(root), home=str(copy),
                 receipts=str(root / 'receipts'), layout=command_layout(root))
    executed = []
    rehearsal = module.rehearsal_host_controls(copy_home=copy, rehearsal_root=root,
                                               inventory=dict(inventory(), source_dir=str(source)),
                                               runner=executed.append)
    payload = dict(home=str(copy), record=value, receipts=[], argv=[
        sys.executable, '-m', 'yeoman_gateway', 'history', 'import-backfill',
        '--staged', '/home/dm/.yeoman/data/raw', '--manifest', value['layout']['conversion_manifest'], '--confirm'])
    with pytest.raises(ValueError, match='rehearsal_command_outside_root'):
        rehearsal('import', payload)
    payload['argv'][-3] = str(tmp_path / 'elsewhere')
    with pytest.raises(ValueError, match='rehearsal_command_outside_root'):
        rehearsal('import', payload)
    payload['argv'] = ['/synthetic/python', *payload['argv'][1:]]
    with pytest.raises(ValueError, match='rehearsal_command_interpreter'):
        rehearsal('import', payload)
    assert executed == []
    # The live control passes the same argv straight through: this refusal is rehearsal-only.
    live_argv = [sys.executable, '-m', 'yeoman_gateway', 'history', 'import-backfill',
                 '--staged', '/home/dm/.yeoman/data/raw',
                 '--manifest', value['layout']['conversion_manifest'], '--dry-run']
    live_calls = []
    monkeypatch.setattr(module.subprocess, 'run', lambda argv, **options: (
        live_calls.append(argv), CompletedProcess(argv, 0, json.dumps(dict(status='dry-run')), ''))[1])
    live = module.live_host_controls(inventory=dict(inventory(), source_dir=str(source)), clock=Clock())
    assert live('import-preview', dict(home=str(tmp_path / 'live-home'), record=value,
                                       argv=live_argv, receipts=[]))['ok']
    assert live_calls == [live_argv]


@pytest.mark.parametrize('unusable', ['not_a_directory', 'symlinked', 'runtime_home', 'live_home_literal'])
def test_rehearsal_refuses_an_unusable_source_dir_through_the_controls(tmp_path, unusable):
    import os
    module = host_module()
    root, copy = tmp_path / 'rehearsal', tmp_path / 'rehearsal/copy'
    copy.mkdir(parents=True)
    value = dict(python=sys.executable, rehearsal_root=str(root), home=str(copy),
                 receipts=str(root / 'receipts'), layout=command_layout(root))
    if unusable == 'not_a_directory':
        source = tmp_path / 'source-file'
        source.write_text('not a checkout')
    elif unusable == 'symlinked':
        (tmp_path / 'real-source').mkdir()
        source = tmp_path / 'source-link'
        source.symlink_to(tmp_path / 'real-source')
    elif unusable == 'runtime_home':
        # The session's isolated YEOMAN_HOME is a protected runtime home; the live one is
        # the same protected set and is never opened by this witness.
        source = Path(os.environ['YEOMAN_HOME']) / 'source'
        source.mkdir()
    else:
        source = Path('/home/dm/.yeoman/data/raw')
    executed = []
    control = module.rehearsal_host_controls(copy_home=copy, rehearsal_root=root,
                                             inventory=dict(inventory(), source_dir=str(source)),
                                             runner=executed.append)
    payload = dict(home=str(copy), record=value, receipts=[],
                   argv=procedure().command_for('import-preview', value))
    with pytest.raises(ValueError, match='rehearsal_source_dir_unusable'):
        control('import-preview', payload)
    assert executed == []


@pytest.mark.parametrize('v3_tables', [False, True])
def test_whole_restore_completes_from_a_store_without_v3_tables(tmp_path, v3_tables):
    """A cutover that failed before publish-v3 leaves a schema-2 store; restore must still run."""
    import sqlite3

    from yeoman_shared.whatsapp_protocol import PROTOCOL_VERSION
    m = procedure()
    path, home, value = record(tmp_path)
    knowledge = home / 'knowledge.db'
    knowledge.unlink()
    with sqlite3.connect(knowledge) as db:
        db.executescript("CREATE TABLE knowledge_meta(key TEXT,value TEXT); "
                         "INSERT INTO knowledge_meta VALUES ('schema_version','2'); "
                         "CREATE TABLE knowledge_statements(statement_id TEXT,status TEXT,revoked_at_ms INTEGER); "
                         "CREATE TABLE knowledge_statement_sources(event_id TEXT,revision INTEGER,status TEXT);")
        db.execute("INSERT INTO knowledge_statements VALUES ('prior-revoked','revoked',11)")
        if v3_tables:
            db.execute('CREATE TABLE knowledge_history_sources(event_id TEXT,revision INTEGER,reason TEXT,revoked INTEGER)')
            db.execute("INSERT INTO knowledge_history_sources VALUES ('prior-source',1,'purged',1)")
    from yeoman_gateway.processing.store import ProcessingStore
    ProcessingStore(home / 'processing.db').close()
    prior_selection = dict(legacyWritersDisabled=False, liveProjectionEnabled=False, readers={})
    (home / 'config.json').write_text(json.dumps(dict(history=prior_selection)))
    value['layout'] = dict(knowledge_live=str(knowledge), raw=str(home / 'raw'), history=str(home / 'no-history.db'))
    for entry in value['inventory']['members']:
        if entry['path'] == 'knowledge.db':
            entry['kind'] = 'sqlite'
    value['inventory']['members'].extend([dict(path='config.json', kind='file', restore=True),
                                          dict(path='processing.db', kind='sqlite', restore=True)])
    value['digest'] = m.record_digest(value)
    path.write_text(json.dumps(value))
    m.acquire_cutover_snapshot(home=home, output=Path(value['output']), inventory=value['inventory'])
    # The failed attempt mutated the live store without ever publishing v3.
    with sqlite3.connect(knowledge) as db:
        if v3_tables:
            db.execute("UPDATE knowledge_meta SET value='3' WHERE key='schema_version'")
        db.execute("UPDATE knowledge_statements SET status='revoked',revoked_at_ms=99 WHERE statement_id='prior-revoked'")
    (home / 'config.json').write_text(json.dumps(dict(history=dict(legacyWritersDisabled=True, liveProjectionEnabled=True))))
    (home / 'raw-status.json').write_text(json.dumps(raw_status()))
    (home / 'bridge-status.json').write_text(json.dumps(dict(outbox=dict(pending=0), queue=dict(inflight=0),
        whatsapp=dict(connected=True), protocolVersion=PROTOCOL_VERSION, persistenceFailure=False)))
    runner_calls = []
    def runner(argv):
        runner_calls.append(argv)
        raise AssertionError('host subprocess forbidden')
    control = host_module().rehearsal_host_controls(copy_home=home, rehearsal_root=tmp_path,
                                                    inventory=value['inventory'], runner=runner)
    with m.injected_controls(control):
        result = m.restore_prior_set(record=path, home=home, failed_snapshot=tmp_path / 'failed', apply=True)
    receipt = json.loads(Path(result['receipt']).read_text())
    assert result['ok'], receipt.get('error_code')
    assert 'error_code' not in receipt and result['failed_phase'] is None
    actions = [p['action'] for p in receipt['phases']]
    assert actions[-1] == 'start-timers'
    assert (actions.index('capture-suppression-delta') < actions.index('restore-files')
            < actions.index('reapply-suppression-delta') < actions.index('verify-current-denials')
            < actions.index('start-bridge'))
    delta = json.loads((Path(value['receipts']) / 'suppression-delta.json').read_text())
    assert delta['sources'] == ([['prior-source', 1, 'purged']] if v3_tables else [])
    assert delta['statements'] == [['prior-revoked', 'revoked', 99]]
    assert next(p['receipt'] for p in receipt['phases'] if p['action'] == 'verify-current-denials')['current_denials']
    health = next(p['receipt'] for p in receipt['phases'] if p['action'] == 'health')
    assert health['prior_ready'] and health['knowledge_schema_version'] == '2'
    with sqlite3.connect(knowledge) as db:
        names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert ('knowledge_history_sources' in names) is v3_tables
        assert db.execute("SELECT status,revoked_at_ms FROM knowledge_statements "
                          "WHERE statement_id='prior-revoked'").fetchone() == ('revoked', 11)
    assert runner_calls == []


def test_verify_current_denials_still_refuses_an_unprovable_delta_without_v3_tables(tmp_path):
    import sqlite3
    module = host_module()
    home = tmp_path / 'copy'
    receipts = home / 'receipts'
    receipts.mkdir(parents=True)
    knowledge = home / 'knowledge.db'
    with sqlite3.connect(knowledge) as db:
        db.executescript("CREATE TABLE knowledge_meta(key TEXT,value TEXT); "
                         "INSERT INTO knowledge_meta VALUES ('schema_version','2'); "
                         "CREATE TABLE knowledge_statements(statement_id TEXT,status TEXT,revoked_at_ms INTEGER);")
    processing = home / 'processing.db'
    with sqlite3.connect(processing) as db:
        db.execute('CREATE TABLE event_source_authority(event_id TEXT,revision INTEGER,revoked_at_ms INTEGER)')
        db.execute("INSERT INTO event_source_authority VALUES ('synthetic-event',1,NULL)")
    (receipts / 'suppression-delta.json').write_text(json.dumps(dict(sources=[['synthetic-event', 1, 'purged']],
                                                                    statements=[])))
    control = module.rehearsal_host_controls(copy_home=home, rehearsal_root=home,
                                             inventory=dict(inventory(), knowledge_db=str(knowledge),
                                                            processing_db=str(processing)))
    payload = dict(home=str(home), record=dict(receipts=str(receipts), layout={}), receipts=[])
    result = control('verify-current-denials', payload)
    assert result['current_denials'] is False and result['ok'] is False


def live_payload(tmp_path, digest, *, receipts='receipts'):
    return dict(home=str(tmp_path), receipts=[], operation='restore',
                record=dict(python=sys.executable, receipts=str(tmp_path/receipts), layout={}, digest=digest))


def test_restore_deploy_is_coherent_with_rewritten_unit_files(tmp_path):
    """restore-files rewrites unit files; the pre-deploy suppression proof must survive that."""
    import hashlib
    import os
    module = host_module()
    runner, runtime = Runner(), tmp_path/'systemd/user'
    runtime.mkdir(parents=True)
    runner.runtime = runtime
    pinned = tmp_path/'installed-text'
    pinned.write_text('prior owner text')
    inv = dict(inventory(), source_dir=str(tmp_path), prior_source_dir=str(tmp_path),
               systemd_runtime_dir=str(runtime), owner_uid=os.getuid(),
               prior_yeoman='/synthetic/prior-yeoman',
               prior_pinned_files={str(pinned): hashlib.sha256(pinned.read_bytes()).hexdigest()})
    (tmp_path/'proc').mkdir()
    payload = live_payload(tmp_path, 'b'*64)
    control = module.live_host_controls(inventory=inv, runner=runner, clock=Clock(), proc_root=tmp_path/'proc')
    assert control('suppress-restarts', payload)['suppressed']
    # The whole-set file restore rewrote the units: the manager has a pending reload.
    runner.mark_pending_reload(*[u['name'] for u in inv['units']])
    runner.calls.clear()
    result = control('restore-software-install-config-units', payload)
    assert result['ok'] and result['restored'], result
    deploy = next(i for i, c in enumerate(runner.calls) if c == ['/synthetic/prior-yeoman', 'deploy'])
    reloads = [i for i, c in enumerate(runner.calls) if c[:3] == ['systemctl','--user','daemon-reload']]
    assert reloads and min(reloads) < deploy
    # The post-deploy proof stays strict: a reload that does not land must refuse the phase.
    strict, strict_runtime = Runner(), tmp_path/'systemd/user-strict'
    strict_runtime.mkdir(parents=True)
    strict.runtime = strict_runtime
    strict_inv = dict(inv, systemd_runtime_dir=str(strict_runtime))
    strict_control = module.live_host_controls(inventory=strict_inv, runner=strict, clock=Clock(), proc_root=tmp_path/'proc')
    strict_payload = live_payload(tmp_path, 'b'*64, receipts='receipts-strict')
    assert strict_control('suppress-restarts', strict_payload)['suppressed']
    strict.mark_pending_reload(*[u['name'] for u in inv['units']])
    strict.deploy_leaves_pending = True
    strict.ignore_reload_after = strict.reloads + 1  # only the post-deploy reload fails to land
    strict_result = strict_control('restore-software-install-config-units', strict_payload)
    assert strict_result['ok'] is False and strict_result['restored'] is True


def test_release_clears_stale_guards_from_prior_attempts(tmp_path):
    """A failed attempt's armed guards must not block a later attempt's start phases."""
    import os
    module = host_module()
    runner, runtime = Runner(), tmp_path/'systemd/user'
    runtime.mkdir(parents=True)
    runner.runtime = runtime
    (tmp_path/'proc').mkdir()
    control = module.live_host_controls(inventory=dict(inventory(), systemd_runtime_dir=str(runtime),
                                                       owner_uid=os.getuid()),
                                        runner=runner, clock=Clock(), proc_root=tmp_path/'proc')
    first = live_payload(tmp_path, '1'*64, receipts='receipts-first')
    second = live_payload(tmp_path, '2'*64, receipts='receipts-second')
    assert control('suppress-restarts', first)['suppressed']
    assert control('suppress-restarts', second)['suppressed']
    assert len(list(runtime.glob('.yeoman-cutover-*.hold'))) == 2
    removed = []
    for action in ('start-bridge', 'start-gateway', 'start-overseer', 'resume-vetted-manual-routes', 'start-timers'):
        phase = control(action, second)
        assert phase['ok'], (action, phase)
        removed.extend(phase['guards']['removed'])
    assert not list(runtime.glob('*.d/zz-yeoman-cutover-*.conf'))
    assert not list(runtime.glob('.yeoman-cutover-*.hold'))
    assert {entry['digest'] for entry in removed} == {'1'*64, '2'*64}
    assert all(entry['reason'] in ('stale_attempt', 'released') for entry in removed)


def test_release_reports_foreign_guard_files_without_removing_them(tmp_path):
    import os
    module = host_module()
    runner, runtime = Runner(), tmp_path/'systemd/user'
    runtime.mkdir(parents=True)
    runner.runtime = runtime
    (tmp_path/'proc').mkdir()
    control = module.live_host_controls(inventory=dict(inventory(), systemd_runtime_dir=str(runtime),
                                                       owner_uid=os.getuid()),
                                        runner=runner, clock=Clock(), proc_root=tmp_path/'proc')
    payload = live_payload(tmp_path, '1'*64)
    assert control('suppress-restarts', payload)['suppressed']
    edited_digest, link_digest = '3'*64, '4'*64
    edited = runtime/'yeoman-bridge.service.d'/f'zz-yeoman-cutover-{edited_digest}.conf'
    edited.write_text(f'[Unit]\nConditionPathExists=!{runtime}/.yeoman-cutover-{edited_digest}.hold\n# edited\n')
    foreign_name = runtime/'yeoman-bridge.service.d'/'zz-yeoman-cutover-not-a-digest.conf'
    foreign_name.write_text('synthetic foreign drop-in')
    link = runtime/f'.yeoman-cutover-{link_digest}.hold'
    link.symlink_to(runtime/f'.yeoman-cutover-{"1"*64}.hold')
    retained = []
    for action in ('start-bridge', 'start-gateway', 'start-overseer', 'resume-vetted-manual-routes', 'start-timers'):
        phase = control(action, payload)
        assert phase['ok'], (action, phase)
        retained.extend(phase['guards']['retained'])
    for path in (edited, foreign_name, link):
        assert path.is_symlink() or path.exists()
    reasons = {Path(entry['path']).name: entry['reason'] for entry in retained}
    assert reasons[edited.name] == 'content_or_owner_drift'
    assert reasons[foreign_name.name] == 'name_not_procedure_owned'
    assert reasons[link.name] == 'content_or_owner_drift'
    assert not list(runtime.glob('.yeoman-cutover-*.hold')) or True


def test_release_keeps_guards_it_does_not_own(tmp_path):
    import os
    module = host_module()
    runner, runtime = Runner(), tmp_path/'systemd/user'
    runtime.mkdir(parents=True)
    runner.runtime = runtime
    (tmp_path/'proc').mkdir()
    payload = live_payload(tmp_path, '1'*64)
    owner = dict(inventory(), systemd_runtime_dir=str(runtime), owner_uid=os.getuid())
    assert module.live_host_controls(inventory=owner, runner=runner, clock=Clock(),
                                     proc_root=tmp_path/'proc')('suppress-restarts', payload)['suppressed']
    foreign = module.live_host_controls(inventory=dict(owner, owner_uid=os.getuid()^1), runner=runner,
                                        clock=Clock(), proc_root=tmp_path/'proc')
    with pytest.raises(ValueError, match='guard_release_unproven'):
        foreign('start-bridge', payload)
    assert list(runtime.glob('*.d/zz-yeoman-cutover-*.conf'))
    assert list(runtime.glob('.yeoman-cutover-*.hold'))


def test_guard_ownership_requires_the_declared_owner_uid(tmp_path):
    import os
    module = host_module()
    runtime = tmp_path/'runtime'
    runtime.mkdir()
    digest = 'a'*64
    marker = runtime/f'.yeoman-cutover-{digest}.hold'
    marker.write_text(digest+'\n')
    dropin = runtime/'yeoman-bridge.service.d'/f'zz-yeoman-cutover-{digest}.conf'
    dropin.parent.mkdir()
    dropin.write_text(f'[Unit]\nConditionPathExists=!{marker}\n')
    owned = dict(owner_uid=os.getuid())
    assert module._owned_guard(marker, kind='marker', digest=digest, root=runtime, **owned)
    assert module._owned_guard(dropin, kind='dropin', digest=digest, root=runtime, **owned)
    for owner_uid in (os.getuid()^1, os.getuid()+1):
        assert not module._owned_guard(marker, kind='marker', digest=digest, root=runtime, owner_uid=owner_uid)
        assert not module._owned_guard(dropin, kind='dropin', digest=digest, root=runtime, owner_uid=owner_uid)
    marker.write_text('edited\n')
    assert not module._owned_guard(marker, kind='marker', digest=digest, root=runtime, **owned)
