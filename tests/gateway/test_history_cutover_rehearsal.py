"""Operator-contract witness: actual data phases, no host subprocesses or services."""
import hashlib
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import pytest
from yeoman_gateway.history.attestations import make
from yeoman_gateway.history.layer1 import row_sha256
from yeoman_gateway.history.live import HistoryBoundary
from yeoman_gateway.history.project import project
from yeoman_gateway.history.queries import HistoryQueries
from yeoman_gateway.history.reader import HistoryReader
from yeoman_gateway.knowledge._store import KnowledgeStore
from yeoman_gateway.knowledge.api import KnowledgeService
from yeoman_gateway.knowledge.authority import EvidenceAudience
from yeoman_gateway.knowledge.models import SourceRef, StatementCandidate, TrustedCaptureContext
from yeoman_gateway.knowledge.runtime import RuntimeKnowledgePolicy, RuntimeKnowledgeSources
from yeoman_gateway.processing.store import ProcessingStore
from yeoman_shared.raw_archive.records import dumps, enumerate_committed

from tests.gateway.test_history_cutover import Controls, procedure, record
from tests.gateway.test_history_cutover_host import raw_status


def bridge_package(tmp_path):
    package = tmp_path/'bridge-package'
    entry = package/'node_modules/@whiskeysockets/baileys/WAProto/index.js'
    entry.parent.mkdir(parents=True,exist_ok=True)
    entry.write_text('/* synthetic decoder is injected */')
    (package/'package.json').write_text('{}')
    return package


def test_rehearsal_root_allows_siblings_and_refuses_escape(tmp_path):
    from scripts.history_cutover_host import rehearsal_host_controls
    root,home = tmp_path/'root',tmp_path/'root/copy'
    home.mkdir(parents=True)
    control = rehearsal_host_controls(copy_home=home,rehearsal_root=root,inventory={})
    payload = dict(home=str(home),record=dict(rehearsal_root=str(root),output=str(root/'acquisition'),
        receipts=str(root/'receipts'),layout=dict(knowledge_source=str(root/'acquisition/v2.db'),staged=str(root/'work/staged'))))
    assert control('stop-gateway',payload)['simulated']
    payload['record']['layout']['staged'] = str(root/'copy/../../escape')
    with pytest.raises(ValueError,match='outside_root'):
        control('stop-gateway',payload)


@pytest.mark.parametrize(('exception','expected'),[
    (ValueError('rehearsal_layout_outside_root'),'rehearsal_layout_outside_root'),
    (ValueError('synthetic private detail'),'unexpected_ValueError'),
    (RuntimeError('synthetic private detail'),'unexpected_RuntimeError')])
def test_failure_receipt_contains_only_safe_code(tmp_path,exception,expected):
    m = procedure()
    path,home,value = record(tmp_path)
    class Refusing(Controls):
        def __call__(self,action,payload):
            raise exception
    with m.injected_controls(Refusing(m)):
        result = m.run_cutover(record=path,home=home,apply=True)
    receipt = json.loads(Path(result['receipt']).read_text())
    assert receipt['error_code'] == expected
    assert 'synthetic private detail' not in json.dumps(receipt)
    diagnostics = list(Path(value['receipts']).glob('refusal-*.txt'))
    assert len(diagnostics) == 1
    diagnostic = diagnostics[0]
    assert diagnostic.stat().st_mode & 0o777 == 0o600
    assert 'Traceback (most recent call last)' in diagnostic.read_text()
    assert f'{type(exception).__name__}: {exception}' in diagnostic.read_text()


def test_builder_accepts_only_executable_interpreter_symlink(tmp_path):
    from scripts.history_cutover_inputs import build_cutover_record
    path,home,value = record(tmp_path)
    example = json.loads((Path(__file__).parents[2]/'scripts/history_cutover_inventory.example.json').read_text())
    example.update(home=str(home),output=value['output'],receipts=value['receipts'],rehearsal_root=str(tmp_path))
    package = bridge_package(tmp_path)
    example['inventory'].update(value['inventory'],bridge_package_dir=str(package))
    interpreter = tmp_path/'python'
    interpreter.symlink_to(sys.executable)
    example['python'] = str(interpreter)
    source = tmp_path/'inventory.json'
    source.write_text(dumps(example))
    kwargs = dict(inventory=source,layout={},mode='rehearsal',window=(1791532800000,1791534600000),expected_gateway_jobs=0)
    assert build_cutover_record(**kwargs)['python'] == str(interpreter)
    interpreter.unlink()
    interpreter.symlink_to(tmp_path/'absent')
    with pytest.raises(ValueError,match='interpreter'):
        build_cutover_record(**kwargs)


