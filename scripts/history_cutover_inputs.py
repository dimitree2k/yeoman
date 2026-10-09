#!/usr/bin/env python3
"""Build private cutover inputs from acquired originals and a reviewed inventory."""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from pathlib import Path
from typing import Literal

from jsonschema import Draft202012Validator
from yeoman_gateway.history.convert.journal import _event
from yeoman_gateway.history.layer1 import row_sha256
from yeoman_gateway.knowledge.models import SourceRef
from yeoman_shared.raw_archive.records import validate_import_manifest

try:
    from scripts.history_cutover import (
        _bridge_package,
        _interpreter,
        _member,
        _paths,
        _private_json,
        _rehearsal_paths,
        check_window_timing,
        record_digest,
    )
    from scripts.history_maintenance_guard import preflight_isolated_paths
except ModuleNotFoundError:
    from history_cutover import (
        _bridge_package,
        _interpreter,
        _member,
        _paths,
        _private_json,
        _rehearsal_paths,
        check_window_timing,
        record_digest,
    )
    from history_maintenance_guard import preflight_isolated_paths


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rows(path: Path, table: str) -> list[dict]:
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro&immutable=1', uri=True)) as db:
        db.row_factory = sqlite3.Row
        return [dict(r) for r in db.execute('SELECT * FROM "'+table.replace('"','""')+'"')]


def _acquisition(home: Path) -> dict:
    receipt = json.loads((home/'manifest.json').read_bytes())
    if receipt.get('version') != 1 or receipt.get('ok') is not True or receipt.get('digest') != record_digest(receipt):
        raise ValueError('acquisition_receipt_mismatch')
    for member in receipt['members']:
        path = _member(home, member)
        preflight_isolated_paths(path)
        if not member['exists']:
            continue
        if member['kind'] == 'tree':
            files = {p.relative_to(path).as_posix(): p for p in path.rglob('*') if p.is_file()}
            preflight_isolated_paths(*files.values()) if files else None
            observed = {name: _hash(p) for name,p in files.items()}
            valid = observed == member['files'] and record_digest(observed) == member['sha256']
        else:
            if member['kind']=='sqlite' and Path(str(path)+'-wal').exists() and Path(str(path)+'-wal').stat().st_size:
                raise ValueError('acquisition_not_checkpointed')
            valid = _hash(path) == member['sha256']
        if not valid:
            raise ValueError('acquisition_member_changed')
    return receipt


def _issued(row: Mapping) -> dict:
    return {k: row[k] for k in SourceRef.__dataclass_fields__}


def _authority(row: Mapping) -> dict:
    return dict(event_id=row['event_id'],revision=row['revision'],channel=row['source_channel'],
                chat_id=row['source_chat_id'],author_principal=row['author_principal'],
                occurred_at_ms=row['occurred_at_ms'])


def _original_state(record: dict, mutations: set[tuple]) -> dict:
    """Only journal message originals with explicit payload evidence can prove state."""
    original = record['original']
    if (record['origin']['table'] != 'events' or original.get('kind') != 'message'
            or isinstance(record.get('payload',{}).get('segments'),list)):
        return {}
    converted = _event(original)
    payload = converted['payload']
    raw_payload = json.loads(original['payload_json']) if original.get('payload_json') else {}
    state = dict(channel=converted['channel'],chat_id=converted['chat_id'],
        native_message_id=payload.get('messageId'),direction=converted['direction'],
        sent_ms=converted['occurred_ms'],time_certainty=converted['time_certainty'],
        provenance=converted['provenance'],deleted=bool(original['payload_purged_ms']))
    if 'text' in raw_payload:
        state.update(text=payload.get('text'),current_text=payload.get('text'))
    if 'media' in raw_payload and not payload.get('generatedDescription'):
        state['media_json'] = raw_payload['media'] or None
    if 'reply_to_message_id' in raw_payload or 'reply_to' in raw_payload:
        state['reply_to_native_id'] = payload.get('replyToMessageId')
    if 'mentions' in raw_payload:
        state['mentions_json'] = raw_payload['mentions'] or None
    # An acquired complete journal proves absence; edit/delete event identities
    # require separate evidence, so they remain withheld rather than reconstructed.
    if not any((original['channel'],original['chat_id'],target) in mutations
               for target in (original['event_id'],state['native_message_id'])):
        state['events'] = []
    return state


def build_cutover_inputs(*, acquisition_home: Path, conversion_manifest: Path,
    staged_raw: Path, forward_start_evidence: Path, output: Path) -> dict:
    preflight_isolated_paths(acquisition_home,conversion_manifest,staged_raw,forward_start_evidence,output)
    for root in (acquisition_home,staged_raw):
        for path in root.rglob('*'):
            preflight_isolated_paths(path)
    receipt = _acquisition(acquisition_home)
    knowledge = acquisition_home/'data/knowledge/knowledge.db'
    processing = acquisition_home/'data/ops/processing.db'
    members = {m['path']:m for m in receipt['members'] if m['exists']}
    if any(str(p.relative_to(acquisition_home)) not in members for p in (knowledge,processing)):
        raise ValueError('required_acquisition_members_missing')
    manifest = json.loads(conversion_manifest.read_bytes())
    blobs = validate_import_manifest(staged_raw,manifest)
    evidence = json.loads(forward_start_evidence.read_bytes())
    if (set(evidence) != {'forward_start_ms','legacy_reinit_ms','log_sha256','source'}
        or type(evidence['forward_start_ms']) is not int or type(evidence['legacy_reinit_ms']) is not int
        or not 0 <= evidence['forward_start_ms'] <= evidence['legacy_reinit_ms']
        or not isinstance(evidence['source'],str) or not evidence['source']
        or not isinstance(evidence['log_sha256'],str) or len(evidence['log_sha256']) != 64
        or any(c not in '0123456789abcdef' for c in evidence['log_sha256'])):
        raise ValueError('invalid_forward_start_evidence')
    meta = {r['key']:r['value'] for r in _rows(knowledge,'knowledge_meta')}
    if meta.get('schema_version') != '2':
        raise ValueError('legacy_knowledge_schema_required')
    boundary = [int(meta['statement_capture_boundary_ms']),meta['statement_capture_boundary_event_id']]
    start = [evidence['forward_start_ms'],'']
    if tuple(start) > tuple(boundary):
        raise ValueError('forward_start_after_capture_boundary')
    events = _rows(processing,'events')
    event_by_id = {r['event_id']:r for r in events}
    authorities = {(r['event_id'],r['revision']):r for r in _rows(processing,'event_source_authority')}
    statements = {r['statement_id']:r for r in _rows(knowledge,'knowledge_statements')}
    links = _rows(knowledge,'knowledge_statement_sources')
    jobs = _rows(knowledge,'knowledge_jobs')
    bindings = _rows(knowledge,'knowledge_identifier_bindings')
    legacy = {}
    for authority in authorities.values():
        issued = _authority(authority)
        row = dict(issued,status='revoked' if authority['revoked_at_ms'] is not None else 'active')
        if authority['audience_status'] == 'author_only':
            row['source_audience_json'] = None
        elif authority['audience_status'] == 'known':
            row['source_audience_json'] = authority['audience_members_json']
        legacy[(row['event_id'],row['revision'])] = row
    for link in links:
        key = link['event_id'],link['revision']
        if key in legacy and _issued(legacy[key]) != _issued(link):
            raise ValueError('conflicting_preserved_source_authority')
        row = legacy.setdefault(key,dict(_issued(link)))
        link_audience = link['source_audience_json']
        # v2 stored only members, so author_only serialized as []; its status
        # must come from the durable authority, never from projected membership.
        if (key in authorities and authorities[key]['audience_status']=='author_only'
                and link_audience is not None and json.loads(link_audience)==[]):
            link_audience = None
        # The original persisted source audience is the issuance ceiling.
        if 'source_audience_json' in row and row['source_audience_json'] != link_audience:
            row.pop('source_audience_json')
            row['audience_conflict'] = True
        elif not row.get('audience_conflict'):
            row['source_audience_json'] = link_audience
        row['status'] = 'revoked' if row.get('status')=='revoked' or link['status']=='revoked' else link['status']
    for job in jobs:
        for source in json.loads(job['sources_json']):
            key = source['event_id'],source['revision']
            if all(field in source for field in SourceRef.__dataclass_fields__):
                if key in legacy and _issued(legacy[key]) != _issued(source):
                    raise ValueError('conflicting_preserved_job_source')
                legacy.setdefault(key,dict(_issued(source),status='unknown'))
            elif key not in legacy:
                raise ValueError('unproven_legacy_job_source')
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
        if any(j['state']=='done' and any((s['event_id'],s['revision'])==key for s in
               json.loads(j['sources_json'])) for j in jobs):
            row['completed'] = True
    preserved = []
    by_event = {}
    for key in sorted(legacy):
        by_event.setdefault(key[0],[]).append(key)
    originals_cache = {}
    authority_cache = {str(processing):authorities}
    events_cache = {str(processing):{(e['channel'],e['chat_id'],e['target_message_id'])
        for e in events if e['kind'] in ('edit','delete')}}
    for relative,blob in blobs.items():
        for number,line in enumerate(blob.splitlines(),1):
            record = json.loads(line)
            original,origin = record.get('original'),record.get('origin')
            if not isinstance(original,dict) or not origin:
                continue
            if original.get('event_id') not in by_event:
                continue
            # Never resolve the manifest's logical source path against the host.
            source = _member(acquisition_home,dict(path=origin['path'],kind='sqlite',restore=False))
            if origin['path'] not in members or members[origin['path']]['kind'] != 'sqlite':
                raise ValueError('conversion_origin_not_acquired')
            cache_key = str(source),origin['table']
            if cache_key not in originals_cache:
                originals_cache[cache_key] = {row_sha256(r) for r in _rows(source,origin['table'])}
            if origin['row_sha256'] not in originals_cache[cache_key]:
                raise ValueError('conversion_original_not_acquired')
            if str(source) not in events_cache:
                events_cache[str(source)] = {(e['channel'],e['chat_id'],e['target_message_id'])
                    for e in _rows(source,'events') if e['kind'] in ('edit','delete')}
                authority_cache[str(source)] = {(r['event_id'],r['revision']):r
                    for r in _rows(source,'event_source_authority')}
            for key in by_event[original['event_id']]:
                envelope = dict(state=_original_state(record,events_cache[str(source)]))
                authority = authority_cache[str(source)].get(key)
                if authority and original.get('principal')==authority['author_principal']:
                    envelope['issued'] = _authority(authority)
                if type(original.get('created_ms')) is int:
                    envelope['created_ms'] = original['created_ms']
                ref = f'{relative}#{number}'
                segments = record.get('payload',{}).get('segments')
                refs = [f'{ref}/{i}' for i in range(len(segments))] if isinstance(segments,list) else [ref]
                for source_ref in refs:
                    if source_ref not in manifest['ref_map']:
                        raise ValueError('conversion_segment_ref_missing')
                    preserved.append(dict(event_id=key[0],revision=key[1],original=envelope,
                        preserved_original=original,envelope_sha256=row_sha256(envelope),
                        origin=origin,source_ref=source_ref))
    capture = []
    for event in events:
        if event['kind']=='message' and event['channel']=='whatsapp':
            payload = json.loads(event['payload_json']) if event['payload_json'] else {}
            native = payload.get('provider_message_id') or payload.get('message_id') or event['source_message_id']
            if native:
                capture.append(dict(message_id=f"whatsapp:{event['chat_id']}:{native}",
                    created_ms=event['created_ms'],event_id=event['event_id'],boundary=boundary,forward_start=start))
    bundle = dict(version=1,snapshot_digest=receipt['digest'],conversion_digest=manifest['package_digest'],
                  forward_start_evidence=evidence,legacy_rows=[legacy[k] for k in sorted(legacy)],
                  preserved_rows=preserved,capture_rows=capture)
    _private_json(output,bundle)
    return {k:len(bundle[k]) for k in ('legacy_rows','preserved_rows','capture_rows')}


def build_cutover_record(*, inventory: Path, layout: Mapping[str,str],
    mode: Literal['rehearsal','live'], window: tuple[int,int],expected_gateway_jobs: int) -> dict:
    preflight_isolated_paths(inventory)
    source = json.loads(inventory.read_bytes())
    schema = json.loads(Path(__file__).with_name('history_cutover_inventory.schema.json').read_bytes())
    if not Draft202012Validator(schema).is_valid(source):
        raise ValueError('invalid_inventory_schema')
    if source.get('version') != 1 or mode not in ('live','rehearsal'):
        raise ValueError('invalid_inventory')
    inv = source['inventory']
    if type(expected_gateway_jobs) is not int or expected_gateway_jobs < 0 or inv['gateway_jobs'] != expected_gateway_jobs:
        raise ValueError('cron_inventory_drift')
    if any(not isinstance(u,dict) or not all(k in u for k in ('name','restart','executable')) for u in inv['units']):
        raise ValueError('structured_unit_inventory_required')
    inv['expected_gateway_jobs'] = expected_gateway_jobs
    inv['host_crontab'].update(window_safe=True,window_start_ms=window[0],window_end_ms=window[1])
    check_window_timing(inv)
    _paths(*(Path(v) for v in (source['home'],source['output'],source['receipts'],*layout.values())))
    _interpreter(Path(source['python']))
    _bridge_package(Path(inv['bridge_package_dir']))
    if mode=='rehearsal':
        if not source.get('rehearsal_root'):
            raise ValueError('rehearsal_root_required')
        _rehearsal_paths(Path(source['rehearsal_root']), Path(source['home']), *(Path(v) for v in (source['output'],source['receipts'],*layout.values())))
    record = dict(version=1,mode=mode,approved=False,approval=None,
        **{k:source[k] for k in ('home','output','receipts','python','candidate','prior')},
        bridge_package_dir=inv['bridge_package_dir'],
        **({'rehearsal_root':source['rehearsal_root']} if mode=='rehearsal' else {}),
        inventory=inv,layout=dict(layout),commands={},inventory_source_sha256=_hash(inventory),
        confirmation_token=str(Path(source['receipts'])/'interactive-confirmation.token'))
    record['digest'] = record_digest(record)
    return record


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='operation',required=True)
    inputs = sub.add_parser('inputs')
    for name in ('acquisition-home','conversion-manifest','staged-raw','forward-start-evidence','output'):
        inputs.add_argument('--'+name,type=Path,required=True)
    record = sub.add_parser('record')
    for name in ('inventory','layout','output'):
        record.add_argument('--'+name,type=Path,required=True)
    record.add_argument('--mode',choices=('live','rehearsal'),required=True)
    record.add_argument('--window-start-ms',type=int,required=True)
    record.add_argument('--window-end-ms',type=int,required=True)
    record.add_argument('--expected-gateway-jobs',type=int,required=True)
    args = parser.parse_args(argv)
    try:
        if args.operation=='inputs':
            result = build_cutover_inputs(**{k:v for k,v in vars(args).items() if k!='operation'})
        else:
            preflight_isolated_paths(args.layout,args.output)
            value = build_cutover_record(inventory=args.inventory,layout=json.loads(args.layout.read_bytes()),
                mode=args.mode,window=(args.window_start_ms,args.window_end_ms),expected_gateway_jobs=args.expected_gateway_jobs)
            _private_json(args.output,value)
            result = dict(version=1,units=len(value['inventory']['units']),members=len(value['inventory']['members']))
        print(json.dumps(result,sort_keys=True))
        return 0
    except Exception:
        print('{"ok":false,"error":"cutover_inputs_refused"}')
        return 1


if __name__=='__main__':
    raise SystemExit(main())
