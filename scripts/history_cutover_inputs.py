#!/usr/bin/env python3
"""Build private cutover inputs from acquired originals and a reviewed inventory."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Mapping
from contextlib import closing
from pathlib import Path
from typing import Any, Literal

from jsonschema import Draft202012Validator
from yeoman_gateway.history.convert.common import clean_text, epoch_or_iso_to_ms
from yeoman_gateway.history.convert.journal import _event
from yeoman_gateway.history.convert.memory_nodes import _line as memory_line
from yeoman_gateway.history.convert.session_jsonl import _line as session_line
from yeoman_gateway.history.layer1 import Origin, row_sha256
from yeoman_gateway.knowledge._history_sources import _principal
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
        validate_host_inventory,
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
        validate_host_inventory,
    )
    from history_maintenance_guard import preflight_isolated_paths


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rows(path: Path, table: str) -> list[dict]:
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro&immutable=1', uri=True)) as db:
        db.row_factory = sqlite3.Row
        return [dict(r) for r in db.execute('SELECT * FROM "'+table.replace('"','""')+'"')]


class InputProofError(ValueError):
    def __init__(self, code: str, store_counts: dict):
        super().__init__(code)
        self.store_counts = store_counts


def _source_paths(home: Path, members: Mapping) -> dict:
    """Match exact relative DB/session and absolute Bridge path strings by digest."""
    result = {}
    for member in members.values():
        names = ([member['path']+'/'+name for name in member['files']]
                 if member['kind']=='tree' else [member['path']])
        for name in names:
            path = home/name
            kind = 'file' if member['kind']=='tree' else member['kind']
            for spelling in (name,path.as_posix()):
                digest = hashlib.sha256(spelling.encode('utf-8')).hexdigest()
                previous = result.get(digest)
                if previous and previous[0] != path:
                    raise ValueError('ambiguous_acquired_source_path')
                if previous is None or kind=='sqlite':
                    result[digest] = path,kind
    return result


def _original_hashes(source: Path, table: str) -> set[str]:
    if table=='effects':
        # Journal originals include receipt linkage, not merely SELECT * effects.
        with closing(sqlite3.connect(source.as_uri()+'?mode=ro&immutable=1',uri=True)) as db:
            db.row_factory = sqlite3.Row
            rows = [dict(r) for r in db.execute(
                'SELECT e.*,r.provider_message_id AS r_provider_message_id,r.chat_id AS r_chat_id,'
                'r.confirmed_ms AS r_confirmed_ms FROM effects e LEFT JOIN transport_receipts r'
                ' ON r.effect_id=e.effect_id ORDER BY e.created_ms,e.effect_id,r.confirmed_ms')]
    else:
        rows = _rows(source,table)
    # This is the converter's canonical_json(default=str), including SQLite BLOBs.
    return {row_sha256(row) for row in rows}


def _file_originals(source: Path, table: str) -> dict:
    if table=='message_reference':
        return {source.name:row_sha256(json.loads(source.read_bytes()))}
    if table!='jsonl':
        return {}
    result = {}
    with source.open(encoding='utf-8',errors='replace') as stream:
        for number,line in enumerate(stream,1):
            if not line.strip():
                continue
            try:
                original = json.loads(line)
            except json.JSONDecodeError:
                original = None
            if not isinstance(original,dict):
                original = {'_unparsed':line.rstrip('\n')}
            result[str(number)] = row_sha256(original)
    return result


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


def _native_original(record: dict) -> dict:
    """Reconstruct non-encoded converted evidence directly from acquired originals."""
    original,origin = record['original'],record['origin']
    provenance = Origin(origin['store'],origin['path'],origin['table'],origin['row_key'])
    if origin['table']=='jsonl':
        return session_line(record['channel'],record['chat_id'],provenance,original)
    if origin['table']=='memory2_nodes':
        return memory_line(original,provenance)
    if origin['table']=='inbound_messages':
        timestamp,certainty = epoch_or_iso_to_ms(original.get('timestamp'))
        if timestamp is None:
            timestamp,certainty = epoch_or_iso_to_ms(original.get('created_at'))
        return dict(record,channel=original.get('channel') or 'whatsapp',chat_id=original.get('chat_id'),
            occurred_ms=timestamp,time_certainty=certainty,payload=dict(
                messageId=original.get('message_id'),senderId=original.get('sender_id'),
                participantJid=original.get('participant'),text=original.get('text')))
    return record


def _original_state(record: dict, mutations: set[tuple]) -> dict:
    """Extract only fields supported by the hash-bound original, never history."""
    original = record['original']
    if record.get('kind') != 'message' or isinstance(record.get('payload',{}).get('segments'),list):
        return {}
    if record['origin']['table'] != 'events':
        payload = record.get('payload',{})
        state = dict(channel=record['channel'],chat_id=record['chat_id'],
            native_message_id=payload.get('messageId'))
        if record['time_certainty'] in ('native', 'provider_timestamp'):
            state.update(sent_ms=record['occurred_ms'], time_certainty=record['time_certainty'])
        table = record['origin']['table']
        text_field = {'inbound_messages':'text','jsonl':'content','memory2_nodes':'content'}.get(table)
        if table == 'message_reference':
            # The pinned conversion's decoder binds these fields to encoded row bytes.
            if 'text' in payload:
                state['text'] = payload['text']
        elif text_field in original:
            cleaned = clean_text(original[text_field])
            if not cleaned.changed:
                state['text'] = original[text_field]
        if 'reply_to_message_id' in original:
            state['reply_to_native_id'] = original['reply_to_message_id']
        if 'is_deleted' in original:
            state['deleted'] = bool(original['is_deleted'])
        return state
    if original.get('kind') != 'message':
        return {}
    converted = _event(original)
    payload = converted['payload']
    raw_payload = json.loads(original['payload_json']) if original.get('payload_json') else {}
    state = dict(channel=converted['channel'],chat_id=converted['chat_id'],
        native_message_id=payload.get('messageId'),direction=converted['direction'],
        sent_ms=converted['occurred_ms'],time_certainty=converted['time_certainty'],
        provenance=converted['provenance'],deleted=bool(original['payload_purged_ms']))
    if 'text' in raw_payload:
        state.update(text=raw_payload['text'],current_text=raw_payload['text'])
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


def _enrich_legacy(legacy,statements,links,bindings,parsed_jobs,event_by_id,boundary,start):
    people_by_key = defaultdict(set)
    completed = set()
    for link in links:
        key = link['event_id'],link['revision']
        statement = statements[link['statement_id']]
        if statement['speaker_person_id']:
            people_by_key[key].add(statement['speaker_person_id'])
        if statement['status'] in ('assertion','confirmed','superseded','expired'):
            completed.add(key)
    for job,sources in parsed_jobs:
        if job['state']=='done':
            completed.update((source['event_id'],source['revision']) for source in sources)
    bindings_by_identifier = defaultdict(list)
    for binding in bindings:
        if binding['kind']=='phone_jid' and binding['mapping_verified']==1 and binding['status'] in ('active','ended'):
            bindings_by_identifier[binding['channel'],binding['value']].append(binding)
    for key,row in legacy.items():
        people = set(people_by_key[key])
        principal = row['author_principal']
        if isinstance(principal,str):
            number = principal.removeprefix('whatsapp:')
            people.update(b['person_id'] for b in bindings_by_identifier[row['channel'],number+'@s.whatsapp.net']
                if b['valid_from_ms'] <= row['occurred_at_ms']
                and (not b['valid_until_ms'] or row['occurred_at_ms'] < b['valid_until_ms']))
        if len(people)==1:
            row['author_contact_id'] = next(iter(people))
        event = event_by_id.get(key[0])
        if event:
            row.update(created_ms=event['created_ms'],boundary=boundary,forward_start=start)
        if key in completed:
            row['completed'] = True


def build_cutover_inputs(*, acquisition_home: Path, conversion_manifest: Path,
    staged_raw: Path, forward_start_evidence: Path, output: Path,
    record: Mapping[str,Any] | None = None) -> dict:
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
    optional = set(record.get('inventory',{}).get('optional_conversion_stores',())) if record else set()
    if optional and (record.get('version') != 1 or record.get('approved') is not True or not record.get('approval')
            or record.get('digest') != record_digest(record) or record.get('output') != str(acquisition_home)
            or receipt.get('inventory_digest') != record_digest(record['inventory'])):
        raise ValueError('approved_optional_store_record_required')
    source_paths = _source_paths(acquisition_home,members)
    store_errors = defaultdict(Counter)
    required_errors = []
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
    parsed_jobs = [(job,json.loads(job['sources_json'])) for job in jobs]
    for job,sources in parsed_jobs:
        for source in sources:
            key = source['event_id'],source['revision']
            if all(field in source for field in SourceRef.__dataclass_fields__):
                if key in legacy and _issued(legacy[key]) != _issued(source):
                    raise ValueError('conflicting_preserved_job_source')
                legacy.setdefault(key,dict(_issued(source),status='unknown'))
            elif key not in legacy:
                raise ValueError('unproven_legacy_job_source')
    _enrich_legacy(legacy,statements,links,bindings,parsed_jobs,event_by_id,boundary,start)
    preserved = []
    by_event = {}
    for key in sorted(legacy):
        by_event.setdefault(key[0],[]).append(key)
    by_locator = defaultdict(set)
    for key,row in legacy.items():
        by_locator[row['channel'],row['chat_id'],key[0]].add(key)
        event = event_by_id.get(key[0])
        if event:
            payload = json.loads(event['payload_json']) if event.get('payload_json') else {}
            native = payload.get('provider_message_id') or payload.get('message_id') or event.get('source_message_id')
            if native:
                by_locator[row['channel'],row['chat_id'],native].add(key)
    originals_cache = {(str(processing),'events'):{row_sha256(e) for e in events}}
    authority_cache = {str(processing):authorities}
    events_cache = {str(processing):{(e['channel'],e['chat_id'],e['target_message_id'])
        for e in events if e['kind'] in ('edit','delete')}}
    file_originals = {}
    for relative,blob in blobs.items():
        for number,line in enumerate(blob.splitlines(),1):
            record = json.loads(line)
            original,origin = record.get('original'),record.get('origin')
            if not isinstance(original,dict) or not origin:
                continue
            bound_origin = manifest['files'][relative]['rows'][number-1]['origin']
            record = _native_original(record)
            payload = record.get('payload',{})
            keys = set(by_event.get(original.get('event_id'),()))
            locator_keys = by_locator.get((record.get('channel'),record.get('chat_id'),payload.get('messageId')),())
            keys.update(key for key in locator_keys if origin['table'] != 'events' or key[0] not in event_by_id)
            inventory_entry = manifest['source_inventory'].get(bound_origin.get('inventory_id'),{})
            located = source_paths.get(inventory_entry.get('origin_path_sha256'))
            code = None
            if located is None or located[1] not in ('sqlite','file'):
                code = 'conversion_origin_not_acquired'
            else:
                source,kind = located
                cache_key = str(source),origin['table']
                if kind=='sqlite':
                    if cache_key not in originals_cache:
                        try:
                            originals_cache[cache_key] = _original_hashes(source,origin['table'])
                        except sqlite3.OperationalError:
                            originals_cache[cache_key] = set()
                    acquired_hashes = originals_cache[cache_key]
                else:
                    if cache_key not in file_originals:
                        file_originals[cache_key] = _file_originals(source,origin['table'])
                    acquired_hashes = {file_originals[cache_key].get(origin['row_key'])}
                if origin['row_sha256'] not in acquired_hashes or origin['row_sha256'] != row_sha256(original):
                    code = 'conversion_original_not_acquired'
            if code:
                store = origin.get('store','unknown_store')
                store = store if isinstance(store,str) and re.fullmatch(r'[a-z][a-z0-9_-]*',store) else 'unknown_store'
                store_errors[store][code] += 1
                if store not in optional:
                    required_errors.append(code)
                continue
            if not keys:
                continue
            if origin['table']=='events' and str(source) not in events_cache:
                events_cache[str(source)] = {(e['channel'],e['chat_id'],e['target_message_id'])
                    for e in _rows(source,'events') if e['kind'] in ('edit','delete')}
                authority_cache[str(source)] = {(r['event_id'],r['revision']):r
                    for r in _rows(source,'event_source_authority')}
            state = _original_state(record,events_cache.get(str(source),set()))
            if origin['table']=='events':
                principal = original.get('principal')
            elif origin['table']=='message_reference':
                principal = _principal(str(payload.get('senderPhoneJid') or payload.get('participantJid') or payload.get('senderId') or ''))
            else:
                # Inferred session-chat senders are not preserved authorship.
                principal = _principal(str(original.get('participant') or original.get('sender_id') or ''))
            if principal and state:
                state['author_principal'] = principal
            for key in sorted(keys):
                envelope = dict(state=state)
                authority = authority_cache.get(str(source),{}).get(key)
                if origin['table']=='events':
                    if authority and original.get('event_id')==key[0] and principal==authority['author_principal']:
                        envelope['issued'] = _authority(authority)
                    elif (original.get('event_id')!=key[0] and principal==legacy[key]['author_principal']
                            and state.get('sent_ms')==legacy[key]['occurred_at_ms']
                            and state.get('time_certainty') in ('native','provider_timestamp')):
                        envelope['issued'] = _issued(legacy[key])
                elif (principal==legacy[key]['author_principal'] and state.get('sent_ms')==legacy[key]['occurred_at_ms']
                        and state.get('time_certainty') in ('native','provider_timestamp')):
                    envelope['issued'] = _issued(legacy[key])
                if type(original.get('created_ms')) is int:
                    envelope['created_ms'] = original['created_ms']
                ref = f'{relative}#{number}'
                segments = payload.get('segments')
                refs = [f'{ref}/{i}' for i in range(len(segments))] if isinstance(segments,list) else [ref]
                for source_ref in refs:
                    if source_ref not in manifest['ref_map']:
                        raise ValueError('conversion_segment_ref_missing')
                    preserved.append(dict(event_id=key[0],revision=key[1],original=envelope,
                        preserved_original=original,envelope_sha256=row_sha256(envelope),
                        origin=bound_origin,source_ref=source_ref))
    proof_errors = {store:dict(sorted(errors.items())) for store,errors in sorted(store_errors.items())}
    if required_errors:
        raise InputProofError(required_errors[0],proof_errors)
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
    if proof_errors:
        bundle['origin_proof_errors'] = proof_errors
    _private_json(output,bundle)
    return {k:len(bundle[k]) for k in ('legacy_rows','preserved_rows','capture_rows')} | (
        {'origin_proof_errors':proof_errors} if proof_errors else {})


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
    home = Path(source['home'])
    for key,relative in dict(config_path='config.json',pause_path='data/ops/response-pauses.json',
            knowledge_db='data/knowledge/knowledge.db',processing_db='data/ops/processing.db').items():
        inv.setdefault(key,layout.get('knowledge_live') if key=='knowledge_db' and layout.get('knowledge_live') else str(home/relative))
    if 'frozen_files' not in inv:
        raise ValueError('missing_inventory_key:frozen_files')
    _paths(*(Path(path) for path in inv['frozen_files']))
    inv['frozen_watermarks'] = {str(Path(path)): _hash(Path(path)) for path in inv['frozen_files']}
    if 'prepared_text_manifest' not in inv:
        raise ValueError('missing_inventory_key:prepared_text_manifest')
    _paths(Path(inv['prepared_text_manifest']))
    inv['prepared_text_manifest_sha256'] = _hash(Path(inv['prepared_text_manifest']))
    if mode=='live' and 'gateway_socket' not in inv:
        _paths(Path(inv['config_path']))
        config = json.loads(Path(inv['config_path']).read_bytes())
        from yeoman_shared.config.loader import _migrate_config_with_change, convert_keys
        from yeoman_shared.config.schema import Config
        migrated, _ = _migrate_config_with_change(config)
        socket = Config.model_validate(convert_keys(migrated)).ipc.gateway_socket_path
        if socket is None or socket=='~/.yeoman/run/gateway.sock':
            socket = str(home/'run/gateway.sock')
        if not Path(socket).is_absolute():
            raise ValueError('invalid_gateway_socket_path')
        inv['gateway_socket'] = socket
    validate_host_inventory(inv, mode=mode)
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
    inputs.add_argument('--record',type=Path,help='Approved digest-bound record for optional store omissions')
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
            kwargs = {k:v for k,v in vars(args).items() if k not in ('operation','record')}
            if args.record is not None:
                preflight_isolated_paths(args.record)
                kwargs['record'] = json.loads(args.record.read_bytes())
            result = build_cutover_inputs(**kwargs)
        else:
            preflight_isolated_paths(args.layout,args.output)
            value = build_cutover_record(inventory=args.inventory,layout=json.loads(args.layout.read_bytes()),
                mode=args.mode,window=(args.window_start_ms,args.window_end_ms),expected_gateway_jobs=args.expected_gateway_jobs)
            _private_json(args.output,value)
            result = dict(version=1,units=len(value['inventory']['units']),members=len(value['inventory']['members']))
        print(json.dumps(result,sort_keys=True))
        return 0
    except InputProofError as error:
        print(json.dumps(dict(ok=False,error=str(error),origin_proof_errors=error.store_counts),sort_keys=True))
        return 1
    except Exception as exc:
        code = str(exc) if isinstance(exc, ValueError) and re.fullmatch(r'missing_inventory_key:[a-z_]+',str(exc)) else 'cutover_inputs_refused'
        print(json.dumps(dict(ok=False,error=code),sort_keys=True))
        return 1


if __name__=='__main__':
    raise SystemExit(main())
