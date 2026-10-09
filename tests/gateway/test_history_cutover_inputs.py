"""Real-schema acquired copies, never runtime stores or host commands."""
import argparse
import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import asdict

import pytest
from yeoman_gateway.history.attestations import make
from yeoman_gateway.history.convert.journal import _event
from yeoman_gateway.history.convert.run import prepare_import_manifest
from yeoman_gateway.history.layer1 import row_sha256
from yeoman_gateway.history.live import HistoryBoundary
from yeoman_gateway.history.project import project
from yeoman_gateway.history.queries import HistoryQueries
from yeoman_gateway.history.reader import HistoryReader
from yeoman_gateway.knowledge._store import KnowledgeStore
from yeoman_gateway.processing.store import ProcessingStore
from yeoman_shared.raw_archive.records import dumps, enumerate_committed

from tests.gateway.test_history_cutover import procedure, record


@pytest.fixture
def acquired(tmp_path):
    home = tmp_path / 'acquired'
    knowledge = home / 'data/knowledge/knowledge.db'
    knowledge.parent.mkdir(parents=True)
    k = KnowledgeStore(knowledge)
    k.set_meta('migration_complete', '1')
    k.set_meta('statement_capture_boundary_ms', '20')
    k.set_meta('statement_capture_boundary_event_id', 'cursor')
    k.close()
    processing = home / 'data/ops/processing.db'
    p = ProcessingStore(processing)
    p.close()
    phone = '10001@s.whatsapp.net'
    ts = 1_800_000_000_000
    raw = home / 'raw'
    (raw / 'owner').mkdir(parents=True)
    (raw / 'owner/attestations.jsonl').write_text('\n'.join(dumps(x) for x in (
        make('contact', 1, 'synthetic', identifiers=[phone], name='Synthetic'),
        make('identifier', 1, 'synthetic', anchor=phone, identifier=phone, valid_from_ms=1),
    )) + '\n')
    (raw / 'whatsapp').mkdir()
    lines = []
    events = []
    for name, created in (('mapped', 21), ('missing', 22), ('changed', 23),
                          ('purged', 24), ('ambiguous', 25), ('historical', 1), ('gap', 15)):
        payload = dict(provider_message_id=name, text='Synthetic '+name,
                       media=None, reply_to_message_id=None, mentions=None)
        event = dict(event_id=name, event_key=name, trace_id=name, kind='message',
                     origin='whatsapp', channel='whatsapp', chat_id=phone,
                     principal='whatsapp:10001', source_message_id=name,
                     target_message_id=None, thread_id=None, turn_id=None,
                     occurred_ms=ts, payload_hash=row_sha256(payload),
                     payload_json=dumps(payload), payload_purged_ms=None, created_ms=created)
        events.append(event)
        lines.append(dict(account='synthetic', archive_version=1, channel='whatsapp',
                          chat_id=phone, direction='in', kind='message', received_ms=100,
                          correlation_id='', media=None, native=dict(type='message', payload=dict(
                              chatJid=phone, senderId=phone, timestamp=ts, messageId=name,
                              text='Synthetic current' if name == 'changed' else payload['text']))))
    (raw / 'whatsapp/messages.jsonl').write_text('\n'.join(map(dumps, lines))+'\n')
    projected = tmp_path/'fixture-history.db'
    project([raw],projected)
    reader = HistoryReader(projected)
    snap = reader.open_snapshot(HistoryBoundary(1,enumerate_committed(raw)))
    try:
        person = HistoryQueries(snap).message('whatsapp:'+phone+':mapped')['sender_contact_id']
    finally:
        snap.close()
    reader.close()
    with sqlite3.connect(knowledge) as db:
        db.execute('INSERT INTO contacts (id,display_name,phone_number,is_owner,created_at,updated_at) VALUES (?,?,?,?,?,?)',(person,'Synthetic',None,0,'synthetic','synthetic'))
        db.execute('INSERT INTO knowledge_identifier_bindings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   ('binding','whatsapp','phone_jid','synthetic',phone,person,'active',1,0,1,'synthetic',1,1,1,1))
        db.commit()
        db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    from yeoman_gateway.knowledge.api import KnowledgeService
    from yeoman_gateway.knowledge.authority import EvidenceAudience
    from yeoman_gateway.knowledge.models import SourceRef, StatementCandidate, TrustedCaptureContext
    from yeoman_gateway.knowledge.runtime import RuntimeKnowledgePolicy, RuntimeKnowledgeSources
    source = SourceRef('mapped',1,'whatsapp',phone,'whatsapp:10001',ts)
    authority = RuntimeKnowledgeSources()
    authority.register_source(source,EvidenceAudience.author_only())
    service = KnowledgeService(store=KnowledgeStore(knowledge),workspace_id='synthetic',
        source_authority=authority,policy_authority=RuntimeKnowledgePolicy(engine=None))
    context = TrustedCaptureContext('synthetic',1,'native',(source,))
    service.capture(StatementCandidate('Curated synthetic original',(source,)),context=context)
    service.enqueue_capture((source,),context=context)
    service.close()
    with sqlite3.connect(processing) as db:
        for event in events:
            db.execute('INSERT INTO events ('+','.join(event)+') VALUES ('+','.join('?' for _ in event)+')', tuple(event.values()))
        for name in ('mapped','missing','changed','purged','ambiguous','other'):
            db.execute('INSERT INTO event_source_authority VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (name, 1, 'whatsapp:10001', 'telegram' if name=='other' else 'whatsapp',
                        phone, ts, 'author_only', '[]', None, 'synthetic',
                        200 if name=='purged' else None, None, 1, 1))
        db.row_factory = sqlite3.Row
        events = [dict(row) for row in db.execute('SELECT * FROM events ORDER BY rowid')]
        db.commit()
        db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    staged = tmp_path / 'staged'
    (staged / 'backfill').mkdir(parents=True)
    preserved = [_event(e) for e in events if e['event_id'] != 'missing']
    preserved.append(preserved[0])
    second = dict(events[4], source_message_id='changed')
    second['payload_json'] = dumps(dict(provider_message_id='changed', text='Synthetic ambiguous',
                                      media=None, reply_to_message_id=None, mentions=None))
    # A second acquired journal copy contains the competing original.
    backup = home / 'copies/processing.db'
    backup.parent.mkdir()
    with sqlite3.connect(processing) as src, sqlite3.connect(backup) as dst:
        src.backup(dst)
        dst.execute('UPDATE events SET source_message_id=?,payload_json=? WHERE event_id=?',
                    ('changed', second['payload_json'], 'ambiguous'))
        dst.commit()
        dst.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    alt = _event(second)
    alt['origin']['path'] = 'copies/processing.db'
    preserved.append(alt)
    (staged / 'backfill/journal.jsonl').write_text('\n'.join(map(dumps,preserved))+'\n')
    manifest = tmp_path / 'conversion.json'
    manifest.write_text(dumps(prepare_import_manifest(staged)))
    for database in (knowledge,processing,backup):
        with closing(sqlite3.connect(database)) as db:
            assert db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0] == 0
    members = [dict(path=str(p.relative_to(home)), kind='sqlite', restore=True, exists=True,
                    sha256=hashlib.sha256(p.read_bytes()).hexdigest()) for p in (knowledge,processing,backup)]
    files = {str(p.relative_to(raw)): hashlib.sha256(p.read_bytes()).hexdigest() for p in raw.rglob('*') if p.is_file()}
    members.append(dict(path='raw',kind='tree',restore=False,role='raw',exists=True,files=files,sha256=procedure().record_digest(files)))
    receipt = dict(version=1,ok=True,members=members,sources=[asdict(s) for s in enumerate_committed(raw)])
    receipt['digest'] = procedure().record_digest(receipt)
    (home / 'manifest.json').write_text(dumps(receipt))
    evidence = tmp_path / 'forward.json'
    evidence.write_text(dumps(dict(forward_start_ms=10,legacy_reinit_ms=20,log_sha256='a'*64,source='synthetic-log')))
    return home, manifest, staged, evidence, knowledge, raw