@pytest.mark.parametrize('environment',['unset','live','copy','child'])
def test_cli_refuses_unsafe_rehearsal_environment(tmp_path,monkeypatch,environment,capsys):
    m = procedure()
    path,home,value = record(tmp_path)
    if environment=='unset':
        monkeypatch.delenv('YEOMAN_HOME',raising=False)
    else:
        monkeypatch.setenv('YEOMAN_HOME',str({'live':Path('/home/dm/.yeoman'),'copy':home,'child':home/'child'}[environment]))
    monkeypatch.setattr(sys,'argv',['cutover','cutover','--record',str(path),'--home',str(home),'--controls','rehearsal'])
    with pytest.raises(ValueError,match='rehearsal_environment'):
        m._rehearsal_environment(home)
    assert m.main()==1
    assert json.loads(capsys.readouterr().out)['ok'] is False
    receipts = Path(value['receipts'])
    assert not list(receipts.glob('*.json'))
    diagnostics = list(receipts.glob('refusal-*.txt')) + list(path.parent.glob('refusal-*.txt'))
    assert len(diagnostics) == 1
    assert diagnostics[0].stat().st_mode & 0o777 == 0o600



@pytest.mark.parametrize('unusable', ['absent', 'entry_missing', 'load_failure'])
def test_cli_refuses_unusable_decoder_before_any_phase(tmp_path, monkeypatch, capsys, unusable):
    from yeoman_gateway.history.convert import bridge_refs
    m = procedure()
    path, home, value = record(tmp_path)
    package = Path(value['bridge_package_dir'])
    if unusable == 'absent':
        value['bridge_package_dir'] = str(tmp_path/'absent-package')
    elif unusable == 'entry_missing':
        (package/'node_modules/@whiskeysockets/baileys/WAProto/index.js').unlink()
    else:
        def factory(directory):
            def decoder(items):
                assert items == []
                raise RuntimeError('synthetic load failure')
            return decoder
        monkeypatch.setattr(bridge_refs, 'node_batch_decoder', factory)
    value['digest'] = m.record_digest(value)
    path.write_text(json.dumps(value))
    monkeypatch.setattr(sys, 'argv', ['cutover','cutover','--record',str(path),'--home',str(home),'--controls','rehearsal','--apply'])
    assert m.main() == 1
    assert json.loads(capsys.readouterr().out)['ok'] is False
    receipts = Path(value['receipts'])
    assert not list(receipts.glob('*.json'))
    diagnostics = list(receipts.glob('refusal-*.txt')) + list(path.parent.glob('refusal-*.txt'))
    assert len(diagnostics) == 1
    assert diagnostics[0].stat().st_mode & 0o777 == 0o600

