"""Coordinator-only, read-only smoke evidence check followed by private ack publication."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import time
from contextlib import closing
from pathlib import Path

from yeoman_gateway.history.extract import _raw_time
from yeoman_gateway.history.layer1 import layer1_files, row_sha256
from yeoman_gateway.history.live import HistoryBoundary
from yeoman_gateway.history.reader import HistoryReader
from yeoman_shared.raw_archive.records import (
    SourceBoundary,
    archive_files,
    file_digest,
    iter_records,
)

try:
    from scripts.history_cutover import _load, _sequence, pause_facts
except ModuleNotFoundError:
    from history_cutover import _load, _sequence, pause_facts
try:
    from scripts.history_cutover_host import _path, _read_db, _write
except ModuleNotFoundError:
    from history_cutover_host import _path, _read_db, _write


def _turn_source_link(db, effect, event) -> bool:
    """Whether the effect's turn recorded this exact inbound revision as a live source.

    This is the store's real reply causality: the join is written when the event enters
    the turn, before that turn's generation and the effect it produced. A store that
    predates turn_sources has no such link; that is a refusal, not an error.
    """
    if not effect['turn_id']:
        return False
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='turn_sources' LIMIT 1").fetchone() is None:
        return False
    return db.execute(
        'SELECT 1 FROM turn_sources ts WHERE ts.turn_id=? AND ts.event_id=? AND ts.revision_at_join=? '
        "AND ts.role IN ('trigger','context') AND ts.removed_ms IS NULL "
        'AND ts.added_ms>=? AND ts.added_ms<=? LIMIT 1',
        (effect['turn_id'], event['event_id'], event['revision'],
         event['created_ms'], effect['created_ms'])).fetchone() is not None


def _ready_boundary(record: dict) -> HistoryBoundary:
    raw, history = (_path(record['layout'][k]) for k in ('raw', 'history'))
    for sub in ('whatsapp', 'backfill', 'derived', 'owner'):
        _path(str(raw/sub))
    observed = []
    # enumerate_committed may recover pending purge state; smoke evidence must only read.
    for relative, path in layer1_files([raw]):
        _path(str(path))
        digest, lines, size = file_digest(path)
        with path.open('rb') as stream:
            if size:
                stream.seek(size-1)
                if stream.read() != b'\n':
                    raise ValueError('smoke_incomplete_raw_prefix')
        observed.append(SourceBoundary(relative, lines, size, digest))
    with closing(_read_db(history)) as db:
        state = json.loads(db.execute("SELECT state_json FROM projector_state WHERE file='@runtime'").fetchone()[0])
        sources = tuple(SourceBoundary(*r) for r in db.execute(
            "SELECT file,lines,end_offset,sha256 FROM projector_state WHERE file<>'@runtime' ORDER BY file"))
    if (state.get('status') != 'ready' or type(state.get('generation')) is not int
            or state['generation'] < 1 or sources != tuple(observed)):
        raise ValueError('smoke_projection_unready')
    boundary = HistoryBoundary(state['generation'], sources)
    reader = HistoryReader(history)
    try:
        reader.open_snapshot(boundary).close()
    finally:
        reader.close()
    return boundary


def _authenticated_fence(record: dict) -> str:
    """The pause digest this attempt authenticated as its owner-stop fence.

    Read from the durable acquisition receipt, so the smoke proof belongs to this
    attempt and to the fence the procedure actually observed - not to a caller.
    """
    root = _path(record['receipts'])
    for path in sorted(root.glob('cutover-*.json')):
        phase = json.loads(_path(str(path)).read_bytes())
        if (phase.get('action') != 'acquire'
                or phase.get('record_digest') != record['digest']):
            continue
        baseline = phase.get('pause_baseline')
        if isinstance(baseline, dict) and isinstance(baseline.get('sha256'), str):
            return str(baseline['sha256'])
    raise ValueError('smoke_acquisition_fence_unauthenticated')


def _release(record: dict) -> int:
    """The release permission, bound to the authenticated acquired fence state."""
    root = _path(record['receipts'])
    if (root/'cutover.json').exists():
        raise ValueError('smoke_attempt_finished')
    phases = []
    for action, suffix in (('release-fence', ''), ('functional-smoke', '-started')):
        path = root/f'cutover-{_sequence(record).index(action)+1:02}{suffix}.json'
        phase = json.loads(_path(str(path)).read_bytes())
        if phase.get('action') != action or phase.get('record_digest') != record['digest']:
            raise ValueError('smoke_attempt_mismatch')
        phases.append(phase)
    release, smoke = phases
    ended = release.get('ended_ns')
    started = smoke.get('started_ns')
    authenticated = _authenticated_fence(record)
    if (release.get('receipt', {}).get('ok') is not True
            or release['receipt'].get('prior_pauses_preserved') is not True
            or release['receipt'].get('prior_pauses_sha256') != authenticated
            or type(ended) is not int or type(started) is not int or ended <= 0 or started < ended):
        raise ValueError('smoke_release_unproven')
    return ended // 1_000_000


def _owner_release(record: dict, *, authenticated: str, released_ms: int) -> dict:
    """Prove the owner actually released the persistent control, from the real store.

    The real pause record must show the verified intentional release: no global pause and
    no chat pause left behind. The still-fenced state and any other drift refuse, and the
    store may not be the same one the fence was taken from.
    """
    facts = pause_facts(_path(str(record['inventory']['pause_path'])), expectation='cleared')
    if facts['sha256'] == authenticated:
        raise ValueError('smoke_release_unproven')
    return facts


def publish_ack(*, record: Path, inputs: Path, owner_confirmed_arrival: bool,
                runner=subprocess.run, clock=time) -> dict:
    """No caller-supplied evidence booleans or hashes can establish delivery."""
    document = json.loads(_path(str(record)).read_bytes())
    value = _load(record, _path(document['home']), apply=False)
    if value['mode'] != 'live' or value.get('approved') is not True or not value.get('approval'):
        raise ValueError('approved_live_smoke_record_required')
    if owner_confirmed_arrival is not True:
        raise ValueError('owner_arrival_confirmation_required')
    root, home = _path(value['receipts']), _path(value['home'])
    if _path(str(inputs)) != root/'functional-smoke.inputs.json':
        raise ValueError('foreign_smoke_inputs')
    supplied = json.loads(inputs.read_bytes())
    if (not isinstance(supplied, dict) or set(supplied) != {'record_digest', 'chat_id', 'inbound_native_id', 'effect_id'}
            or supplied['record_digest'] != value['digest']
            or any(not isinstance(supplied[k], str) or not supplied[k] for k in ('chat_id', 'inbound_native_id', 'effect_id'))):
        raise ValueError('invalid_smoke_inputs')
    inv, layout = value['inventory'], value['layout']
    expected = ((inv['config_path'], home/'config.json'),
                (inv['processing_db'], home/'data/ops/processing.db'),
                (inv['pause_path'], home/'data/ops/response-pauses.json'),
                (layout['raw'], home/'data/raw'), (layout['history'], home/'data/history/history.db'))
    if any(_path(path) != target for path, target in expected):
        raise ValueError('foreign_smoke_store')
    released = _release(value)
    authenticated = _authenticated_fence(value)
    # The owner must have released the persistent control the fence was taken from.
    _owner_release(value, authenticated=authenticated, released_ms=released)
    now = int(clock.time()*1000)
    if now < released or now > released + value.get('owner_ack_timeout_seconds', 1200)*1000:
        raise ValueError('smoke_window_expired')
    env = dict(os.environ, YEOMAN_HOME=str(home), YEOMAN_SOURCE_DIR=inv['source_dir'])
    env['PYTHONPATH'] = ':'.join(str(_path(inv['source_dir'])/'packages'/p) for p in ('gateway','shared','overseer'))
    for family, command in (('raw', 'check-capture'), ('history', 'projection-status')):
        remaining = (released + value.get('owner_ack_timeout_seconds',1200)*1000 - int(clock.time()*1000))/1000
        if remaining <= 0:
            raise ValueError('smoke_window_expired')
        result = runner([value['python'], '-m', 'yeoman_gateway', family, command],
                        env=env, cwd=inv['source_dir'], capture_output=True, text=True, check=False, timeout=remaining)
        if result.returncode != 0:
            raise ValueError('smoke_cli_check_failed')
        if family == 'raw':
            if not result.stdout.startswith('status=ok '):
                raise ValueError('smoke_capture_unproven')
        else:
            status = json.loads(result.stdout)
            health = status.get('health', {})
            if (status.get('status') != 'ok' or health.get('status') != 'ready'
                    or type(health.get('generation')) is not int or health['generation'] < 1
                    or any(type(health.get(k)) is not int or health[k] != 0 for k in ('lag_lines', 'lag_bytes'))):
                raise ValueError('smoke_projection_unready')
    boundary = _ready_boundary(value)
    if boundary.generation != health['generation']:
        raise ValueError('smoke_projection_unready')
    chat, native, effect = (supplied[k] for k in ('chat_id','inbound_native_id','effect_id'))
    with closing(_read_db(_path(inv['processing_db']))) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute('SELECT event_id,trace_id,revision,created_ms FROM events WHERE channel=? AND chat_id=? '
            'AND source_message_id=? AND direction=? AND kind=?', ('whatsapp',chat,native,'in','message')).fetchall()
        if len(rows) != 1 or not released < rows[0]['created_ms'] <= now:
            raise ValueError('smoke_inbound_not_fresh')
        event = rows[0]
        e = db.execute('SELECT state,payload_kind,target_json,trace_id,turn_id,turn_revision,created_ms FROM effects WHERE effect_id=?', (effect,)).fetchone()
        target = json.loads(e['target_json']) if e else {}
        causal = e is not None and bool(event['trace_id']) and e['trace_id'] == event['trace_id']
        if e is not None and e['turn_id'] and e['trace_id'] == e['turn_id']:
            causal = db.execute('SELECT 1 FROM generation_sources s JOIN generations g USING(generation_id) '
                'WHERE g.turn_id=? AND g.revision=? AND s.event_id=? AND s.revision_at_join=? '
                "AND s.role IN ('trigger','context') AND g.created_ms>=? AND g.created_ms<=? LIMIT 1",
                (e['turn_id'],e['turn_revision'],event['event_id'],event['revision'],event['created_ms'],e['created_ms'])).fetchone() is not None
        if not causal and e is not None:
            causal = _turn_source_link(db, e, event)
        if (e is None or e['state'] != 'sent' or e['payload_kind'] != 'text'
                or target.get('channel') != 'whatsapp' or target.get('chat_id') != chat
                or not released < event['created_ms'] <= e['created_ms'] <= now
                or not causal):
            raise ValueError('smoke_causal_reply_unproven')
        receipts = db.execute('SELECT receipt_id,effect_id,attempt_id,channel,chat_id,provider_message_id,client_message_id,confirmed_ms '
            'FROM transport_receipts WHERE effect_id=? ORDER BY confirmed_ms,receipt_id', (effect,)).fetchall()
        if (len(receipts) != 1 or receipts[0]['channel'] != 'whatsapp' or receipts[0]['chat_id'] != chat
                or not receipts[0]['provider_message_id'] or not e['created_ms'] <= receipts[0]['confirmed_ms'] <= now):
            raise ValueError('smoke_transport_receipt_unproven')
        receipt = dict(receipts[0])
    inbound_refs = []
    requests, results = [], []
    for path in archive_files(_path(layout['raw']), 'whatsapp'):
        _path(str(path))
        for number, raw, _ in iter_records(path):
            if not isinstance(raw, dict):
                continue
            body = raw.get('native')
            body = body if isinstance(body, dict) else {}
            payload = body.get('payload')
            payload = payload if isinstance(payload, dict) else {}
            if (raw.get('channel') == 'whatsapp' and raw.get('chat_id') == chat
                    and raw.get('direction') == 'out' and body.get('type') == 'send_text'
                    and type(raw.get('received_ms')) is int and released < raw['received_ms'] <= now):
                if raw.get('kind') == 'outbound_request':
                    requests.append(raw)
                elif raw.get('kind') == 'outbound_result':
                    result_data = body.get('result', {})
                    if (not body.get('error') and isinstance(result_data, dict) and result_data.get('ok') is not False
                            and raw.get('native_id') == receipt['provider_message_id']):
                        results.append(raw)
            if (raw.get('channel') == 'whatsapp' and raw.get('chat_id') == chat
                    and raw.get('direction') == 'in' and raw.get('kind') == 'message'
                    and 'backfill_version' not in raw and body.get('type') == 'message'
                    and payload.get('messageId') == native and payload.get('chatJid') == chat
                    and type(raw.get('received_ms')) is int and released < raw['received_ms'] <= now):
                sent_ms, certainty = _raw_time(payload, raw)
                if certainty != 'provider_timestamp' or sent_ms is None or not released < sent_ms <= now:
                    raise ValueError('smoke_inbound_not_fresh')
                inbound_refs.append(f'{path.relative_to(layout["raw"])}#{number}')
    if len(inbound_refs) != 1:
        raise ValueError('smoke_inbound_not_archived')
    if len(results) != 1:
        raise ValueError('smoke_outbound_not_archived')
    correlation = results[0].get('correlation_id') or results[0]['native'].get('requestId')
    if (not correlation or len([r for r in requests
            if (r.get('correlation_id') or r['native'].get('requestId')) == correlation]) != 1):
        raise ValueError('smoke_outbound_request_unproven')
    with closing(_read_db(_path(layout['history']))) as db:
        db.execute('BEGIN')
        state = json.loads(db.execute("SELECT state_json FROM projector_state WHERE file='@runtime'").fetchone()[0])
        rows = db.execute('SELECT source_refs FROM messages WHERE channel=? AND chat_id=? AND native_message_id=? '
            'AND direction=? AND provenance=?', ('whatsapp',chat,native,'in','native')).fetchall()
        if (state.get('status') != 'ready' or state.get('generation') != boundary.generation
                or len(rows) != 1 or not set(inbound_refs) <= set(json.loads(rows[0][0]))):
            raise ValueError('smoke_inbound_not_projected')
    # Recheck the attempt and the owner's release after every evidence read, so a
    # timed-out, refenced or re-paused run cannot publish an acknowledgement.
    if (_release(value) != released or _ready_boundary(value) != boundary
            or _authenticated_fence(value) != authenticated
            or int(clock.time()*1000) > released + value.get('owner_ack_timeout_seconds',1200)*1000):
        raise ValueError('smoke_window_expired')
    _owner_release(value, authenticated=authenticated, released_ms=released)
    ack = dict(action='functional-smoke',record_digest=value['digest'],owner_ack=True,
        proof=dict(inbound_message_id_hash=hashlib.sha256(f'whatsapp:{chat}:{native}'.encode()).hexdigest(),
                   outbound_receipt_hash=row_sha256(receipt), observed_ms=int(clock.time()*1000)))
    _write(root/'functional-smoke.owner_ack.json', json.dumps(ack,sort_keys=True).encode())
    return dict(ok=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--record',type=Path,required=True)
    parser.add_argument('--inputs',type=Path,required=True)
    parser.add_argument('--owner-confirmed-arrival',action='store_true',required=True)
    args = parser.parse_args(argv)
    try:
        result = publish_ack(record=args.record,inputs=args.inputs,owner_confirmed_arrival=args.owner_confirmed_arrival)
    except Exception:
        result = dict(ok=False,error='smoke_ack_refused')
    print(json.dumps(result,sort_keys=True))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