def build(acquired, output):
    from scripts.history_cutover_inputs import build_cutover_inputs
    home, manifest, staged, evidence, *_ = acquired
    return build_cutover_inputs(acquisition_home=home,conversion_manifest=manifest,
                               staged_raw=staged,forward_start_evidence=evidence,output=output)


def test_builder_preserves_originals_and_missing_fields(acquired, tmp_path):
    output = tmp_path/'inputs.json'
    summary = build(acquired,output)
    bundle = json.loads(output.read_text())
    assert set(summary) == {'legacy_rows','preserved_rows','capture_rows'}
    assert bundle['version'] == 1 and summary['legacy_rows'] == 6
    assert output.stat().st_mode & 0o777 == 0o600
    assert bundle['forward_start_evidence']['legacy_reinit_ms'] == 20
    for row in bundle['preserved_rows']:
        assert row['origin']['row_sha256'] == row_sha256(row['preserved_original'])
        assert row['envelope_sha256'] == row_sha256(row['original'])
    assert all(r.get('author_contact_id') for r in bundle['legacy_rows'] if r['channel']=='whatsapp')
    assert {r['message_id'] for r in bundle['capture_rows']} == {
        f'whatsapp:10001@s.whatsapp.net:{name}' for name in
        ('mapped','missing','changed','purged','ambiguous','historical','gap')}


def test_builder_feeds_prepare_full_native_prefix(acquired,tmp_path):
    from scripts.prepare_history_cutover import prepare
    home, _, _, _, knowledge, raw = acquired
    output = home/'cutover-inputs.json'
    build(acquired,output)
    history = tmp_path/'history.db'
    project([raw],history)
    policy = tmp_path/'policy-copy.json'
    policy.write_text(dumps({'defaults':{'whoCanTalk':{'mode':'everyone'}}}))
    result = prepare(argparse.Namespace(snapshot_home=home, history_db=history,
        knowledge_source=knowledge, knowledge_target=tmp_path/'v3.db',policy_snapshot=policy,output_root=tmp_path/'aliases'))
    assert result['total'] == 6 and result['handover']
    assert result['mapped'] == 1 and result['missing'] == 1 and result['ambiguous'] == 1
    assert result['changed'] == 1 and result['purged_revoked'] == 1 and result['other_channel'] == 1
    receipt = json.loads((tmp_path/'aliases/legacy-alias-manifest.json').read_text())
    assert len(receipt['handover']['classifications']) == 6
    assert receipt['handover']['classifications']['whatsapp:10001@s.whatsapp.net:historical'] == 'historical_not_selected'
    assert receipt['handover']['classifications']['whatsapp:10001@s.whatsapp.net:gap'] == 'pending'
    with sqlite3.connect(tmp_path/'v3.db') as target,sqlite3.connect(knowledge) as original:
        assert target.execute('SELECT count(*) FROM knowledge_history_capture').fetchone()[0] == 7
        assert target.execute('SELECT * FROM knowledge_jobs').fetchall() == original.execute('SELECT * FROM knowledge_jobs').fetchall()
        assert target.execute('SELECT * FROM knowledge_statements').fetchall() == original.execute('SELECT * FROM knowledge_statements').fetchall()


def test_builder_refuses_tampered_copy_and_unsafe_paths(acquired,tmp_path,monkeypatch):
    acquired[4].write_bytes(b'changed')
    with pytest.raises(ValueError,match='acquisition'):
        build(acquired,tmp_path/'inputs.json')
    from scripts.history_cutover_inputs import build_cutover_inputs
    monkeypatch.setattr(type(tmp_path),'read_bytes',lambda _:pytest.fail('unsafe read'))
    with pytest.raises(ValueError,match='runtime'):
        build_cutover_inputs(acquisition_home=tmp_path/'safe',conversion_manifest=tmp_path/'m',
            staged_raw=tmp_path/'s',forward_start_evidence=tmp_path/'e',output=type(tmp_path)('/home/dm/.yeoman/data/refused'))


def test_missing_payload_proof_remains_absent(acquired,tmp_path):
    home,manifest,staged,_,_,_ = acquired
    processing = home/'data/ops/processing.db'
    with sqlite3.connect(processing) as db:
        db.row_factory = sqlite3.Row
        event = dict(db.execute("SELECT * FROM events WHERE event_id='mapped'").fetchone())
        payload = json.loads(event['payload_json'])
        del payload['media']
        event['payload_json'] = dumps(payload)
        db.execute("UPDATE events SET payload_json=? WHERE event_id='mapped'",(event['payload_json'],))
        db.commit()
        db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    path = staged/'backfill/journal.jsonl'
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows = [_event(event) if row['original']['event_id']=='mapped' else row for row in rows]
    path.write_text('\n'.join(map(dumps,rows))+'\n')
    manifest.write_text(dumps(prepare_import_manifest(staged)))
    receipt = json.loads((home/'manifest.json').read_text())
    next(m for m in receipt['members'] if m['path']=='data/ops/processing.db')['sha256'] = hashlib.sha256(processing.read_bytes()).hexdigest()
    receipt['digest'] = procedure().record_digest(receipt)
    (home/'manifest.json').write_text(dumps(receipt))
    out = tmp_path/'missing-proof.json'
    build(acquired,out)
    bundle = json.loads(out.read_text())
    assert all('media_json' not in r['original']['state'] for r in bundle['preserved_rows'] if r['event_id']=='mapped')


def test_segment_refs_preserve_original_without_inventing_state(acquired,tmp_path):
    _,manifest,staged,*_ = acquired
    path = staged/'backfill/journal.jsonl'
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    candidate = next(row for row in rows if row['original']['event_id']=='changed')
    candidate['payload'] = {'segments':[candidate['payload'],{'messageId':'synthetic-segment'}]}
    path.write_text('\n'.join(map(dumps,rows))+'\n')
    manifest.write_text(dumps(prepare_import_manifest(staged)))
    out = tmp_path/'segments.json'
    build(acquired,out)
    bundle = json.loads(out.read_text())
    copies = [r for r in bundle['preserved_rows'] if r['event_id']=='changed']
    assert len(copies)==2
    assert {r['source_ref'].rsplit('/',1)[-1] for r in copies} == {'0','1'}
    assert all(r['original']['state']=={} for r in copies)
    assert all(r['origin']['row_sha256']==row_sha256(r['preserved_original']) for r in copies)


@pytest.mark.parametrize('mode',['rehearsal','live'])
def test_record_builder_loads_and_refuses_drift(tmp_path,mode):
    from scripts.history_cutover_inputs import build_cutover_record
    path,home,value = record(tmp_path)
    inventory = tmp_path/'inventory.json'
    value['inventory']['units'] = [dict(name='synthetic.service',restart='always',executable='/synthetic/writer')]
    from pathlib import Path
    example = json.loads((Path(__file__).parents[2]/'scripts/history_cutover_inventory.example.json').read_text())
    value['inventory'].update(overseer_jobs=[],external_text_targets=[],gateway_jobs=0,manual_routes=[],
        forward_start_evidence_member='inputs/forward-start.json',reader_smoke=example['inventory']['reader_smoke'])
    inventory.write_text(dumps({k:v for k,v in value.items() if k in ('home','output','receipts','candidate','prior','inventory','rehearsal_root')} | {'version':1,'python':__import__('sys').executable}))
    generated = build_cutover_record(inventory=inventory,layout={},mode=mode,
        window=(1791532800000,1791534600000),expected_gateway_jobs=0)
    assert generated['approved'] is False and generated['mode'] == mode
    path.write_text(dumps(generated))
    assert procedure()._load(path,home,apply=False) == generated
    with pytest.raises(ValueError,match='cron_inventory_drift'):
        build_cutover_record(inventory=inventory,layout={},mode=mode,
            window=(1791532800000,1791534600000),expected_gateway_jobs=1)
    generated['inventory']['gateway_jobs'] = 2
    path.write_text(dumps(generated))
    with pytest.raises(ValueError,match='record_pin_mismatch'):
        procedure()._load(path,home,apply=False)


def original_enrichment(legacy,statements,links,bindings,parsed_jobs,event_by_id,boundary,start):
    """Pre-optimization metadata join, retained only as a small-fixture byte oracle."""
    for key,row in legacy.items():
        people = {statements[link['statement_id']]['speaker_person_id'] for link in links
            if (link['event_id'],link['revision'])==key and statements[link['statement_id']]['speaker_person_id']}
        number = row['author_principal'].removeprefix('whatsapp:')
        people.update(b['person_id'] for b in bindings if b['channel']==row['channel']
            and b['kind']=='phone_jid' and b['value']==number+'@s.whatsapp.net'
            and b['mapping_verified']==1 and b['status'] in ('active','ended')
            and b['valid_from_ms'] <= row['occurred_at_ms']
            and (not b['valid_until_ms'] or row['occurred_at_ms'] < b['valid_until_ms']))
        if len(people)==1:
            row['author_contact_id'] = next(iter(people))
        event = event_by_id.get(key[0])
        if event:
            row.update(created_ms=event['created_ms'],boundary=boundary,forward_start=start)
        if any((link['event_id'],link['revision'])==key and statements[link['statement_id']]['status'] in
            ('assertion','confirmed','superseded','expired') for link in links):
            row['completed'] = True
        if any(j['state']=='done' and any((s['event_id'],s['revision'])==key for s in json.loads(j['sources_json'])) for j,_ in parsed_jobs):
            row['completed'] = True


@pytest.mark.parametrize('variant',['original','missing_fields','segments'])
def test_indexed_builder_matches_original_bytes(acquired,tmp_path,monkeypatch,variant):
    from scripts import history_cutover_inputs as module
    if variant=='missing_fields':
        test_missing_payload_proof_remains_absent(acquired,tmp_path)
    elif variant=='segments':
        test_segment_refs_preserve_original_without_inventing_state(acquired,tmp_path)
    first,reference = tmp_path/'indexed.json',tmp_path/'reference.json'
    build(acquired,first)
    monkeypatch.setattr(module,'_enrich_legacy',original_enrichment)
    build(acquired,reference)
    assert first.read_bytes()==reference.read_bytes()


def test_prepare_withholds_statement_with_unissued_source(acquired,tmp_path):
    from scripts.prepare_history_cutover import prepare
    home,_,_,_,knowledge,raw = acquired
    output = home/'cutover-inputs.json'
    build(acquired,output)
    bundle = json.loads(output.read_text())
    row = next(r for r in bundle['legacy_rows'] if r['event_id']=='missing')
    row['author_principal'] = ''
    with sqlite3.connect(knowledge) as db:
        db.row_factory = sqlite3.Row
        link = dict(db.execute('SELECT * FROM knowledge_statement_sources LIMIT 1').fetchone())
        link.update(event_id='missing',author_principal='')
        db.execute('INSERT INTO knowledge_statement_sources ('+','.join(link)+') VALUES ('+','.join('?' for _ in link)+')',tuple(link.values()))
        db.commit()
        db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    output.write_text(dumps(bundle))
    history = tmp_path/'history.db'
    project([raw],history)
    policy = tmp_path/'policy-copy.json'
    policy.write_text(dumps({'defaults':{'whoCanTalk':{'mode':'everyone'}}}))
    result = prepare(argparse.Namespace(snapshot_home=home,history_db=history,knowledge_source=knowledge,
        knowledge_target=tmp_path/'v3.db',policy_snapshot=policy,output_root=tmp_path/'aliases'))
    assert result['handover'] and result['missing']==1 and result['withheld_statements']==1
    manifest = json.loads((tmp_path/'aliases/legacy-alias-manifest.json').read_text())
    entry = next(e for e in manifest['entries'] if e['issued']['event_id']=='missing')
    assert entry['status']=='missing' and entry['reason']=='unissued_principal'
    assert entry['issued']['author_principal']=='' and 'alias' not in entry
