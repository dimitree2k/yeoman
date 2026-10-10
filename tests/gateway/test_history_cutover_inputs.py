"""Real-schema acquired copies, never runtime stores or host commands."""
import argparse
import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import asdict
from pathlib import Path

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
    assert result['cited_reason_counts']=={}
    assert result['uncited_reason_counts']=={'ambiguous_locator':1,'no_preserved_original':1,'other_channel':1,'purged_revoked':1,'text_mismatch':1}
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
    assert result['cited_reason_counts']=={'unissued_principal':1}
    assert sum(result['uncited_reason_counts'].values())==4
    manifest = json.loads((tmp_path/'aliases/legacy-alias-manifest.json').read_text())
    entry = next(e for e in manifest['entries'] if e['issued']['event_id']=='missing')
    assert entry['status']=='missing' and entry['reason']=='unissued_principal'
    assert entry['issued']['author_principal']=='' and 'alias' not in entry


@pytest.mark.parametrize('store',['reply_context','inbound_archive','session_jsonl','memory_nodes','bridge_refs'])
def test_native_original_from_non_journal_store_maps(acquired,tmp_path,store):
    from yeoman_gateway.history.convert.bridge_refs import convert_bridge_refs
    from yeoman_gateway.history.convert.inbound_db import convert_inbound_db
    from yeoman_gateway.history.convert.memory_nodes import convert_memory_nodes
    from yeoman_gateway.history.convert.session_jsonl import convert_session_jsonl

    from scripts.prepare_history_cutover import prepare
    home,manifest,staged,_,knowledge,raw = acquired
    phone = '10001@s.whatsapp.net'
    ts = 1_800_000_000_000
    if store in ('reply_context','inbound_archive'):
        source = home/('data/'+store+'.db')
        with sqlite3.connect(source) as db:
            db.execute('CREATE TABLE inbound_messages (channel TEXT,chat_id TEXT,message_id TEXT,sender_id TEXT,text TEXT,timestamp INTEGER)')
            db.execute('INSERT INTO inbound_messages VALUES (?,?,?,?,?,?)',('whatsapp',phone,'missing',phone,'Synthetic missing',ts))
        records = list(convert_inbound_db(home,source.relative_to(home).as_posix(),store))
        member = dict(path=source.relative_to(home).as_posix(),kind='sqlite',exists=True,restore=True,sha256=hashlib.sha256(source.read_bytes()).hexdigest())
    else:
        folder = home/'data/inbound' if store=='session_jsonl' else home/('data/'+store)
        folder.mkdir(parents=True)
        if store=='session_jsonl':
            source = folder/('whatsapp_'+phone+'.jsonl')
            source.write_text(dumps(dict(role='user',message_id='missing',sender_id=phone,content='Synthetic missing',timestamp=ts))+'\n')
            records = list(convert_session_jsonl(home))
        elif store=='bridge_refs':
            source = folder/'reference.json'
            source.write_text(dumps(dict(chatJid=phone,encoded='synthetic-encoded',storedAtMs=ts)))
            decoded = dict(key=dict(id='missing',remoteJid=phone,participant=phone),
                           messageTimestamp=ts//1000,message=dict(conversation='Synthetic missing'))
            records = list(convert_bridge_refs([folder],lambda batch:{name:{'value':decoded} for name,_ in batch}))
        else:
            source = folder/'nodes.db'
            with sqlite3.connect(source) as db:
                db.execute('CREATE TABLE memory2_nodes (id TEXT,kind TEXT,channel TEXT,chat_id TEXT,sender_id TEXT,source_message_id TEXT,content TEXT,created_at INTEGER,source TEXT,source_role TEXT,meta_json TEXT,is_deleted INTEGER)')
                db.execute('INSERT INTO memory2_nodes VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',('node','utterance','whatsapp',phone,phone,'missing','Synthetic missing',ts,'native','user','{}',0))
            records = list(convert_memory_nodes(home,source.relative_to(home).as_posix(),store))
            # Memory only records approximate capture time: the native source time is
            # intentionally not invented; its identity still stays withheld.
        files = {p.relative_to(folder).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.rglob('*') if p.is_file()}
        member = dict(path=folder.relative_to(home).as_posix(),kind='tree',exists=True,restore=True,files=files,sha256=procedure().record_digest(files))
        if store=='memory_nodes':
            member = dict(path=source.relative_to(home).as_posix(),kind='sqlite',exists=True,restore=True,sha256=hashlib.sha256(source.read_bytes()).hexdigest())
    receipt = json.loads((home/'manifest.json').read_text())
    receipt['members'].append(member)
    receipt['digest'] = procedure().record_digest(receipt)
    (home/'manifest.json').write_text(dumps(receipt))
    (staged/('backfill/'+store+'.jsonl')).write_text('\n'.join(map(dumps,records))+'\n')
    manifest.write_text(dumps(prepare_import_manifest(staged)))
    build(acquired,home/'cutover-inputs.json')
    bundle = json.loads((home/'cutover-inputs.json').read_text())
    originals = [r for r in bundle['preserved_rows'] if r['event_id']=='missing']
    assert originals and originals[0]['origin']['store']==store
    assert originals[0]['origin']['row_sha256']==row_sha256(originals[0]['preserved_original'])
    history = tmp_path/'history.db'
    project([raw],history)
    policy = tmp_path/'policy-copy.json'
    policy.write_text(dumps({'defaults':{'whoCanTalk':{'mode':'everyone'}}}))
    result = prepare(argparse.Namespace(snapshot_home=home,history_db=history,knowledge_source=knowledge,
        knowledge_target=tmp_path/'v3.db',policy_snapshot=policy,output_root=tmp_path/'aliases'))
    assert result['mapped']==(1 if store=='memory_nodes' else 2)
    entries = json.loads((tmp_path/'aliases/legacy-alias-manifest.json').read_text())['entries']
    missing = next(e for e in entries if e['issued']['event_id']=='missing')
    assert missing['reason']==('no_author_or_text_proof' if store=='memory_nodes' else 'mapped')
    if store!='memory_nodes':
        assert 'text' in missing['proven_fields'] and 'media_json' not in missing['proven_fields']


@pytest.mark.parametrize('inferred',[False,True])
def test_original_identity_is_bound_without_projected_defaults(acquired,tmp_path,inferred):
    home,manifest,staged,*_ = acquired
    if inferred:
        from yeoman_gateway.history.convert.session_jsonl import convert_session_jsonl
        folder = home/'data/inbound'
        folder.mkdir(parents=True)
        source = folder/'whatsapp_10001@s.whatsapp.net.jsonl'
        source.write_text(dumps(dict(role='user',message_id='missing',content='Synthetic missing',timestamp=1_800_000_000_000))+'\n')
        rows = list(convert_session_jsonl(home))
        files = {source.name:hashlib.sha256(source.read_bytes()).hexdigest()}
        member = dict(path='data/inbound',kind='tree',exists=True,restore=True,files=files,sha256=procedure().record_digest(files))
    else:
        source = home/'data/ops/processing.db'
        with sqlite3.connect(source) as db:
            db.execute("UPDATE events SET event_id='journal-wrapper' WHERE event_id='missing'")
            db.row_factory = sqlite3.Row
            original = dict(db.execute("SELECT * FROM events WHERE event_id='journal-wrapper'").fetchone())
            db.commit()
            db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        rows = [_event(original)]
        member = dict(path='data/ops/processing.db',kind='sqlite',exists=True,restore=True,sha256=hashlib.sha256(source.read_bytes()).hexdigest())
    receipt = json.loads((home/'manifest.json').read_text())
    receipt['members'] = [m for m in receipt['members'] if m['path']!=member['path']]+[member]
    receipt['digest'] = procedure().record_digest(receipt)
    (home/'manifest.json').write_text(dumps(receipt))
    (staged/'backfill/identity.jsonl').write_text('\n'.join(map(dumps,rows))+'\n')
    manifest.write_text(dumps(prepare_import_manifest(staged)))
    output = tmp_path/'identity-inputs.json'
    build(acquired,output)
    bundle = json.loads(output.read_text())
    candidate, = [r for r in bundle['preserved_rows'] if r['event_id']=='missing']
    assert candidate['origin']['row_sha256']==row_sha256(candidate['preserved_original'])
    if inferred:
        assert 'issued' not in candidate['original']
        assert 'author_principal' not in candidate['original']['state']
    else:
        legacy = next(r for r in bundle['legacy_rows'] if r['event_id']=='missing')
        assert candidate['original']['issued']=={k:legacy[k] for k in candidate['original']['issued']}
        assert candidate['original']['state']['text']=='Synthetic missing'


def test_journal_proof_keeps_original_text_bytes(acquired):
    from scripts.history_cutover_inputs import _original_state
    home,*_ = acquired
    with sqlite3.connect(home/'data/ops/processing.db') as db:
        db.row_factory = sqlite3.Row
        original = dict(db.execute("SELECT * FROM events WHERE event_id='mapped'").fetchone())
    payload = json.loads(original['payload_json'])
    payload['text'] = '  Synthetic original bytes  '
    original['payload_json'] = dumps(payload)
    state = _original_state(_event(original),set())
    assert state['text']==payload['text'] and state['current_text']==payload['text']


def test_preserved_origins_use_manifest_source_inventory(acquired,tmp_path):
    _,manifest,*_ = acquired
    output = tmp_path/'inventory-inputs.json'
    build(acquired,output)
    bundle = json.loads(output.read_text())
    package = json.loads(manifest.read_text())
    for preserved in bundle['preserved_rows']:
        file,number = preserved['source_ref'].split('#')
        expected = package['files'][file]['rows'][int(number)-1]['origin']
        assert preserved['origin']==expected
        assert expected['path']==package['source_inventory'][expected['inventory_id']]['path']


@pytest.mark.parametrize('code',['conversion_origin_not_acquired','conversion_original_not_acquired'])
@pytest.mark.parametrize('optional',[False,True])
def test_origin_errors_are_counted_per_store_and_require_approved_record(acquired,tmp_path,code,optional):
    from scripts.history_cutover_inputs import build_cutover_inputs
    home,manifest,staged,evidence,*_ = acquired
    path = staged/'backfill/journal.jsonl'
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    if code=='conversion_origin_not_acquired':
        for row in rows:
            row['origin']['path'] = 'data/unacquired.db'
    else:
        for row in rows:
            row['original']['trace_id'] = 'changed-synthetic'
            row['origin']['row_sha256'] = row_sha256(row['original'])
    path.write_text('\n'.join(map(dumps,rows))+'\n')
    manifest.write_text(dumps(prepare_import_manifest(staged)))
    inventory = dict(optional_conversion_stores=['journal'] if optional else [])
    record = dict(version=1,approved=True,approval='synthetic-review',output=str(home),inventory=inventory)
    record['digest'] = procedure().record_digest(record)
    receipt = json.loads((home/'manifest.json').read_text())
    receipt['inventory_digest'] = procedure().record_digest(inventory)
    receipt['digest'] = procedure().record_digest(receipt)
    (home/'manifest.json').write_text(dumps(receipt))
    args = dict(acquisition_home=home,conversion_manifest=manifest,staged_raw=staged,
                forward_start_evidence=evidence,output=tmp_path/'errors-inputs.json',record=record)
    if optional:
        result = build_cutover_inputs(**args)
        assert result['origin_proof_errors']=={'journal':{code:len(rows)}}
        assert result['preserved_rows']==0
        assert json.loads(args['output'].read_text())['origin_proof_errors']==result['origin_proof_errors']
        record['approved'] = False
        record['digest'] = procedure().record_digest(record)
        args['output'] = tmp_path/'unapproved.json'
        with pytest.raises(ValueError,match='approved_optional_store_record_required'):
            build_cutover_inputs(**args)
        assert not args['output'].exists()
    else:
        with pytest.raises(ValueError,match=code) as failure:
            build_cutover_inputs(**args)
        assert failure.value.store_counts=={'journal':{code:len(rows)}}
        assert not args['output'].exists()


def test_real_conversion_inventory_collects_all_native_stores(acquired,tmp_path):
    from yeoman_gateway.history.convert.run import run_conversion
    from yeoman_gateway.processing.models import TextPayload
    from yeoman_shared.utils.helpers import get_operational_store_path

    from scripts.prepare_history_cutover import prepare
    home,_,_,_,knowledge,raw = acquired
    phone,ts = '10001@s.whatsapp.net',1_800_000_000_000
    inbound = home/'data/inbound'
    inbound.mkdir(parents=True)
    members = []
    for filename,store in (('reply_context.db','reply_context'),('archive.db','inbound_archive')):
        path = inbound/filename
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE inbound_messages (channel TEXT,chat_id TEXT,message_id TEXT,sender_id TEXT,text TEXT,timestamp INTEGER,opaque BLOB)')
            db.execute('INSERT INTO inbound_messages VALUES (?,?,?,?,?,?,?)',('whatsapp',phone,store,phone,'Synthetic '+store,ts,b'\x00\xff'))
        members.append(dict(path=path.relative_to(home).as_posix(),kind='sqlite',exists=True,restore=True))
    session = inbound/('whatsapp_'+phone+'.jsonl')
    session.write_text(dumps(dict(role='user',message_id='session_jsonl',sender_id=phone,content='Synthetic session_jsonl',timestamp=ts))+'\n')
    members.append(dict(path=session.relative_to(home).as_posix(),kind='file',exists=True,restore=True))
    folder = get_operational_store_path('bridge_references',data_dir=home/'data')
    folder.mkdir(parents=True)
    reference = folder/'reference.json'
    reference.write_text(dumps(dict(chatJid=phone,encoded='synthetic-encoded',storedAtMs=ts)))
    members.append(dict(path=folder.relative_to(home).as_posix(),kind='tree',exists=True,restore=True))
    memory = home/'data/memory/memory.db'
    memory.parent.mkdir(parents=True)
    with sqlite3.connect(memory) as db:
        db.execute('CREATE TABLE memory2_nodes (id TEXT,kind TEXT,channel TEXT,chat_id TEXT,sender_id TEXT,source_message_id TEXT,content TEXT,created_at INTEGER,source TEXT,source_role TEXT,meta_json TEXT,is_deleted INTEGER,opaque BLOB)')
        db.execute('INSERT INTO memory2_nodes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',('node','utterance','whatsapp',phone,phone,'memory','Synthetic memory',ts,'native','user','{}',0,b'\x00\xff'))
    members.append(dict(path=memory.relative_to(home).as_posix(),kind='sqlite',exists=True,restore=True))
    processing = home/'data/ops/processing.db'
    p = ProcessingStore(processing)
    p.enqueue_effect(effect_id='synthetic-effect',operation_key='synthetic-operation',payload=TextPayload('Synthetic effect'),now_ms=ts)
    p.close()
    stores = ('reply_context','inbound_archive','session_jsonl','bridge_refs','memory')
    with sqlite3.connect(processing) as db:
        db.row_factory = sqlite3.Row
        authority = dict(db.execute("SELECT * FROM event_source_authority WHERE event_id='mapped'").fetchone())
        for name in stores:
            row = dict(authority,event_id=name)
            db.execute('INSERT INTO event_source_authority ('+','.join(row)+') VALUES ('+','.join('?' for _ in row)+')',tuple(row.values()))
        db.commit()
        db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    with sqlite3.connect(knowledge) as db:
        db.row_factory = sqlite3.Row
        template = dict(db.execute('SELECT * FROM knowledge_jobs LIMIT 1').fetchone())
        for name in stores:
            row = dict(template,job_id='done-'+name,state='done',sources_json=dumps([dict(event_id=name,revision=1,channel='whatsapp')]))
            db.execute('INSERT INTO knowledge_jobs ('+','.join(row)+') VALUES ('+','.join('?' for _ in row)+')',tuple(row.values()))
        db.commit()
        db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    with (raw/'whatsapp/messages.jsonl').open('a') as out:
        for name in stores[:-1]:
            out.write(dumps(dict(account='synthetic',archive_version=1,channel='whatsapp',chat_id=phone,
                direction='in',kind='message',received_ms=100,correlation_id='',media=None,native=dict(type='message',payload=dict(
                    chatJid=phone,senderId=phone,timestamp=ts,messageId=name,text='Synthetic '+name))))+'\n')
    receipt = json.loads((home/'manifest.json').read_text())
    receipt['members'].extend(members)
    for member in receipt['members']:
        path = home/member['path']
        if member['kind']=='tree':
            member['files'] = {p.relative_to(path).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in path.rglob('*') if p.is_file()}
            member['sha256'] = procedure().record_digest(member['files'])
        else:
            member['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    receipt['digest'] = procedure().record_digest(receipt)
    (home/'manifest.json').write_text(dumps(receipt))
    staged = tmp_path/'real-conversion'
    decoded = dict(key=dict(id='bridge_refs',remoteJid=phone,participant=phone),messageTimestamp=ts//1000,
                   message=dict(conversation='Synthetic bridge_refs'))
    run_conversion(home,staged,decode=lambda batch:{name:{'value':decoded} for name,_ in batch})
    package = prepare_import_manifest(staged)
    manifest = tmp_path/'real-conversion.json'
    manifest.write_text(dumps(package))
    assert all(entry['path'].startswith('sources/') for entry in package['source_inventory'].values())
    assert not (home/'sources').exists()
    revised = (home,manifest,staged,acquired[3],knowledge,raw)
    build(revised,home/'cutover-inputs.json')
    bundle = json.loads((home/'cutover-inputs.json').read_text())
    assert {'journal',*stores}<={r['origin']['store'] for r in bundle['preserved_rows']}
    for row in bundle['preserved_rows']:
        file,number = row['source_ref'].split('#')
        assert row['origin']==package['files'][file]['rows'][int(number)-1]['origin']
        assert row['origin']['row_sha256']==row_sha256(row['preserved_original'])
    history = tmp_path/'history.db'
    project([raw],history)
    policy = tmp_path/'policy-copy.json'
    policy.write_text(dumps({'defaults':{'whoCanTalk':{'mode':'everyone'}}}))
    result = prepare(argparse.Namespace(snapshot_home=home,history_db=history,knowledge_source=knowledge,
        knowledge_target=tmp_path/'v3.db',policy_snapshot=policy,output_root=tmp_path/'aliases'))
    entries = json.loads((tmp_path/'aliases/legacy-alias-manifest.json').read_text())['entries']
    for name in stores[:-1]:
        entry = next(e for e in entries if e['issued']['event_id']==name)
        assert entry['status']=='mapped' and entry['alias']['message_id']=='whatsapp:'+phone+':'+name
        assert {proof['origin']['store'] for proof in entry['proofs']}=={name}
    assert result['unmapped_terminal_job_refs']==1  # Memory capture time does not prove issuance.


def test_host_input_errors_have_private_counts_and_cli_has_no_content(acquired,tmp_path,monkeypatch,capsys):
    from scripts import history_cutover_inputs as inputs
    from scripts.history_cutover_host import _prepare_inputs
    home,manifest,staged,evidence,_,raw = acquired
    acquired_evidence = home/'forward.json'
    acquired_evidence.write_bytes(evidence.read_bytes())
    receipts = tmp_path/'receipts'
    receipts.mkdir()
    record = dict(output=str(home),receipts=str(receipts),inventory={'optional_conversion_stores':['journal']},layout=dict(
        preparation_home=str(tmp_path/'prepared'),raw=str(raw),conversion_manifest=str(manifest),staged=str(staged)))
    error = inputs.InputProofError('conversion_original_not_acquired',{'journal':{'conversion_original_not_acquired':2}})
    def refuse(**kwargs):
        assert kwargs['record']==record
        raise error
    monkeypatch.setattr(inputs,'build_cutover_inputs',refuse)
    with pytest.raises(inputs.InputProofError):
        _prepare_inputs({'record':record},dict(forward_start_evidence_member='forward.json'))
    proof = receipts/'prepare-input-bundle-errors.json'
    assert proof.stat().st_mode & 0o777 == 0o600
    assert json.loads(proof.read_text())==dict(error_code=str(error),origin_proof_errors=error.store_counts)
    record_file = tmp_path/'record.json'
    record_file.write_text(dumps(record))
    argv = ['inputs']
    for name,path in (('acquisition-home',home),('conversion-manifest',manifest),('staged-raw',staged),
                      ('forward-start-evidence',evidence),('output',tmp_path/'cli-inputs.json'),('record',record_file)):
        argv.extend(['--'+name,str(path)])
    assert inputs.main(argv)==1
    assert json.loads(capsys.readouterr().out)==dict(ok=False,error=str(error),origin_proof_errors=error.store_counts)


@pytest.mark.parametrize('shared', [False, True])
def test_legacy_node_survives_prepare_reopen_and_acl_revalidation(acquired, tmp_path, monkeypatch, shared):
    from yeoman_gateway.knowledge._history_sources import HistoryKnowledgeSources
    from yeoman_gateway.knowledge._memory.read_gate import FactReadGate
    from yeoman_gateway.knowledge._memory.shared_facts import FactReadContext
    from yeoman_gateway.knowledge.api import open_knowledge_store
    from yeoman_gateway.knowledge.runtime import RuntimeKnowledgePolicy, RuntimeKnowledgeSources

    from scripts.prepare_history_cutover import prepare
    home, _, _, _, knowledge, raw = acquired
    note_id = 'legacy-node:synthetic'
    chat = 'synthetic-node@g.us'
    members = {'whatsapp:10001', 'whatsapp:10002'}
    audience = dumps(sorted(members)) if shared else None
    with sqlite3.connect(knowledge) as db:
        db.execute("UPDATE knowledge_statement_sources SET event_id=?,chat_id=?,source_audience_json=?,status='unknown'", (note_id, chat, audience))
        db.execute("UPDATE memory2_facts SET chat_scope_key=?,visibility_scope=?,group_rule=?",
            ('channel:whatsapp:chat:'+chat, 'chat_shared' if shared else 'author_only', 'chat_members_at_source' if shared else 'author_only'))
        if shared:
            for (fact_id,) in db.execute('SELECT fact_id FROM memory2_facts').fetchall():
                for principal in members:
                    db.execute("INSERT OR IGNORE INTO memory2_fact_principals VALUES (?,?,'audience')", (fact_id, principal))
        db.execute('UPDATE memory2_fact_sources SET source_event_id=?,source_chat_id=?', (note_id, chat))
        for job_id, refs in db.execute('SELECT job_id,sources_json FROM knowledge_jobs').fetchall():
            refs = json.loads(refs)
            for ref in refs:
                ref.update(event_id=note_id, chat_id=chat)
            db.execute('UPDATE knowledge_jobs SET sources_json=? WHERE job_id=?', (dumps(refs), job_id))
        db.commit()
        db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    baseline = open_knowledge_store(knowledge, workspace_id='synthetic', source_authority=RuntimeKnowledgeSources(),
        policy_authority=RuntimeKnowledgePolicy(engine=None))
    try:
        memory = baseline.memory_store()
        sid = baseline._store.query_one("SELECT statement_id FROM knowledge_statement_sources WHERE event_id=?", (note_id,))['statement_id']
        fact = memory.get_fact(sid)
        context = FactReadContext(fact.author_principal, fact.chat_scope_key, frozenset({fact.author_principal}), now_ms=fact.valid_from_ms+1)
        assert FactReadGate(memory).recheck((sid,), context)==frozenset({sid})
        from yeoman_gateway.knowledge.models import SourceRef
        original = dict(baseline._store.query_one('SELECT * FROM knowledge_statement_sources WHERE statement_id=?', (sid,)))
        expected_source = SourceRef(**{k: original[k] for k in SourceRef.__dataclass_fields__})
        assert baseline._statements.sources_of(sid)==((expected_source, 'unknown'),)
    finally:
        baseline.close()
    with closing(sqlite3.connect(knowledge)) as db:
        assert db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0]==0
    manifest_path = home/'manifest.json'
    receipt = json.loads(manifest_path.read_text())
    next(m for m in receipt['members'] if m['path']=='data/knowledge/knowledge.db')['sha256'] = hashlib.sha256(knowledge.read_bytes()).hexdigest()
    receipt['digest'] = procedure().record_digest(receipt)
    manifest_path.write_text(dumps(receipt))
    build(acquired, home/'cutover-inputs.json')
    history = tmp_path/'history.db'
    project([raw], history)
    policy = tmp_path/'policy-copy.json'
    policy.write_text(dumps({'defaults': {'whoCanTalk': {'mode': 'everyone'}}}))
    target = tmp_path/'v3.db'
    output = tmp_path/'aliases'
    result = prepare(argparse.Namespace(snapshot_home=home, history_db=history, knowledge_source=knowledge,
        knowledge_target=target, policy_snapshot=policy, output_root=output))
    assert result['legacy_node']==1 and result['withheld_statements']==0 and result['affected_jobs']==0
    assert result['cited_reason_counts']=={'legacy_node': 1}
    manifest = json.loads((output/'legacy-alias-manifest.json').read_text())
    assert manifest['capture_summary']['no_legacy_row_pending']==0
    with closing(sqlite3.connect(target)) as db, closing(sqlite3.connect(knowledge)) as original:
        assert db.execute('SELECT * FROM knowledge_jobs').fetchall()==original.execute('SELECT * FROM knowledge_jobs').fetchall()
        assert not db.execute('SELECT 1 FROM knowledge_history_source_aliases WHERE event_id=?', (note_id,)).fetchall()
        assert not db.execute('SELECT 1 FROM knowledge_history_capture WHERE message_id=?', (note_id,)).fetchall()
    # Fresh runtime authority has no processing-store record for this legacy note.
    service = open_knowledge_store(target, workspace_id='synthetic', source_authority=RuntimeKnowledgeSources(),
        policy_authority=RuntimeKnowledgePolicy(engine=None), history_mode=True)
    reader = HistoryReader(history)
    with closing(sqlite3.connect(history)) as db:
        runtime = json.loads(db.execute("SELECT state_json FROM projector_state WHERE file='@runtime'").fetchone()[0])
    snapshot = reader.open_snapshot(HistoryBoundary(runtime["generation"], tuple(enumerate_committed(raw))))
    try:
        q = HistoryQueries(snapshot)
        authority = HistoryKnowledgeSources(q, {}, service.history_source_ledger, service._legacy_authority)
        monkeypatch.setattr(q, '_rows', lambda *a: pytest.fail('legacy-node revalidation consulted history'))
        memory = service.memory_store()
        sid = service._store.query_one("SELECT statement_id FROM knowledge_statement_sources WHERE event_id=?", (note_id,))['statement_id']
        fact = memory.get_fact(sid)
        context = FactReadContext(fact.author_principal, fact.chat_scope_key, frozenset({fact.author_principal}), now_ms=fact.valid_from_ms+1)
        with service.history_scope(q, authority):
            monkeypatch.setattr(snapshot, 'assert_current', lambda *a: pytest.fail('legacy-node revalidation consulted history'))
            gate = FactReadGate(memory)
            assert gate.recheck((sid,), context)==frozenset({sid})
            from dataclasses import replace
            assert gate.recheck((sid,), replace(context, principal_id='whatsapp:outsider'))==frozenset()
            member_context = replace(context, principal_id='whatsapp:10002', current_members=frozenset(members))
            assert gate.recheck((sid,), member_context)==(frozenset({sid}) if shared else frozenset())
            later = replace(context, principal_id='whatsapp:10003', current_members=frozenset(members|{'whatsapp:10003'}))
            assert gate.recheck((sid,), later)==frozenset()
            assert not authority.permits_principal(expected_source, later.principal_id, now_ms=later.now_ms)
            source = service._statements.sources_of(sid)[0][0]
            assert source==expected_source and source.chat_id==chat
            authority.mark_source_revoked(source)
            assert gate.recheck((sid,), context)==frozenset()
    finally:
        snapshot.close()
        reader.close()
        service.close()


def test_supplemental_capture_time_is_not_native_send_time():
    from scripts.history_cutover_inputs import _original_state
    record = dict(kind='message', channel='whatsapp', chat_id='synthetic', occurred_ms=200,
        time_certainty='capture_time_approx', origin={'table':'memory2_nodes'},
        payload={'messageId':'native'}, original={'content':'Synthetic preserved text'})
    state = _original_state(record, set())
    assert state==dict(channel='whatsapp', chat_id='synthetic', native_message_id='native', text='Synthetic preserved text')


@pytest.mark.parametrize('mode',['rehearsal','live'])
def test_record_derives_control_paths_hashes_and_config_socket(tmp_path,mode):
    from scripts.history_cutover_inputs import build_cutover_record
    _,home,value = record(tmp_path)
    inv = value['inventory']
    for key in ('config_path','pause_path','knowledge_db','processing_db','gateway_socket',
            'frozen_watermarks','prepared_text_manifest_sha256'):
        inv.pop(key)
    (home/'config.json').write_text(dumps({'ipc':{'gatewaySocketPath':str(home/'run/custom.sock')},'models':{'profiles':{'syntheticFast':{'kind':'chat','model':'synthetic'}},'routes':{'assistant.reply':'syntheticFast'}}}))
    inv['frozen_files'] = [str(home/'cron.json')]
    source = {k:value[k] for k in ('home','output','receipts','candidate','prior','inventory','rehearsal_root')}
    source.update(version=1,python=__import__('sys').executable)
    path = tmp_path/'inventory.json'
    path.write_text(dumps(source))
    result = build_cutover_record(inventory=path,layout={},mode=mode,
        window=(1791532800000,1791534600000),expected_gateway_jobs=0)['inventory']
    assert result['config_path']==str(home/'config.json')
    assert result['knowledge_db']==str(home/'data/knowledge/knowledge.db')
    assert result['processing_db']==str(home/'data/ops/processing.db')
    assert result['pause_path']==str(home/'data/ops/response-pauses.json')
    assert result['frozen_watermarks']=={str(home/'cron.json'):__import__('hashlib').sha256((home/'cron.json').read_bytes()).hexdigest()}
    assert result['prepared_text_manifest_sha256']==__import__('hashlib').sha256(Path(inv['prepared_text_manifest']).read_bytes()).hexdigest()
    if mode=='live':
        assert result['gateway_socket']==str(home/'run/custom.sock')
    del source['inventory']['prepared_text_manifest']
    path.write_text(dumps(source))
    with pytest.raises(ValueError,match='missing_inventory_key:prepared_text_manifest'):
        build_cutover_record(inventory=path,layout={},mode=mode,
            window=(1791532800000,1791534600000),expected_gateway_jobs=0)


@pytest.mark.parametrize('mode',['rehearsal','live'])
def test_record_cli_reports_only_safe_missing_control_key(tmp_path,mode,capsys):
    from scripts.history_cutover_inputs import main
    _,_,value = record(tmp_path)
    source = {k:value[k] for k in ('home','output','receipts','candidate','prior','inventory','rehearsal_root')}
    source.update(version=1,python=__import__('sys').executable)
    del source['inventory']['prepared_text_manifest']
    inventory,layout,output = tmp_path/'inventory.json',tmp_path/'layout.json',tmp_path/'generated.json'
    inventory.write_text(dumps(source))
    layout.write_text('{}')
    assert main(['record','--inventory',str(inventory),'--layout',str(layout),'--mode',mode,
        '--window-start-ms','1791532800000','--window-end-ms','1791534600000',
        '--expected-gateway-jobs','0','--output',str(output)])==1
    assert json.loads(capsys.readouterr().out)==dict(ok=False,error='missing_inventory_key:prepared_text_manifest')
    assert not output.exists() and not Path(value['receipts']).exists()