@pytest.mark.parametrize('file_invocation',[False,True])
def test_cli_operator_contract_real_sequence(tmp_path,monkeypatch,capsys,file_invocation):
    from yeoman_gateway.history.convert import bridge_refs

    from scripts import history_cutover, history_cutover_inputs
    root = tmp_path/'rehearsal'
    home,work,acquisition = root/'copy',root/'work',root/'acquisition'
    home.mkdir(parents=True)
    work.mkdir()
    package = bridge_package(root)
    sandbox = tmp_path/'empty-environment'
    sandbox.mkdir()
    monkeypatch.setenv('YEOMAN_HOME',str(sandbox))
    ts,phone,member,chat = 1_790_000_000_000,'10001@s.whatsapp.net','10002@s.whatsapp.net','synthetic@g.us'
    raw = home/'data/raw'
    (raw/'whatsapp').mkdir(parents=True)
    (raw/'owner').mkdir()
    owners = [item for who in (phone,member) for item in (
        make('contact',1,'synthetic',identifiers=[who],name='Synthetic'),
        make('identifier',1,'synthetic',anchor=who,identifier=who,valid_from_ms=1))]
    (raw/'owner/attestations.jsonl').write_text('\n'.join(map(dumps,owners))+'\n')
    def native(kind,payload,media=None):
        return dict(archive_version=1,account='synthetic',channel='whatsapp',chat_id=chat,
            direction='in',kind=kind,received_ms=ts,correlation_id='',media=media,
            native=dict(type=kind,payload=dict(chatJid=chat,senderId=phone,timestamp=ts,**payload)))
    lines = [native('membership_snapshot',dict(complete=True,participants=[phone,member]))]
    # timestamp is supplied explicitly below to keep the real native format.
    lines[0]['native']['payload']['timestamp'] = ts-1
    lines += [native('message',dict(messageId='source',text='Synthetic original')),
              native('message',dict(messageId='media',text='Synthetic media'),{'type':'image'})]
    (raw/'whatsapp/native.jsonl').write_text('\n'.join(map(dumps,lines))+'\n')
    fixture_history = work/'fixture-history.db'
    project([raw],fixture_history)
    reader = HistoryReader(fixture_history)
    snap = reader.open_snapshot(HistoryBoundary(1,enumerate_committed(raw)))
    person = HistoryQueries(snap).message(f'whatsapp:{chat}:source')['sender_contact_id']
    snap.close()
    reader.close()
    knowledge = home/'data/knowledge/knowledge.db'
    knowledge.parent.mkdir(parents=True)
    k = KnowledgeStore(knowledge)
    k.set_meta('migration_complete','1')
    k.set_meta('statement_capture_boundary_ms',str(ts-1))
    k.set_meta('statement_capture_boundary_event_id','boundary')
    k.execute('INSERT INTO contacts(id,display_name,created_at,updated_at) VALUES (?,?,?,?)',(person,'Synthetic','2027-01-15T08:00:00+00:00','2027-01-15T08:00:00+00:00'))
    k.execute("INSERT INTO knowledge_identifier_bindings(binding_id,channel,kind,namespace,value,person_id,valid_from_ms,evidence_ref,mapping_verified,created_ms,updated_ms) VALUES (?,'whatsapp','phone_jid','whatsapp',?,?,1,'synthetic',1,1,1)",('binding',phone,person))
    authority = RuntimeKnowledgeSources()
    source = SourceRef('legacy-source',1,'whatsapp',chat,'whatsapp:10001',ts)
    authority.register_source(source,EvidenceAudience.known({'whatsapp:10001','whatsapp:10002'}))
    service = KnowledgeService(store=k,workspace_id='synthetic',source_authority=authority,policy_authority=RuntimeKnowledgePolicy(engine=None))
    result = service.capture(StatementCandidate('Curated synthetic statement',(source,)),context=TrustedCaptureContext('synthetic',1,'native',(source,)))
    statement = result.statement_ids[0]
    service.close()
    processing = home/'data/ops/processing.db'
    p = ProcessingStore(processing)
    p.close()
    with closing(sqlite3.connect(processing)) as db:
        for native_id,event_id in (('source','legacy-source'),('media','media-event')):
            payload = dict(provider_message_id=native_id,text='Synthetic original' if native_id=='source' else 'Synthetic media',
                media=None if native_id=='source' else {'type':'image'},reply_to_message_id=None,mentions=None)
            db.execute('INSERT INTO events(event_id,event_key,trace_id,kind,origin,channel,chat_id,principal,source_message_id,occurred_ms,payload_hash,payload_json,created_ms) VALUES (?,?,?,\'message\',\'whatsapp\',\'whatsapp\',?,?,?, ?,?,?,?)',
                (event_id,event_id,event_id,chat,'whatsapp:10001',native_id,ts,row_sha256(payload),dumps(payload),ts))
        db.execute('INSERT INTO event_source_authority VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            ('legacy-source',1,'whatsapp:10001','whatsapp',chat,ts,'known',dumps(['whatsapp:10001','whatsapp:10002']),None,'synthetic',None,None,ts,ts))
        db.commit()
        db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    (home/'inputs').mkdir()
    (home/'inputs/forward.json').write_text(dumps(dict(forward_start_ms=ts-10,legacy_reinit_ms=ts-1,log_sha256='a'*64,source='synthetic')))
    policy = home/'policy-copy.json'
    policy.write_text(dumps(dict(owners=dict(whatsapp=['10001']),defaults=dict(whoCanTalk=dict(mode='everyone')),channels=dict(whatsapp=dict(chats={chat:{}})))))
    config = home/'config-copy.json'
    config.write_text('{}')
    (home/'cron.json').write_text('{"jobs":[]}')
    texts = work/'texts.json'
    texts.write_text('{"actions":[]}')
    decisions = work/'decisions.json'
    decisions.write_text(dumps([dict(record=item) for item in owners]))
    references = home/'data/ops/bridge-message-references'
    references.mkdir(parents=True)
    (references/'synthetic.json').write_text(dumps(dict(encoded='c3ludGhldGlj',chatJid=chat,storedAtMs=ts)))
    decoded = []
    def factory(directory):
        assert directory==package
        def decoder(items):
            decoded.append(items)
            return {name:dict(value=dict(key=dict(id='source',remoteJid=chat,participant=phone,fromMe=False),
                messageTimestamp=ts//1000,message=dict(conversation='Synthetic original'))) for name,_ in items}
        return decoder
    monkeypatch.setattr(bridge_refs,'node_batch_decoder',factory)
    layout = dict(staged=str(work/'staged'),conversion_manifest=str(work/'conversion.json'),reviewed_decisions=str(decisions),
        owner_package=str(work/'owners.jsonl'),raw=str(raw),history=str(home/'data/history/history.db'),
        verification_home=str(root/'verification'),verify_scratch=str(work/'verify'),verification_report=str(work/'verification.json'),
        preparation_home=str(work/'preparation'),knowledge_source=str(acquisition/'data/knowledge/knowledge.db'),
        knowledge_target=str(work/'v3.db'),knowledge_live=str(knowledge),policy_snapshot=str(policy),alias_output=str(work/'aliases'))
    inv = json.loads((Path(__file__).parents[2]/'scripts/history_cutover_inventory.example.json').read_text())
    inv.update(home=str(home),output=str(acquisition),receipts=str(root/'receipts'),python=sys.executable,rehearsal_root=str(root))
    inv['inventory'].update(original_home=str(home),bridge_package_dir=str(package),raw_path='data/raw',forward_start_evidence_member='inputs/forward.json',
        members=[dict(path=path,kind=kind,restore=restore) for path,kind,restore in (
            ('data/knowledge/knowledge.db','sqlite',True),('data/ops/processing.db','sqlite',True),('data/raw','tree',False),
            ('data/ops/bridge-message-references','tree',False),('inputs/forward.json','file',False),('cron.json','file',True))],
        config_path=str(config),processing_db=str(processing),knowledge_db=str(knowledge),pause_path=str(home/'pauses.json'),
        prepared_text_manifest=str(texts),prepared_text_manifest_sha256=hashlib.sha256(texts.read_bytes()).hexdigest(),
        frozen_files=[str(home/'cron.json')],frozen_watermarks={str(home/'cron.json'):hashlib.sha256((home/'cron.json').read_bytes()).hexdigest()},
        raw_status_path='raw-status.json',bridge_status_path='bridge-status.json',
        reader_smoke=dict(workspace_id='synthetic',channel='whatsapp',chat_id=chat,principal='whatsapp:10001',phone=phone,
            message_id=f'whatsapp:{chat}:source',source_event_id='source',statement_id=statement,query='Curated',text='Synthetic',
            curated_text='Curated synthetic statement',at_ms=ts,owner_scope=True,rights=dict(knowledge_read=True,tools_read=True,owner_export=True)))
    (home/'raw-status.json').write_text(dumps(raw_status()))
    (home/'bridge-status.json').write_text(dumps(dict(outbox=dict(pending=0),queue=dict(inflight=0))))
    inventory,layout_file,record_file = root/'inventory.json',root/'layout.json',root/'record.json'
    inventory.write_text(dumps(inv))
    layout_file.write_text(dumps(layout))
    assert history_cutover_inputs.main(['record','--inventory',str(inventory),'--layout',str(layout_file),
        '--mode','rehearsal','--window-start-ms','1791532800000','--window-end-ms','1791534600000',
        '--expected-gateway-jobs','0','--output',str(record_file)])==0
    value = json.loads(record_file.read_text())
    assert value['approved'] is False
    value.update(approved=True,approval='synthetic-owner')
    value['digest'] = history_cutover.record_digest(value)
    record_file.write_text(dumps(value))
    receipts = Path(value['receipts'])
    receipts.mkdir()
    (receipts/'functional-smoke.owner_ack.json').write_text(dumps(dict(action='functional-smoke',record_digest=value['digest'],owner_ack=True,proof=dict(inbound_message_id_hash='a'*64,outbound_receipt_hash='b'*64,observed_ms=1))))
    monkeypatch.setattr(sys,'argv',['cutover','cutover','--record',str(record_file),'--home',str(home),'--controls','rehearsal','--apply'])
    if file_invocation:
        import os
        import subprocess
        stub = tmp_path/'subprocess-stub'
        stub.mkdir()
        (stub/'sitecustomize.py').write_text(
            'from yeoman_gateway.history.convert import bridge_refs\n'
            'import subprocess\n'
            'original_run = subprocess.run\n'
            'def guarded_run(argv, **kwargs):\n'
            '    assert argv[0] != "systemctl" and "deploy" not in argv\n'
            '    return original_run(argv, **kwargs)\n'
            'subprocess.run = guarded_run\n'
            'bridge_refs.node_batch_decoder = lambda path: lambda items: '
            + ' {name:{"value":'+repr(dict(key=dict(id='source',remoteJid=chat,participant=phone,fromMe=False),
                messageTimestamp=ts//1000,message=dict(conversation='Synthetic original')))
            + '} for name,_ in items}\n')
        env = dict(os.environ,PYTHONPATH=os.pathsep.join([str(stub),*[str(Path(history_cutover.__file__).parents[1]/'packages'/p) for p in ('gateway','shared','overseer')]]))
        completed = subprocess.run([sys.executable,str(Path(history_cutover.__file__)),*sys.argv[1:]],
            cwd=work,env=env,capture_output=True,text=True,timeout=180)
        assert completed.returncode==0, completed.stdout+completed.stderr
    else:
        assert history_cutover.main()==0, (receipts/'cutover.json').read_text() if (receipts/'cutover.json').exists() else capsys.readouterr().out
    journal = json.loads((receipts/'cutover.json').read_text())
    assert journal['ok'] and 'error_code' not in journal
    actions = [p['action'] for p in journal['phases']]
    assert actions == history_cutover._sequence(value)
    assert all(p['receipt']['complete'] for p in journal['phases'] if p['action'] in ('prepare-v3','publish-v3'))
    prepared = next(p['receipt'] for p in journal['phases'] if p['action']=='prepare-v3')
    capture_summary=json.loads((work/'aliases/legacy-alias-manifest.json').read_text())['capture_summary']
    for counter in ('duplicate_mapped_sources','historical_backfill_aliases'):
        assert prepared[counter]==capture_summary[counter]
    assert sum(a.startswith('smoke-reader-') for a in actions)==6
    if not file_invocation:
        assert any(items==[('synthetic.json','c3ludGhldGlj')] for items in decoded)
    assert not any('decoder_not_supplied' in p.read_text() for p in (work/'staged').rglob('*.jsonl'))


@pytest.mark.parametrize('mode',['rehearsal','live'])
@pytest.mark.parametrize('key','config_path pause_path knowledge_db processing_db frozen_files frozen_watermarks prepared_text_manifest prepared_text_manifest_sha256 original_home'.split())
def test_missing_host_inputs_refused_before_first_phase(tmp_path,mode,key):
    from scripts import history_cutover as m
    path,home,value = record(tmp_path)
    value['mode'] = mode
    value['inventory'].pop(key,None)
    value['digest'] = m.record_digest(value)
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError,match=f'missing_inventory_key:{key}'):
        m._load(path,home,apply=False)
    assert not Path(value['receipts']).exists()


@pytest.mark.parametrize('key','pinned_files prior_pinned_files source_dir prior_source_dir tool_python yeoman prior_yeoman gateway_socket'.split())
def test_missing_live_host_inputs_refused_before_first_phase(tmp_path,key):
    from scripts import history_cutover as m
    path,home,value = record(tmp_path)
    value['mode'] = 'live'
    value['inventory'].pop(key)
    value['digest'] = m.record_digest(value)
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError,match=f'missing_inventory_key:{key}'):
        m._load(path,home,apply=False)
    assert not Path(value['receipts']).exists()


@pytest.mark.parametrize('blocked_receipts', [False, True])
def test_cli_refusal_private_traceback_and_fallback(tmp_path, monkeypatch, capsys, blocked_receipts):
    m = procedure()
    path, home, value = record(tmp_path)
    receipts = Path(value['receipts'])
    if blocked_receipts:
        receipts.write_text('synthetic occupied path')
    monkeypatch.setattr(m, '_cli_run', lambda _: (_ for _ in ()).throw(KeyError('synthetic private detail')))
    monkeypatch.setattr(sys, 'argv', ['cutover', 'cutover', '--record', str(path), '--home', str(home)])
    assert m.main() == 1
    assert json.loads(capsys.readouterr().out) == dict(ok=False,error='cutover_refused')
    directory = path.parent if blocked_receipts else receipts
    diagnostics = list(directory.glob('refusal-*.txt'))
    assert len(diagnostics) == 1
    assert diagnostics[0].stat().st_mode & 0o777 == 0o600
    assert "KeyError: 'synthetic private detail'" in diagnostics[0].read_text()
    assert 'Traceback (most recent call last)' in diagnostics[0].read_text()
    assert m.main() == 1
    capsys.readouterr()
    assert len(list(directory.glob('refusal-*.txt'))) == 2


def test_record_create_approve_immediate_apply_in_subprocess(tmp_path):
    import subprocess
    code = """
import json, sys, time, subprocess
from pathlib import Path
from tests.gateway.test_history_cutover import record
from scripts import history_cutover as m, history_cutover_probes as probes
from yeoman_gateway.history.convert import bridge_refs
from scripts.history_cutover_inputs import build_cutover_record
from yeoman_gateway.knowledge._store import KnowledgeStore
root = Path(sys.argv[1])
path, home, old = record(root)
# This CLI witness uses the real authenticated writer-off baseline.
(home/'knowledge.db').unlink()
KnowledgeStore(home/'knowledge.db').close()
next(item for item in old['inventory']['members'] if item['path']=='knowledge.db')['kind'] = 'sqlite'
inventory = json.loads((Path.cwd()/'scripts/history_cutover_inventory.example.json').read_text())
inventory.update(home=str(home),output=old['output'],receipts=old['receipts'],python=sys.executable,rehearsal_root=str(root))
inventory['inventory'].update(old['inventory'],bridge_package_dir=old['bridge_package_dir'])
inventory['inventory']['host_crontab']['danger_minutes'] = []
source = root/'inventory.json'
source.write_text(json.dumps(inventory))
now = int(time.time()*1000)
value = build_cutover_record(inventory=source,layout={},mode='rehearsal',window=(now,now+60000),expected_gateway_jobs=0)
assert value['approved'] is False
path.write_text(json.dumps(value))
value.update(approved=True,approval='synthetic-immediate-owner')
value['digest'] = m.record_digest(value)
path.write_text(json.dumps(value))
m._sequence = lambda _: ['verify-quiescent','acquire','stop-overseer-clean','start-timers']
probes.build_probes = lambda **_: {}
bridge_refs.node_batch_decoder = lambda _: lambda rows: {}
def refuse(argv, **kwargs):
    raise AssertionError('host subprocess forbidden')
subprocess.run = refuse
sys.argv = ['cutover','cutover','--record',str(path),'--home',str(home),'--controls','rehearsal','--apply']
assert m.main() == 0
receipt = json.loads((Path(value['receipts'])/'cutover.json').read_text())
assert receipt['ok'] and receipt['phases'][-1]['action'] == 'start-timers'
assert receipt['phases'][0]['receipt']['writers_absent']
assert receipt['phases'][1]['frozen_baseline_digest']
"""
    result = subprocess.run([sys.executable,'-c',code,str(tmp_path)],capture_output=True,text=True,timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr
