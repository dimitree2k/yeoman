"""Synthetic real-scale input-builder budget; run serially with --run-perf."""
# ruff: noqa: F811
import cProfile
import io
import json
import pstats
import sqlite3
import time

import pytest
from yeoman_gateway.history.convert.journal import _event
from yeoman_gateway.history.convert.run import prepare_import_manifest
from yeoman_shared.raw_archive.records import dumps

from tests.gateway.test_history_cutover_inputs import acquired, build  # noqa: F401


def replace_rows(db,table,rows):
    db.execute('DELETE FROM '+table)
    keys = list(rows[0])
    db.executemany('INSERT INTO '+table+' ('+','.join(keys)+') VALUES ('+','.join('?' for _ in keys)+')',
        [tuple(row[k] for k in keys) for row in rows])


@pytest.mark.perf
def test_real_scale_input_builder_under_sixty_seconds(acquired,tmp_path):
    from scripts.history_cutover import _hash, record_digest
    home,manifest,staged,evidence,knowledge,raw = acquired
    processing = home/'data/ops/processing.db'
    with sqlite3.connect(processing) as db:
        db.row_factory = sqlite3.Row
        event = dict(db.execute("SELECT * FROM events WHERE event_id='mapped'").fetchone())
        authority = dict(db.execute("SELECT * FROM event_source_authority WHERE event_id='mapped'").fetchone())
        events = [dict(event,event_id='scale-'+str(i),event_key='scale-'+str(i),trace_id='scale-'+str(i),
            source_message_id='scale-'+str(i),channel='whatsapp' if i<7184 else 'telegram',
            payload_json=dumps(dict(provider_message_id='scale-'+str(i),text='Synthetic scale',media=None,reply_to_message_id=None,mentions=None))) for i in range(26028)]
        replace_rows(db,'events',events)
        replace_rows(db,'event_source_authority',[dict(authority,event_id=e['event_id'],source_channel=e['channel']) for e in events[:24099]])
        db.commit()
        db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    with sqlite3.connect(knowledge) as db:
        db.row_factory = sqlite3.Row
        link = dict(db.execute('SELECT * FROM knowledge_statement_sources LIMIT 1').fetchone())
        job = dict(db.execute('SELECT * FROM knowledge_jobs LIMIT 1').fetchone())
        binding = dict(db.execute('SELECT * FROM knowledge_identifier_bindings LIMIT 1').fetchone())
        replace_rows(db,'knowledge_statement_sources',[dict(link,event_id=e['event_id'],channel=e['channel']) for e in events[:24099]])
        replace_rows(db,'knowledge_jobs',[dict(job,job_id='job-'+str(i),state='done',sources_json=dumps([dict(event_id=events[i]['event_id'],revision=1)])) for i in range(128)])
        replace_rows(db,'knowledge_identifier_bindings',[dict(binding,binding_id='binding-'+str(i),value=str(20000+i)+'@s.whatsapp.net') for i in range(512)])
        db.commit()
        db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    # Count actual native messages independently from the additional archive lines.
    lines = [dumps(dict(archive_version=1,account='synthetic',channel='whatsapp',chat_id='synthetic@g.us',
        direction='in',kind='message',received_ms=100,correlation_id='',media=None,
        native=dict(type='message',payload=dict(chatJid='synthetic@g.us',senderId='10001@s.whatsapp.net',
            timestamp=100,messageId=e['event_id'],text='Synthetic scale')))) for e in events]
    lines.extend([dumps(dict(kind='synthetic-receipt',received_ms=100))]*(85000-len(lines)))
    (raw/'whatsapp/messages.jsonl').write_text('\n'.join(lines)+'\n')
    records = [_event(e) for e in events[:18983]]
    records.append(records[0])
    (staged/'backfill/journal.jsonl').write_text('\n'.join(map(dumps,records))+'\n')
    manifest.write_text(dumps(prepare_import_manifest(staged)))
    receipt = json.loads((home/'manifest.json').read_text())
    for member in receipt['members']:
        path = home/member['path']
        if member['kind']=='tree':
            member['files'] = {p.relative_to(path).as_posix():_hash(p) for p in path.rglob('*') if p.is_file()}
            member['sha256'] = record_digest(member['files'])
        else:
            member['sha256'] = _hash(path)
    receipt['digest'] = record_digest(receipt)
    (home/'manifest.json').write_text(dumps(receipt))
    profile = cProfile.Profile()
    started = time.monotonic()
    try:
        summary = profile.runcall(build,acquired,tmp_path/'scale-inputs.json')
    finally:
        elapsed = time.monotonic()-started
        profile.dump_stats(str(tmp_path/'builder.prof'))
        report = io.StringIO()
        pstats.Stats(profile,stream=report).sort_stats('cumulative').print_stats(25)
        print(report.getvalue())
        print(f'builder_seconds={elapsed:.3f}; legacy=24099 preserved=18984 capture=7184 raw_lines=85002 messages=26028')
    assert summary==dict(legacy_rows=24099,preserved_rows=18984,capture_rows=7184)
    assert elapsed <= 60
    # Profile the original quadratic join on a bounded sample of the full stores.
    from scripts import history_cutover_inputs as module
    from tests.gateway.test_history_cutover_inputs import original_enrichment
    bundle = json.loads((tmp_path/'scale-inputs.json').read_bytes())
    sample = {(r['event_id'],r['revision']):dict(r) for r in bundle['legacy_rows'][:256]}
    statements = {r['statement_id']:r for r in module._rows(knowledge,'knowledge_statements')}
    links = module._rows(knowledge,'knowledge_statement_sources')
    bindings = module._rows(knowledge,'knowledge_identifier_bindings')
    jobs = [(j,json.loads(j['sources_json'])) for j in module._rows(knowledge,'knowledge_jobs')]
    event_by_id = {e['event_id']:e for e in module._rows(processing,'events')}
    baseline = cProfile.Profile()
    before = time.monotonic()
    baseline.runcall(original_enrichment,sample,statements,links,bindings,jobs,event_by_id,[20,'cursor'],[10,''])
    sample_seconds = time.monotonic()-before
    baseline.dump_stats(str(tmp_path/'original-join-sample.prof'))
    report = io.StringIO()
    pstats.Stats(baseline,stream=report).sort_stats('cumulative').print_stats(15)
    print(report.getvalue())
    print(f'original_join_sample_keys=256; full_links=24099; sample_seconds={sample_seconds:.3f}')
