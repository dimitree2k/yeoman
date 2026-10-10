"""Synthetic smoke proof and fake-clock owner wait; never contact a transport."""
import hashlib
import json
import sqlite3
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from tests.gateway.test_history_cutover import procedure, record
from tests.gateway.test_history_cutover_host import host_module, payload


class FakeClock:
    now = 0
    def monotonic(self):
        return self.now
    def time(self):
        return 10
    def sleep(self, seconds):
        self.now += seconds
        if hasattr(self, 'publish'):
            self.publish()


def proof():
    return dict(inbound_message_id_hash='a'*64, outbound_receipt_hash='b'*64, observed_ms=10000)


def wait_payload(tmp_path):
    p = payload(tmp_path)
    p['record'].update(mode='live', owner_ack_timeout_seconds=30)
    p['receipts'] = [dict(action='release-fence', ended_ns=1000000000, receipt=dict(ok=True))]
    return p


def publish(p, **changes):
    root = Path(p['record']['receipts'])
    root.mkdir(exist_ok=True)
    value = dict(action='functional-smoke', record_digest=p['record']['digest'], owner_ack=True, proof=proof())
    value.update(changes)
    (root/'functional-smoke.owner_ack.json').write_text(json.dumps(value))


def test_wait_delayed_ack_and_rehearsal_immediate(tmp_path):
    p = wait_payload(tmp_path)
    clock = FakeClock()
    clock.publish = lambda: publish(p)
    h = host_module()
    assert h._ack('functional-smoke', p, wait=True, clock=clock)['ok']
    assert clock.now > 0
    (Path(p['record']['receipts'])/'functional-smoke.owner_ack.json').unlink()
    with pytest.raises(FileNotFoundError):
        h._ack('functional-smoke', p, clock=clock)


def test_timeout_refences_after_release(tmp_path):
    m = procedure()
    _, home, value = record(tmp_path)
    value['owner_ack_timeout_seconds'] = 30
    calls = []
    h = host_module()
    clock = FakeClock()
    pause = Path(value['inventory']['pause_path'])
    pause.parent.mkdir(parents=True, exist_ok=True)
    pause.write_text(FENCED_PAUSE)
    digest = hashlib.sha256(pause.read_bytes()).hexdigest()
    def control(action, p):
        calls.append(action)
        if action == 'functional-smoke':
            return h._ack(action, p, wait=True, clock=clock)
        return dict(ok=True, fenced=True, prior_pauses_preserved=True,
                    pause_baseline_sha256=digest, prior_pauses_sha256=digest)
    with m.injected_controls(control):
        result = m._run(value, home, ['fence-effects', 'release-fence', 'functional-smoke'], record_dir=tmp_path)
    journal = json.loads(Path(result['receipt']).read_text())
    assert journal['error_code'] == 'owner_ack_timeout'
    assert result['fenced'] and result['fence_verified']
    assert calls == ['fence-effects', 'frozen-watermarks', 'release-fence', 'functional-smoke', 'fence-effects']
    assert clock.now == 30


@pytest.mark.parametrize('change', [dict(record_digest='foreign'), dict(action='other'), dict(proof={}),
    dict(proof=proof() | {'ok': True}), dict(proof=proof() | {'action':'other'}),
    dict(proof=proof() | {'record_digest':'foreign'}), dict(proof=proof() | {'observed_ms':True}),
    dict(proof=proof() | {'observed_ms':0}), dict(owner_ack=1)])
def test_wait_rejects_malformed_proof_without_polling(tmp_path, change):
    p = wait_payload(tmp_path)
    publish(p, **change)
    clock = FakeClock()
    with pytest.raises(ValueError, match='owner_ack'):
        host_module()._ack('functional-smoke', p, wait=True, clock=clock)
    assert clock.now == 0


@pytest.mark.parametrize('timeout', [0, -1, True, 1201, '30', 1.5])
def test_invalid_timeout_refused_before_phases(tmp_path, timeout):
    m = procedure()
    path, home, value = record(tmp_path)
    value['owner_ack_timeout_seconds'] = timeout
    value['digest'] = m.record_digest(value)
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match='owner_ack_timeout'):
        m._load(path, home, apply=False)


FENCED_PAUSE = '{"version": 1, "global_until_ms": -1, "chat_until_ms": {}}'
RELEASED_PAUSE = '{"version": 1, "global_until_ms": 0, "chat_until_ms": {}}'


def write_pause(home, content=RELEASED_PAUSE):
    path = home/'data/ops/response-pauses.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def smoke_case(tmp_path):
    from yeoman_gateway.processing.store import ProcessingStore
    from yeoman_shared.raw_archive.records import enumerate_committed
    _, home, value = record(tmp_path)
    raw = home/'data/raw'
    (raw/'whatsapp').mkdir(parents=True)
    inbound = dict(channel='whatsapp', chat_id='synthetic-chat', native_id='synthetic-in',
        kind='message', direction='in', received_ms=2000,
        native=dict(type='message', payload=dict(chatJid='synthetic-chat', messageId='synthetic-in',providerTimestampMs=2000)))
    outbound_request = dict(channel='whatsapp',chat_id='synthetic-chat',kind='outbound_request',direction='out',
        received_ms=3000,correlation_id='synthetic-request',native=dict(type='send_text',requestId='synthetic-request',payload={}))
    outbound_result = dict(channel='whatsapp',chat_id='synthetic-chat',kind='outbound_result',direction='out',
        received_ms=4000,correlation_id='synthetic-request',native_id='synthetic-out',
        native=dict(type='send_text',requestId='synthetic-request',result=dict(sent=dict(providerMessageId='synthetic-out'))))
    (raw/'whatsapp/2026-10.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in (inbound,outbound_request,outbound_result)))
    history = home/'data/history/history.db'
    history.parent.mkdir()
    with sqlite3.connect(history) as db:
        db.executescript('CREATE TABLE projector_state(file TEXT,state_json TEXT,lines INTEGER,end_offset INTEGER,sha256 TEXT); '
            'CREATE TABLE messages(channel TEXT,chat_id TEXT,native_message_id TEXT,direction TEXT,provenance TEXT,source_refs TEXT);')
        db.execute('INSERT INTO projector_state(file,state_json) VALUES (?,?)', ('@runtime', json.dumps(dict(status='ready', generation=3))))
        for b in enumerate_committed(raw):
            db.execute('INSERT INTO projector_state VALUES (?,NULL,?,?,?)', (b.relative_path,b.line_number,b.end_offset,b.prefix_sha256))
        db.execute('INSERT INTO messages VALUES (?,?,?,?,?,?)', ('whatsapp','synthetic-chat','synthetic-in','in','native',json.dumps(['whatsapp/2026-10.jsonl#1'])))
    processing = home/'data/ops/processing.db'
    processing.parent.mkdir(parents=True, exist_ok=True)
    ProcessingStore(processing).close()
    with sqlite3.connect(processing) as db:
        db.execute("INSERT INTO events(event_id,event_key,trace_id,kind,origin,channel,chat_id,direction,source_message_id,payload_hash,created_ms) VALUES ('synthetic-event','synthetic-key','synthetic-trace','message','whatsapp','whatsapp','synthetic-chat','in','synthetic-in','synthetic-hash',2000)")
        db.execute("INSERT INTO effects(effect_id,operation_key,trace_id,payload_kind,payload_hash,payload_json,target_json,state,created_ms,updated_ms) VALUES ('synthetic-effect','synthetic-operation','synthetic-trace','text','synthetic-hash','{}',?, 'sent',3000,3000)", (json.dumps(dict(channel='whatsapp',chat_id='synthetic-chat')),))
        db.execute("INSERT INTO transport_receipts(receipt_id,effect_id,channel,chat_id,provider_message_id,confirmed_ms) VALUES ('synthetic-receipt','synthetic-effect','whatsapp','synthetic-chat','synthetic-out',4000)")
    value.update(python='/synthetic/python',mode='live',owner_ack_timeout_seconds=1200,layout=dict(raw=str(raw),history=str(history)))
    value['inventory'].update(config_path=str(home/'config.json'),processing_db=str(processing))
    value['digest'] = procedure().record_digest(value)
    path = tmp_path/'approved-record.json'
    path.write_text(json.dumps(value))
    receipts = Path(value['receipts'])
    receipts.mkdir()
    actions = procedure()._sequence(value)
    # The acquisition the smoke must bind to: the authenticated owner-stop fence.
    fenced = write_pause(home, FENCED_PAUSE)
    fence_digest = hashlib.sha256(fenced.read_bytes()).hexdigest()
    (receipts/f'cutover-{actions.index("acquire")+1:02}.json').write_text(json.dumps(dict(
        action='acquire', record_digest=value['digest'],
        pause_baseline=dict(path=str(fenced), sha256=fence_digest, global_until_ms=-1, chat_keys=[]))))
    for action in ('release-fence','functional-smoke'):
        phase = dict(action=action,record_digest=value['digest'],started_ns=1000000000)
        if action == 'release-fence':
            phase.update(ended_ns=1000000000,receipt=dict(ok=True,prior_pauses_preserved=True,
                                                          prior_pauses_sha256=fence_digest))
        suffix = '' if action == 'release-fence' else '-started'
        (receipts/f'cutover-{actions.index(action)+1:02}{suffix}.json').write_text(json.dumps(phase))
    # The owner released the persistent control before the smoke helper ran.
    write_pause(home, RELEASED_PAUSE)
    inputs = receipts/'functional-smoke.inputs.json'
    inputs.write_text(json.dumps(dict(record_digest=value['digest'],chat_id='synthetic-chat',inbound_native_id='synthetic-in',effect_id='synthetic-effect')))
    calls = []
    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        output = 'status=ok effects=1' if argv[-1]=='check-capture' else json.dumps(dict(status='ok',health=dict(status='ready',generation=3,lag_lines=0,lag_bytes=0)))
        return CompletedProcess(argv,0,output,'')
    return path, value, inputs, runner, calls


def test_helper_publishes_only_exact_private_hashed_ack(tmp_path):
    from scripts.history_cutover_smoke import publish_ack
    path, value, inputs, runner, calls = smoke_case(tmp_path)
    before = {p.relative_to(value['layout']['raw']):p.read_bytes() for p in Path(value['layout']['raw']).rglob('*') if p.is_file()}
    result = publish_ack(record=path, inputs=inputs, owner_confirmed_arrival=True, runner=runner, clock=FakeClock())
    ack = Path(value['receipts'])/'functional-smoke.owner_ack.json'
    observed = json.loads(ack.read_text())
    assert set(observed)=={'action','record_digest','owner_ack','proof'}
    assert set(observed['proof'])==set(proof())
    assert observed['owner_ack'] is True and result == dict(ok=True)
    assert ack.stat().st_mode & 0o777 == 0o600
    assert before == {p.relative_to(value['layout']['raw']):p.read_bytes() for p in Path(value['layout']['raw']).rglob('*') if p.is_file()}
    assert len(calls)==2
    assert calls[0][0][-2:]==['raw','check-capture']
    assert calls[1][0][-2:]==['history','projection-status']
    assert calls[0][1]['env']['YEOMAN_HOME']==value['home']
    assert all(call[1]['timeout']==1191 for call in calls)
    assert 'synthetic-in' not in ack.read_text() and 'synthetic-receipt' not in ack.read_text()


@pytest.mark.parametrize('defect', ['prior', 'unprojected','wrong-ref','missing-receipt','wrong-chat','wrong-cause',
    'not-sent','future','foreign-digest','foreign-inputs','foreign-store','unready','late','owner','unarchived-outbound','missing-request','wrong-generation','cli-failed','unarchived-inbound','release-digest','release-time','expired','malformed-inputs'])
def test_helper_refuses_incomplete_or_foreign_evidence(tmp_path, defect):
    from scripts.history_cutover_smoke import publish_ack
    path, value, inputs, runner, calls = smoke_case(tmp_path)
    processing = Path(value['inventory']['processing_db'])
    history = Path(value['layout']['history'])
    if defect in ('cli-failed', 'wrong-generation'):
        original = runner
        def runner(argv, **kwargs):
            result = original(argv, **kwargs)
            if defect=='cli-failed':
                result.returncode=1
            elif argv[-1]=='projection-status':
                result.stdout=result.stdout.replace('3','4')
            return result
    elif defect in ('unarchived-outbound','missing-request','unarchived-inbound'):
        raw = Path(value['layout']['raw'])/'whatsapp/2026-10.jsonl'
        lines = raw.read_text().splitlines()
        index = {'unarchived-outbound':2,'missing-request':1,'unarchived-inbound':0}[defect]
        raw.write_text(''.join(line+'\n' for i,line in enumerate(lines) if i!=index))
    elif defect in ('release-digest','release-time','expired'):
        release = Path(value['receipts'])/f'cutover-{procedure()._sequence(value).index("release-fence")+1:02}.json'
        data=json.loads(release.read_text())
        if defect=='release-digest':
            data['record_digest']='foreign'
        elif defect=='release-time':
            data['ended_ns']=20000000000
        else:
            value['owner_ack_timeout_seconds']=1
            value['digest']=procedure().record_digest(value)
            path.write_text(json.dumps(value))
            data['record_digest']=value['digest']
            for target in (inputs,Path(value['receipts'])/f'cutover-{procedure()._sequence(value).index("functional-smoke")+1:02}-started.json'):
                bound=json.loads(target.read_text())
                bound['record_digest']=value['digest']
                target.write_text(json.dumps(bound))
        release.write_text(json.dumps(data))
    elif defect=='malformed-inputs':
        inputs.write_text('[]')
    elif defect == 'prior':
        raw = Path(value['layout']['raw'])/'whatsapp/2026-10.jsonl'
        raw.write_text(raw.read_text().replace('2000','500'))
    elif defect in ('unprojected','wrong-ref','unready'):
        with sqlite3.connect(history) as db:
            if defect=='unprojected':
                db.execute('DELETE FROM messages')
            elif defect=='wrong-ref':
                db.execute("UPDATE messages SET source_refs='[]'")
            else:
                db.execute('UPDATE projector_state SET state_json=? WHERE file=?', (json.dumps(dict(status='paused',generation=3)), '@runtime'))
    elif defect in ('missing-receipt','wrong-chat','wrong-cause','not-sent','future'):
        with sqlite3.connect(processing) as db:
            db.execute({'missing-receipt':'DELETE FROM transport_receipts',
                'wrong-chat':"UPDATE transport_receipts SET chat_id='other'",
                'wrong-cause':"UPDATE effects SET trace_id='other'",
                'not-sent':"UPDATE effects SET state='unknown'",
                'future':'UPDATE transport_receipts SET confirmed_ms=20000'}[defect])
    elif defect=='foreign-digest':
        data = json.loads(inputs.read_text())
        data['record_digest']='foreign'
        inputs.write_text(json.dumps(data))
    elif defect=='foreign-inputs':
        other = tmp_path/'foreign.json'
        other.write_bytes(inputs.read_bytes())
        inputs=other
    elif defect=='foreign-store':
        value['inventory']['processing_db']=str(tmp_path/'foreign.db')
        value['digest']=procedure().record_digest(value)
        path.write_text(json.dumps(value))
    elif defect=='late':
        (Path(value['receipts'])/'cutover.json').write_text('{}')
    with pytest.raises(ValueError):
        publish_ack(record=path,inputs=inputs,owner_confirmed_arrival=defect!='owner',runner=runner,clock=FakeClock())
    assert not (Path(value['receipts'])/'functional-smoke.owner_ack.json').exists()


@pytest.mark.parametrize('timeout', [0, True, 1201])
def test_builder_validates_timeout_before_reading_inventory(tmp_path, timeout):
    from scripts.history_cutover_inputs import build_cutover_record
    with pytest.raises(ValueError, match='owner_ack_timeout'):
        build_cutover_record(inventory=tmp_path/'never-read.json',layout={},mode='live',
            window=(0,1),expected_gateway_jobs=0,owner_ack_timeout_seconds=timeout)


def test_helper_cli_reports_only_safe_refusal(tmp_path, capsys):
    from scripts.history_cutover_smoke import main
    assert main(['--record',str(tmp_path/'missing.json'),'--inputs',str(tmp_path/'inputs.json'),
                 '--owner-confirmed-arrival'])==1
    assert json.loads(capsys.readouterr().out)==dict(ok=False,error='smoke_ack_refused')


@pytest.mark.parametrize('role,causal', [('trigger',True),('context',True),('trigger',False)])
def test_helper_uses_frozen_source_for_turn_bound_reply(tmp_path, role, causal):
    from scripts.history_cutover_smoke import publish_ack
    path,value,inputs,runner,_ = smoke_case(tmp_path)
    with sqlite3.connect(value['inventory']['processing_db']) as db:
        db.execute("UPDATE effects SET trace_id='synthetic-turn',turn_id='synthetic-turn'")
        db.execute("INSERT INTO generations(generation_id,turn_id,thread_id,revision,context_version,snapshot_hash,created_ms) VALUES ('synthetic-generation','synthetic-turn','synthetic-thread',1,1,'synthetic-hash',2500)")
        db.execute("INSERT INTO generation_sources(generation_id,event_id,role) VALUES ('synthetic-generation',?,?)", ('synthetic-event' if causal else 'other',role))
    if causal:
        assert publish_ack(record=path,inputs=inputs,owner_confirmed_arrival=True,runner=runner,clock=FakeClock())['ok']
    else:
        with pytest.raises(ValueError,match='causal_reply'):
            publish_ack(record=path,inputs=inputs,owner_confirmed_arrival=True,runner=runner,clock=FakeClock())


def test_helper_rejects_old_provider_timestamp_even_if_newly_received(tmp_path):
    from yeoman_shared.raw_archive.records import enumerate_committed

    from scripts.history_cutover_smoke import publish_ack
    path,value,inputs,runner,_ = smoke_case(tmp_path)
    raw = Path(value['layout']['raw'])/'whatsapp/2026-10.jsonl'
    rows = [json.loads(line) for line in raw.read_text().splitlines()]
    rows[0]['native']['payload']['providerTimestampMs']=500
    raw.write_text(''.join(json.dumps(row)+'\n' for row in rows))
    with sqlite3.connect(value['layout']['history']) as db:
        for b in enumerate_committed(Path(value['layout']['raw'])):
            db.execute('UPDATE projector_state SET lines=?,end_offset=?,sha256=? WHERE file=?', (b.line_number,b.end_offset,b.prefix_sha256,b.relative_path))
    with pytest.raises(ValueError,match='inbound_not_fresh'):
        publish_ack(record=path,inputs=inputs,owner_confirmed_arrival=True,runner=runner,clock=FakeClock())
