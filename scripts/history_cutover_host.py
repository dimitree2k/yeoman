"""Observed live controls and isolated, host-free rehearsal controls for cutover.

Inventory is a pinned input, never discovered from the owner's running services.
The stop fence leaves response_pauses unchanged; /stop all has no stopped-Gateway
admin route. Clean Overseer stop therefore precedes every other fence stop.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import socket
import sqlite3
import subprocess
import time
from collections.abc import Callable, Mapping
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from typing import Any

from yeoman_gateway.history.export import require_isolated_paths
from yeoman_gateway.history.live import HistoryBoundary
from yeoman_gateway.history.reader import HistoryReader
from yeoman_shared.raw_archive.records import (
    SourceBoundary,
    _import_receipt,
    copy_committed,
    enumerate_committed,
    preview_import,
)

try:
    from scripts.history_cutover import _attempt_pause_digests
    from scripts.history_maintenance_guard import preflight_isolated_paths, runtime_homes
except ModuleNotFoundError:
    from history_cutover import _attempt_pause_digests
    from history_maintenance_guard import preflight_isolated_paths, runtime_homes

SHOW = '--property=ActiveState,Result,UnitFileState,Restart,ActiveEnterTimestampMonotonic,InactiveEnterTimestampMonotonic,ExecMainStartTimestampMonotonic,ExecMainExitTimestampMonotonic,LoadState,NeedDaemonReload,DropInPaths'
SERVICE_ACTIONS = {
    'stop-overseer-clean', 'stop-timers-and-manual-routes', 'stop-gateway', 'stop-bridge',
    'suppress-restarts', 'verify-restart-suppression', 'verify-deploy-suppression',
    'verify-quiescent', 'verify-no-writer-after-deploy', 'fence-effects', 'release-fence',
    'deploy', 'start-bridge', 'start-gateway', 'start-overseer', 'start-timers',
    'resume-vetted-manual-routes',
}
READER_FAMILIES = ('knowledge', 'whatsapp', 'responder', 'tools', 'participation', 'secondary')
SELECT_ACTIONS = {f'select-{f}' for f in READER_FAMILIES}
ACK_ACTIONS = {'functional-smoke'}
COMMAND_ACTIONS = {'import-preview', 'import', 'owner-preview', 'owner-append'}


def _pause_facts(path: Any, *, expectation: str = 'any') -> dict:
    """The procedure's authenticated pause reader, imported without a cycle."""
    try:
        from scripts.history_cutover import pause_facts
    except ModuleNotFoundError:
        from history_cutover import pause_facts
    return pause_facts(Path(path), expectation=expectation)


def _raw_writer(raw: Any) -> dict:
    """Only the CLI status envelope supplies writer-state/counter proof."""
    if not isinstance(raw, dict) or not {'files', 'lines', 'root', 'started_ms', 'writer'} <= raw.keys():
        raise ValueError('invalid_raw_status')
    writer = raw['writer']
    if (not isinstance(writer, dict)
            or not {'state', 'spooled', 'pending_in_memory', 'last_error', 'updated_ms'} <= writer.keys()
            or not isinstance(raw['root'], str)
            or not isinstance(writer['state'], str) or not isinstance(writer['last_error'], str)
            or any(type(raw[k]) is not int or raw[k] < 0 for k in ('files', 'lines', 'started_ms'))
            or any(type(writer[k]) is not int or writer[k] < 0 for k in ('spooled', 'pending_in_memory', 'updated_ms'))):
        raise ValueError('invalid_raw_status')
    return writer


def _subprocess(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def gateway_socket_client(request: dict, *, socket_path: Path | None = None, timeout_seconds: float = 360) -> dict:
    from yeoman_shared.config.loader import load_config
    deadline=time.monotonic()+timeout_seconds
    def remaining():
        left=deadline-time.monotonic()
        if left<=0:
            raise TimeoutError('gateway_readiness_deadline')
        return left
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
        stream.settimeout(remaining())
        stream.connect(str(socket_path or Path(load_config().ipc.gateway_socket_path).expanduser()))
        stream.settimeout(remaining())
        stream.sendall(json.dumps(request).encode() + b'\n')
        data=bytearray()
        while b'\n' not in data and len(data)<=1024*1024:
            stream.settimeout(remaining())
            chunk=stream.recv(min(65536,1024*1024+1-len(data)))
            remaining()
            if not chunk:
                raise ValueError('invalid_ipc_response')
            data.extend(chunk)
        line=bytes(data).split(b'\n',1)[0]
        if b'\n' not in data or len(line)>1024*1024:
            raise ValueError('invalid_ipc_response')
        return json.loads(line)


def _bridge_probe(*, config_path: Path | None = None, timeout_seconds: float = 30) -> dict:
    from yeoman_gateway.channels.whatsapp_runtime import WhatsAppRuntimeManager
    from yeoman_shared.config.loader import load_config
    config = load_config(config_path=config_path)
    # The existing probe generates a token if absent. Refuse that config mutation.
    if not config.channels.whatsapp.bridge_token:
        raise ValueError('bridge_token_required')
    async def bounded_probe():
        return await asyncio.wait_for(WhatsAppRuntimeManager(config=config)._health_check_async(timeout_seconds),timeout=timeout_seconds)
    try:
        return asyncio.run(bounded_probe())
    except RuntimeError as exc:
        code='readiness_protocol_mismatch' if str(exc).startswith('Bridge protocol mismatch:') else 'invalid_readiness_evidence'
        raise ValueError(code) from None


def _protocol_version():
    from yeoman_shared.whatsapp_protocol import PROTOCOL_VERSION
    return PROTOCOL_VERSION


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _path(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('explicit_unsymlinked_path_required')
    return path


def _write(path: Path, data: bytes, *, exclusive: bool = False) -> None:
    _path(str(path))
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = path if exclusive else path.with_name(path.name + '.cutover-new')
    with target.open('xb') as stream:
        os.chmod(target, 0o600)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    if not exclusive:
        os.replace(target, path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _command_failure_code(response: subprocess.CompletedProcess) -> str:
    """Bounded, journal-safe code for a refused CLI child; the status stays visible."""
    return f'command_failed_exit_{response.returncode}' if response.returncode > 0 else 'command_failed'


def _persist_command_output(action: str, payload: Mapping[str, Any],
                            response: subprocess.CompletedProcess) -> None:
    """Keep the child's own diagnostic privately; a bounded name, never the argv."""
    if action not in COMMAND_ACTIONS:
        raise ValueError('unknown_host_action')
    root = _path(str(payload['record']['receipts']))
    for stream, suffix, always in ((response.stderr, 'stderr', True), (response.stdout, 'stdout', False)):
        if stream is None or (not always and not stream.strip()):
            continue
        _write(root / f'command-{action}.{suffix}.txt', str(stream).encode('utf-8', 'replace'))


def _command_result(action: str, payload: Mapping[str, Any],
                    response: subprocess.CompletedProcess) -> dict:
    """Interpret one CLI child; live and rehearsal must call this same function."""
    if action not in COMMAND_ACTIONS:
        raise ValueError('unknown_host_action')
    if response.returncode != 0:
        _persist_command_output(action, payload, response)
        raise ValueError(_command_failure_code(response))
    if not response.stdout.strip():
        raise ValueError('command_empty_output')
    try:
        result = json.loads(response.stdout)
    except json.JSONDecodeError:
        _persist_command_output(action, payload, response)
        raise ValueError('command_output_not_json') from None
    if not isinstance(result, dict):
        _persist_command_output(action, payload, response)
        raise ValueError('command_output_not_object')
    if action == 'import':
        result = _import_proof(payload, result)
    elif action == 'import-preview':
        result['ok'] = result.get('status') == 'dry-run'
    else:
        count = len(_path(payload['record']['layout']['owner_package']).read_text().splitlines())
        complete = result.get('validated') == count and result.get('committed', -1) + result.get('suppressed', -1) == (count if action == 'owner-append' else 0)
        result.update(ok=complete, complete=complete)
    return result


def _read_db(path: Path):
    _path(str(path))
    for suffix in ('-wal', '-shm'):
        _path(str(path) + suffix)
    if not path.is_file():
        raise FileNotFoundError(path)
    return sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)


def _boundary(payload: dict) -> dict:
    layout = payload['record']['layout']
    with closing(_read_db(_path(layout['history']))) as db:
        runtime = json.loads(db.execute("SELECT state_json FROM projector_state WHERE file='@runtime'").fetchone()[0])
        sources = [dict(relative_path=r[0], line_number=r[1], end_offset=r[2], prefix_sha256=r[3])
                   for r in db.execute("SELECT file,lines,end_offset,sha256 FROM projector_state WHERE file<>'@runtime' ORDER BY file")]
    observed = [asdict(s) for s in enumerate_committed(_path(layout['raw']))]
    committed = runtime.get('status') == 'ready' and sources == observed and type(runtime.get('generation')) is int and runtime['generation'] > 0
    reopened = False
    if committed:
        reader = HistoryReader(_path(layout['history']))
        snapshot = reader.open_snapshot(HistoryBoundary(runtime['generation'], tuple(SourceBoundary(**s) for s in sources)))
        snapshot.close()
        reopened = True
    return dict(generation=runtime.get('generation'), sources=sources, all_committed=committed, reopened=reopened)


def _capture_ready(payload: dict) -> bool:
    with closing(_read_db(_path(payload['record']['layout']['knowledge_live']))) as db:
        version = db.execute("SELECT value FROM knowledge_meta WHERE key='schema_version'").fetchone()[0]
        row = db.execute("SELECT value_json FROM knowledge_history_capture_state WHERE key='handover'").fetchone()
        if version != '3' or row is None:
            return False
        handover = json.loads(row[0])
        proof = _boundary(payload)
        actual = {s['relative_path']: s for s in proof['sources']}
        covered = all((o := actual.get(s['relative_path'])) is not None and o['end_offset'] >= s['end_offset'] and o['line_number'] >= s['line_number'] and (o['end_offset'] != s['end_offset'] or o['prefix_sha256'] == s['prefix_sha256']) for s in handover.get('sources', []))
        return handover.get('version') == 1 and type(handover.get('generation')) is int and 0 < handover['generation'] <= proof['generation'] and covered


def _knowledge_state(path: Path) -> tuple[str | None, bool]:
    try:
        with closing(_read_db(path)) as db:
            integrity = db.execute('PRAGMA integrity_check').fetchall() == [('ok',)]
            row = db.execute("SELECT value FROM knowledge_meta WHERE key='schema_version'").fetchone()
            return row[0] if row else None, integrity
    except sqlite3.DatabaseError:
        return None, False


def _restore_health(payload: dict, inventory: Mapping, raw: dict, bridge: dict) -> dict:
    # The prior schema/selection are pinned by acquisition, not by the v3 handover.
    try:
        from scripts.history_cutover import record_digest
    except ModuleNotFoundError:
        from history_cutover import record_digest
    record = payload['record']
    snapshot = _path(record['output'])
    manifest = json.loads(_path(str(snapshot/'manifest.json')).read_bytes())
    if (manifest.get('digest') != record_digest(manifest) or manifest['home'] != payload['home']
            or manifest.get('inventory_digest') != record_digest(record['inventory'])):
        raise ValueError('prior_snapshot_pin_mismatch')
    def prior_path(current: Path) -> Path:
        for entry in manifest['members']:
            target = Path(entry['source']) if entry.get('source') else Path(manifest['home'])/entry['path']
            if target == current and entry['restore'] and entry['exists']:
                prior = _path(str(snapshot/entry['path']))
                if _hash(prior) != entry['sha256']:
                    raise ValueError('prior_snapshot_changed')
                return prior
        raise ValueError('prior_health_member_missing')
    knowledge, config = _path(inventory['knowledge_db']), _path(inventory['config_path'])
    prior_version, prior_integrity = _knowledge_state(prior_path(knowledge))
    if prior_version is None or not prior_integrity:
        raise ValueError('prior_knowledge_unproven')
    prior_selection = json.loads(prior_path(config).read_bytes()).get('history', {})
    selection = json.loads(config.read_bytes()).get('history', {})
    version, integrity = _knowledge_state(knowledge)
    prior_ready = integrity and version == prior_version and selection == prior_selection
    writer = _raw_writer(raw)
    proof = dict(prior_ready=prior_ready, prior_schema_version=prior_version,
        knowledge_schema_version=version, integrity_ok=integrity, prior_selection_matches=selection == prior_selection,
        writer_ok=writer['state'] == 'ok', raw_deferred=writer['spooled'] + writer['pending_in_memory'],
        connected=bridge['whatsapp']['connected'], protocol=bridge['protocolVersion'],
        bridge_pending=bridge['outbox']['pending'], bridge_inflight=bridge['queue']['inflight'])
    proof['ok'] = (prior_ready and proof['writer_ok'] and proof['connected'] is True
        and proof['protocol'] == _protocol_version() and bridge.get('persistenceFailure') is False
        and all(type(proof[k]) is int and proof[k] == 0 for k in ('raw_deferred', 'bridge_pending', 'bridge_inflight')))
    return proof


def _ack(action: str, payload: dict, *, wait: bool = False, clock=time) -> dict:
    try:
        from scripts.history_cutover import validate_ack_timeout
    except ModuleNotFoundError:
        from history_cutover import validate_ack_timeout
    timeout = validate_ack_timeout(payload['record'])
    root = _path(payload['record']['receipts'])
    path = _path(str(root / f'{action}.owner_ack.json'))
    deadline = clock.monotonic() + timeout
    while True:
        try:
            proof = json.loads(_path(str(path)).read_bytes())
            break
        except FileNotFoundError:
            if not wait:
                raise
            remaining = deadline - clock.monotonic()
            if remaining <= 0:
                raise ValueError('owner_ack_timeout') from None
            clock.sleep(min(1, remaining))
        except (ValueError, UnicodeError):
            raise ValueError('invalid_owner_ack') from None
    if (not isinstance(proof, dict) or set(proof) != {'action', 'record_digest', 'owner_ack', 'proof'}
            or proof.get('record_digest') != payload['record']['digest'] or proof.get('action') != action
            or proof.get('owner_ack') is not True):
        raise ValueError('owner_ack_required')
    evidence = proof['proof']
    if (not isinstance(evidence, dict)
            or set(evidence) != {'inbound_message_id_hash', 'outbound_receipt_hash', 'observed_ms'}
            or any(not isinstance(evidence[k], str) or not re.fullmatch(r'[a-f0-9]{64}', evidence[k])
                   for k in ('inbound_message_id_hash', 'outbound_receipt_hash'))
            or type(evidence['observed_ms']) is not int or evidence['observed_ms'] <= 0):
        raise ValueError('invalid_owner_ack')
    if wait:
        releases = [p for p in payload.get('receipts', []) if p.get('action') == 'release-fence' and p.get('receipt', {}).get('ok') is True]
        if (len(releases) != 1 or type(releases[0].get('ended_ns')) is not int
                or not releases[0]['ended_ns'] // 1_000_000 <= evidence['observed_ms'] <= int(clock.time() * 1000)):
            raise ValueError('stale_owner_ack')
    return dict(ok=True, owner_ack=str(path), **evidence)


def _effects(payload: dict, inventory: Mapping) -> dict:
    with closing(_read_db(_path(inventory['processing_db']))) as db:
        duplicates = db.execute('SELECT COUNT(*) FROM (SELECT operation_key FROM effects GROUP BY operation_key HAVING COUNT(*)>1)').fetchone()[0]
        # Unknown effects are never re-queued/repeated. A dispatch lease would violate the hold.
        leased = db.execute("SELECT COUNT(*) FROM effects WHERE state IN ('unknown','unknown_nonrepeatable') AND lease_owner IS NOT NULL").fetchone()[0]
        repeated = db.execute("SELECT COUNT(*) FROM (SELECT effect_id FROM effect_attempts WHERE outcome='sent' GROUP BY effect_id HAVING COUNT(*)>1)").fetchone()[0]
    return dict(ok=duplicates == leased == repeated == 0, no_duplicate_effects=duplicates == repeated == 0, unknown_effects_held=leased == 0)


def _apply_texts(payload: dict, inventory: Mapping, *, copy_home: Path | None) -> dict:
    manifest = _path(inventory['prepared_text_manifest'])
    if _hash(manifest) != inventory['prepared_text_manifest_sha256']:
        raise ValueError('text_manifest_drift')
    value = json.loads(manifest.read_bytes())
    changes = []
    for item in value['actions']:
        target = _path(item['path'])
        if copy_home is not None:
            original = _path(inventory['original_home'])
            target = copy_home / target.relative_to(original)
        target = _path(str(target))
        current = target.read_bytes()
        if hashlib.sha256(current).hexdigest() != item['current_sha256']:
            raise ValueError('prepared_text_current_hash_drift')
        # Apply exact unified hunks in-process; no patch subprocess or host path lookup.
        lines, diff = current.decode().splitlines(keepends=True), item['apply_diff'].splitlines(keepends=True)
        output, cursor, index = [], 0, 2
        while index < len(diff):
            match = re.fullmatch(r'@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@.*\n?', diff[index])
            if not match:
                raise ValueError('invalid_text_hunk')
            start = int(match[1]) - 1
            if start < cursor:
                raise ValueError('overlapping_text_hunk')
            output.extend(lines[cursor:start])
            cursor, index = start, index + 1
            while index < len(diff) and not diff[index].startswith('@@'):
                line = diff[index]
                if line[0] in ' -':
                    if cursor >= len(lines) or lines[cursor] != line[1:]:
                        raise ValueError('text_hunk_drift')
                    cursor += 1
                if line[0] in ' +':
                    output.append(line[1:])
                if line[0] not in ' +-':
                    raise ValueError('invalid_text_hunk')
                index += 1
        after = ''.join([*output, *lines[cursor:]]).encode()
        if hashlib.sha256(after).hexdigest() != item['proposed_sha256']:
            raise ValueError('prepared_text_result_hash_drift')
        changes.append((target, current, after))
    unchanged = value.get('unchanged_owner_runbook')
    if unchanged:
        target = _path(unchanged['path'])
        if copy_home is not None:
            target = copy_home / target.relative_to(_path(inventory['original_home']))
        target = _path(str(target))
        if _hash(target) != unchanged['sha256']:
            raise ValueError('unchanged_owner_text_drift')
    for index, (target, current, after) in enumerate(changes):
        _write(_path(payload['record']['receipts']) / f'text-undo-{index}.txt', current, exclusive=True)
        _write(target, after)
    return dict(ok=all(_hash(t) == hashlib.sha256(a).hexdigest() for t, _, a in changes), applied=len(changes), evolve_nonblocking=True)


def _configure(payload: dict, inventory: Mapping) -> dict:
    path = _path(inventory['config_path'])
    before = path.read_bytes()
    config = json.loads(before)
    config['history'] = payload['selection']
    from yeoman_shared.config.loader import _migrate_config_with_change, convert_keys
    from yeoman_shared.config.schema import Config
    migrated, _ = _migrate_config_with_change(config)
    Config.model_validate(convert_keys(migrated))
    backup = _path(payload['record']['receipts']) / 'config-before.json'
    if not backup.exists():
        _write(backup, before, exclusive=True)
    _write(path, json.dumps(config, indent=2).encode())
    observed = json.loads(path.read_bytes())['history']
    return dict(ok=observed == payload['selection'], selection=observed, backup=str(backup))



def _prepare_inputs(payload: dict, inventory: Mapping) -> dict:
    try:
        from scripts.history_cutover_inputs import InputProofError, build_cutover_inputs
    except ModuleNotFoundError:
        from history_cutover_inputs import InputProofError, build_cutover_inputs
    layout = payload['record']['layout']
    snapshot = _path(payload['record']['output'])
    home = _path(layout['preparation_home'])
    preflight_isolated_paths(home)
    require_isolated_paths(home)
    home.mkdir(mode=0o700, parents=True, exist_ok=False)
    vector = enumerate_committed(_path(layout['raw']))
    copy_committed(_path(layout['raw']), vector, home / 'raw')
    evidence = snapshot / inventory['forward_start_evidence_member']
    if snapshot not in evidence.parents:
        raise ValueError('forward_evidence_not_acquired')
    try:
        summary = build_cutover_inputs(acquisition_home=snapshot,
            conversion_manifest=_path(layout['conversion_manifest']),staged_raw=_path(layout['staged']),
            forward_start_evidence=evidence,output=home/'cutover-inputs.json',record=payload['record'])
    except InputProofError as error:
        _write(_path(payload['record']['receipts'])/'prepare-input-bundle-errors.json',
            json.dumps(dict(error_code=str(error),origin_proof_errors=error.store_counts),sort_keys=True).encode(),exclusive=True)
        raise
    return dict(ok=True,complete=True,**summary)


def _import_proof(payload: dict, report: dict) -> dict:
    layout = payload['record']['layout']
    manifest = json.loads(_path(layout['conversion_manifest']).read_bytes())
    observed = _import_receipt(_path(layout['raw']), manifest['package_digest'])
    complete = observed is not None and observed['status'] == 'complete'
    if complete:
        checked = preview_import(_path(layout['raw']), _path(layout['staged']), manifest)
        complete = checked == observed and report.get('status') == 'complete'
    return dict(observed or {}, ok=complete, complete=complete)

def _procedure_digest(value):
    try:
        from scripts.history_cutover import record_digest
    except ModuleNotFoundError:
        from history_cutover import record_digest
    return record_digest(value)


def _baseline_inputs(payload):
    record=payload['record']
    manifest=json.loads(_path(record['output']).joinpath('manifest.json').read_bytes())
    quiescent=[p for p in payload['receipts'] if p.get('action')=='verify-quiescent']
    if (manifest.get('digest') != _procedure_digest(manifest)
            or manifest.get('inventory_digest') != _procedure_digest(record['inventory'])
            or manifest.get('home') != record['home'] or not manifest.get('ok')
            or len(quiescent)!=1 or quiescent[0].get('record_digest') != record['digest']
            or any(quiescent[0].get('receipt',{}).get(k) is not True for k in ('ok','writers_absent','bridge_stopped'))):
        raise ValueError('quiescent_baseline_required')
    receipt_root=_path(record['receipts'])
    if not any(json.loads(_path(str(p)).read_bytes())==quiescent[0] for p in receipt_root.glob('cutover-[0-9][0-9].json')):
        raise ValueError('quiescent_baseline_required')
    return manifest,quiescent[0]


def _frozen_state(record):
    from yeoman_gateway.knowledge._history_upgrade import FROZEN_IDENTITY_TABLES, _digest
    inv=record['inventory']
    knowledge=_path(inv['knowledge_db'])
    with closing(_read_db(knowledge)) as db:
        identity=_digest(db,FROZEN_IDENTITY_TABLES)
    # SQLite physical bytes and its WAL/SHM include permitted statement/job growth.
    excluded={str(knowledge)+suffix for suffix in ('','-wal','-shm')}
    watermarks={str(_path(p)):_hash(_path(p)) for p in inv['frozen_files'] if p not in excluded}
    return dict(watermarks=watermarks,identity_digest=identity)


def _capture_frozen_baseline(payload):
    manifest,quiescent=_baseline_inputs(payload)
    if payload.get('acquisition') != manifest:
        raise ValueError('quiescent_baseline_required')
    from yeoman_gateway.knowledge._history_upgrade import FROZEN_IDENTITY_TABLES, _digest
    record=payload['record']
    knowledge=_path(record['inventory']['knowledge_db'])
    snapshots=[m for m in manifest['members'] if (Path(m['source']) if m.get('source') else Path(manifest['home'])/m['path'])==knowledge and m['exists']]
    if len(snapshots)!=1:
        raise ValueError('knowledge_baseline_not_acquired')
    snapshot=_path(str(Path(record['output'])/snapshots[0]['path']))
    if _hash(snapshot)!=snapshots[0]['sha256']:
        raise ValueError('quiescent_baseline_required')
    state=_frozen_state(record)
    with closing(_read_db(snapshot)) as db:
        if _digest(db,FROZEN_IDENTITY_TABLES)!=state['identity_digest']:
            raise ValueError('frozen_identity_changed')
    baseline=dict(record_digest=payload['record']['digest'],acquisition_digest=manifest['digest'],
                  quiescent_digest=_procedure_digest(quiescent),**state)
    baseline['digest']=_procedure_digest(baseline)
    _write(_path(payload['record']['receipts'])/'frozen-baseline.json',json.dumps(baseline,sort_keys=True).encode(),exclusive=True)
    return dict(ok=True,baseline_digest=baseline['digest'])


def _verify_frozen_baseline(payload):
    manifest,quiescent=_baseline_inputs(payload)
    baseline=json.loads((_path(payload['record']['receipts'])/'frozen-baseline.json').read_bytes())
    if (baseline.get('digest')!=_procedure_digest(baseline) or baseline.get('record_digest')!=payload['record']['digest']
            or baseline.get('acquisition_digest')!=manifest['digest'] or baseline.get('quiescent_digest')!=_procedure_digest(quiescent)):
        raise ValueError('frozen_baseline_pin_mismatch')
    acquired=[p for p in payload['receipts'] if p.get('action')=='acquire']
    if len(acquired)!=1 or acquired[0].get('record_digest')!=payload['record']['digest'] or acquired[0].get('frozen_baseline_digest')!=baseline['digest'] or acquired[0].get('receipt')!=manifest:
        raise ValueError('frozen_baseline_pin_mismatch')
    if not any(json.loads(_path(str(p)).read_bytes())==acquired[0] for p in _path(payload['record']['receipts']).glob('cutover-[0-9][0-9].json')):
        raise ValueError('frozen_baseline_pin_mismatch')
    current=_frozen_state(payload['record'])
    if current['watermarks']!=baseline['watermarks']:
        raise ValueError('frozen_watermark_changed')
    if current['identity_digest']!=baseline['identity_digest']:
        raise ValueError('frozen_identity_changed')
    return dict(ok=True,baseline_digest=baseline['digest'])


def live_host_controls(*, inventory: Mapping[str, Any],
                       runner: Callable[[list[str]], subprocess.CompletedProcess] = _subprocess,
                       ipc: Callable[[dict], dict] = gateway_socket_client,
                       clock=time, bridge_probe: Callable[[], dict] = _bridge_probe,
                       proc_root: Path = Path('/proc')):
    """Return the live action callback; only observed state can establish a proof."""
    units = inventory.get('units', [])
    if any(not isinstance(u, dict) or not re.fullmatch(r'[A-Za-z0-9_.@-]+\.(service|timer)', u.get('name', '')) for u in units):
        raise ValueError('explicit_unit_inventory_required')
    names = [u['name'] for u in units]
    restart = [u['name'] for u in units if u.get('restart') == 'always']
    gateway = inventory.get('gateway_unit', 'yeoman-gateway.service')
    bridge = inventory.get('bridge_unit', 'yeoman-bridge.service')
    overseer = 'yeoman-overseer.service'
    timers = list(inventory.get('timers', []))
    associations = inventory.get('timer_services') or {}
    # A stopped timer does not stop the oneshot it already activated, so the associated
    # services are first-class members of the fence: guarded, stopped and observed.
    associated = [service for timer in timers for service in associations.get(timer, [])]
    if timers and any(not associations.get(timer) for timer in timers):
        raise ValueError('timer_service_association_required')
    routes = [*timers, *inventory.get('manual_routes', []), *[u['name'] for u in units if u.get('role') == 'a2a' or u['name'] == 'yeoman-a2a.service']]
    all_units = list(dict.fromkeys([*names, *routes, *associated]))
    if any(not re.fullmatch(r'[A-Za-z0-9_.@-]+\.(service|timer)', u) for u in [*all_units, gateway, bridge, overseer]):
        raise ValueError('invalid_unit_name')
    if any(not re.fullmatch(r'[A-Za-z0-9_.@-]+\.service', u) for u in associated):
        raise ValueError('invalid_unit_name')
    pause_before: bytes | None = None
    readiness_deadline: float | None = None

    def run(argv, payload, *, source_dir=None):
        if runner is _subprocess:
            source = source_dir or inventory['prior_source_dir' if payload.get('operation') == 'restore' else 'source_dir']
            env = dict(os.environ, YEOMAN_HOME=payload['home'], YEOMAN_SOURCE_DIR=source)
            env.pop('PYTHONPATH', None)
            if argv[:3] == [payload['record'].get('python'), '-m', 'yeoman_gateway']:
                env['PYTHONPATH'] = ':'.join(str(Path(source) / 'packages' / p) for p in ('gateway', 'shared', 'overseer'))
            kwargs = dict(timeout=max(0.001,readiness_deadline-clock.monotonic())) if readiness_deadline is not None else {}
            return subprocess.run(argv, cwd=source, env=env, capture_output=True, text=True, check=False, **kwargs)
        return runner(argv)

    def show(unit, payload):
        result = run(['systemctl', '--user', 'show', unit, SHOW], payload)
        if result.returncode != 0:
            raise ValueError('unit_observation_failed')
        state = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
        required = {'ActiveState', 'UnitFileState', 'ActiveEnterTimestampMonotonic'}
        if unit.endswith('.service'):
            required.update(('Result', 'Restart'))
        if unit == 'yeoman-overseer-alert.service':
            required.update(('ExecMainStartTimestampMonotonic', 'ExecMainExitTimestampMonotonic'))
        if not required <= state.keys():
            raise ValueError('unit_observation_incomplete')
        return state

    def stopped(selected, payload):
        return all(show(u, payload)['ActiveState'] == 'inactive' for u in selected)

    def guard_paths(payload):
        root = _path(inventory['systemd_runtime_dir'])
        digest = payload['record']['digest']
        if not re.fullmatch('[0-9a-f]{64}', digest):
            raise ValueError('invalid_guard_digest')
        marker = _path(str(root / f'.yeoman-cutover-{digest}.hold'))
        guards = {u: _path(str(root / (u + '.d') / f'zz-yeoman-cutover-{digest}.conf')) for u in all_units}
        content = f'[Unit]\nConditionPathExists=!{marker}\n'.encode()
        return marker, guards, content

    def install_guards(payload):
        marker, guards, content = guard_paths(payload)
        receipt = _path(payload['record']['receipts']) / 'restart-guards.json'
        owned = dict(record_digest=payload['record']['digest'], marker=str(marker),
                     marker_sha256=hashlib.sha256((payload['record']['digest']+'\n').encode()).hexdigest(),
                     guards={u: str(p) for u,p in guards.items()}, guard_sha256=hashlib.sha256(content).hexdigest())
        if receipt.exists():
            if json.loads(receipt.read_bytes()) != owned or (marker.exists() and _hash(marker) != owned['marker_sha256']):
                raise ValueError('guard_drift')
            if any(p.exists() and p.read_bytes() != content for p in guards.values()):
                raise ValueError('guard_drift')
            if not stopped(all_units, payload):
                raise ValueError('guard_writer_active')
        else:
            if marker.exists() or any(p.exists() for p in guards.values()):
                raise ValueError('guard_path_exists')
            _write(receipt, json.dumps(owned,sort_keys=True).encode(),exclusive=True)
            _write(marker, (payload['record']['digest']+'\n').encode(), exclusive=True)
        if not marker.exists():
            _write(marker,(payload['record']['digest']+'\n').encode(),exclusive=True)
        for path in guards.values():
            if not path.exists():
                _write(path, content, exclusive=True)
        if run(['systemctl','--user','daemon-reload'],payload).returncode:
            raise ValueError('guard_reload_failed')

    def guard_loaded(unit, payload, state):
        marker, guards, content = guard_paths(payload)
        path = guards[unit]
        if (not marker.exists() or marker.read_bytes() != (payload['record']['digest']+'\n').encode()
                or not path.exists() or path.read_bytes() != content):
            raise ValueError('guard_drift')
        if (state.get('LoadState') != 'loaded' or state.get('NeedDaemonReload') != 'no'
                or str(path) not in state.get('DropInPaths','').split()):
            return False
        escaped = ''.join(c if c.isascii() and c.isalnum() else f'_{ord(c):02x}' for c in unit)
        response = run(['busctl','--user','--json=short','get-property','org.freedesktop.systemd1',
                        '/org/freedesktop/systemd1/unit/'+escaped,'org.freedesktop.systemd1.Unit','Conditions'],payload)
        if response.returncode:
            raise ValueError('guard_condition_unobservable')
        value = json.loads(response.stdout)
        return value.get('type') == 'a(sbbsi)' and any(
            isinstance(c,list) and len(c)==5 and c[:4] == ['ConditionPathExists',False,True,str(marker)]
            for c in value.get('data',[]))

    def suppression(payload):
        before = {u: show(u, payload) for u in all_units}
        matches = all((s['Restart'] == 'always') == (u in restart) for u,s in before.items() if u in names)
        loaded = all(guard_loaded(u,payload,s) and s['ActiveState']=='inactive' for u,s in before.items())
        clock.sleep(30)
        after = {u: show(u,payload) for u in all_units}
        return matches and loaded and all(guard_loaded(u,payload,s) and s['ActiveState']=='inactive' for u,s in after.items())

    def release_guards(payload, released):
        """Release the selected units and every stale guard a prior attempt left armed.

        A failed attempt never reaches its start phases, so its marker and drop-ins stay
        installed; the next attempt's restore must clear them or its starts hit the old
        condition. Only strictly named, owned, exact-body files are removed: anything else
        is foreign drift and is reported in the phase receipt instead.
        """
        current = payload['record']['digest']
        marker, guards, content = guard_paths(payload)
        receipt = _path(payload['record']['receipts'])/'restart-guards.json'
        expected=dict(record_digest=current,marker=str(marker),
            marker_sha256=hashlib.sha256((current+'\n').encode()).hexdigest(),
            guards={u:str(p) for u,p in guards.items()},guard_sha256=hashlib.sha256(content).hexdigest())
        if not receipt.exists() or json.loads(receipt.read_bytes()) != expected:
            raise ValueError('guard_ownership_unproven')
        for unit in released:
            if not guard_loaded(unit,payload,show(unit,payload)):
                raise ValueError('guard_loaded_unproven')
        root = _path(inventory['systemd_runtime_dir'])
        owner_uid = inventory.get('owner_uid', os.getuid())
        released_paths = {guards[u] for u in released if u in guards}
        removed: list[dict[str, Any]] = []
        retained: list[dict[str, Any]] = []
        markers: list[tuple[Path, str | None]] = []
        for kind, path, digest in _guard_candidates(root):
            if kind == 'marker':
                markers.append((path, digest))
                continue
            if digest is None or not _owned_guard(path, kind=kind, digest=digest, root=root, owner_uid=owner_uid):
                retained.append(dict(path=str(path), kind=kind,
                    reason='name_not_procedure_owned' if digest is None else 'content_or_owner_drift'))
                continue
            if digest == current and path not in released_paths:
                continue  # this attempt's fence for a unit that is not being started yet
            path.unlink()
            removed.append(dict(path=str(path), kind=kind, digest=digest,
                reason='released' if digest == current else 'stale_attempt'))
        referenced = {digest for kind,_path_,digest in _guard_candidates(root) if kind=='dropin' and digest}
        for path, digest in markers:
            if digest is None or not _owned_guard(path, kind='marker', digest=digest, root=root, owner_uid=owner_uid):
                retained.append(dict(path=str(path), kind='marker',
                    reason='name_not_procedure_owned' if digest is None else 'content_or_owner_drift'))
                continue
            if digest in referenced:
                continue
            path.unlink()
            removed.append(dict(path=str(path), kind='marker', digest=digest,
                reason='released' if digest == current else 'stale_attempt'))
        if run(['systemctl','--user','daemon-reload'],payload).returncode:
            raise ValueError('guard_reload_failed')
        for unit in released:
            state=show(unit,payload)
            if state.get('NeedDaemonReload')!='no':
                raise ValueError('guard_release_unproven')
            # A retained drift file only blocks the start while its marker exists; an
            # unarmed one leaves the unit startable and stays reported above.
            for entry in state.get('DropInPaths','').split():
                match = re.fullmatch(r'zz-yeoman-cutover-([0-9a-f]{64})\.conf', Path(entry).name)
                if match and (root/f'.yeoman-cutover-{match[1]}.hold').exists():
                    raise ValueError('guard_release_unproven')
        return dict(removed=removed, retained=retained)

    def quiescent(payload):
        absent = stopped(all_units, payload)
        owner_uid = inventory.get('owner_uid', os.getuid())
        if type(owner_uid) is not int or owner_uid != os.getuid():
            raise ValueError('writer_owner_uid_mismatch')
        configured = {Path(u['executable']) for u in units}
        if any(not p.is_absolute() for p in configured):
            raise ValueError('absolute_writer_executables_required')
        executables = configured | {p.resolve() for p in configured}
        for entry in proc_root.iterdir():
            if not entry.name.isdecimal():
                continue
            try:
                if entry.stat().st_uid != owner_uid:
                    continue
                try:
                    executable = (entry / 'exe').resolve(strict=True)
                except PermissionError:
                    executable = None
                try:
                    raw = (entry / 'cmdline').read_bytes()
                except PermissionError as exc:
                    raise ValueError('writer_cmdline_unobservable') from exc
                if executable is None and (not raw or not raw.endswith(b'\0')):
                    raise ValueError('writer_cmdline_unobservable')
                arguments = raw.split(b'\0')
            except FileNotFoundError:
                continue
            if executable in executables or any(os.fsencode(p) in arguments for p in executables):
                absent = False
        return dict(writers_absent=absent, bridge_stopped=stopped([bridge], payload))

    def probe_bridge():
        bound=min(30,max(0.001,readiness_deadline-clock.monotonic())) if readiness_deadline is not None else 30
        return (_bridge_probe(config_path=_path(inventory['config_path']),timeout_seconds=bound)
                if bridge_probe is _bridge_probe else bridge_probe())

    def validate_bridge(health):
        try:
            if (not isinstance(health,dict) or type(health['protocolVersion']) is not int
                    or type(health['whatsapp']['connected']) is not bool
                    or type(health['persistenceFailure']) is not bool
                    or any(type(health[a][b]) is not int or health[a][b]<0 for a,b in [('outbox','pending'),('queue','inflight')])):
                raise ValueError('invalid_readiness_evidence')
            if health['protocolVersion'] != _protocol_version():
                raise ValueError('readiness_protocol_mismatch')
            if health['persistenceFailure']:
                raise ValueError('readiness_persistence_failure')
        except (KeyError,TypeError) as exc:
            raise ValueError('invalid_readiness_evidence') from exc
        return health

    def wait_ready(sample,payload):
        nonlocal readiness_deadline
        try:
            from scripts.history_cutover import validate_readiness_timeout
        except ModuleNotFoundError:
            from history_cutover import validate_readiness_timeout
        readiness_deadline=clock.monotonic()+validate_readiness_timeout(payload['record'])
        try:
            while True:
                try:
                    result=sample(payload)
                    if result['ok'] and clock.monotonic()<=readiness_deadline:
                        return result
                except (FileNotFoundError,ConnectionRefusedError,TimeoutError,subprocess.TimeoutExpired):
                    pass
                remaining=readiness_deadline-clock.monotonic()
                if remaining<=0:
                    raise ValueError('readiness_timeout')
                clock.sleep(min(30,remaining))
        finally:
            readiness_deadline=None

    def raw_observed(payload):
        response=run([payload['record']['python'],'-m','yeoman_gateway','raw','status','--json'],payload)
        if response.returncode:
            raise ValueError('raw_status_observation_failed')
        raw=json.loads(response.stdout)
        writer=_raw_writer(raw)
        if writer['state']!='ok' or writer['last_error']:
            raise ValueError('readiness_persistence_failure')
        return raw

    def health_sample(payload):
        health=validate_bridge(probe_bridge())
        raw=raw_observed(payload)
        writer=_raw_writer(raw)
        if payload.get('operation')=='restore':
            result=_restore_health(payload,inventory,raw,health)
            if not result['prior_ready']:
                raise ValueError('prior_readiness_mismatch')
            return result
        return dict(ok=health['whatsapp']['connected'] and writer['spooled']==writer['pending_in_memory']==health['outbox']['pending']==health['queue']['inflight']==0,
                    connected=health['whatsapp']['connected'],protocol=health['protocolVersion'],
                    bridge_pending=health['outbox']['pending'],bridge_inflight=health['queue']['inflight'],writer_ok=True)

    def tails(payload):
        if payload.get('operation') == 'restore':
            raw = raw_observed(payload)
            result=_restore_health(payload, inventory, raw, validate_bridge(probe_bridge()))
            if not result['prior_ready']:
                raise ValueError('prior_readiness_mismatch')
            return result
        request = {'cmd': 'history_control', 'args': {'operation': 'status'}}
        health = (gateway_socket_client(request, socket_path=_path(inventory['gateway_socket']),timeout_seconds=max(0.001,readiness_deadline-clock.monotonic()))
                  if ipc is gateway_socket_client else ipc(request))
        # The response is health-only; persisted vector and a reopened lease supply the rest.
        raw = raw_observed(payload)
        bridge_health = validate_bridge(probe_bridge())
        writer = _raw_writer(raw)
        if writer['state']!='ok' or writer['last_error']:
            raise ValueError('readiness_persistence_failure')
        if not isinstance(health,dict) or health.get('status')!='ok' or not isinstance(health.get('health'),dict):
            raise ValueError('invalid_readiness_evidence')
        h=health['health']
        if h.get('status') not in ('ready','starting','rebuilding','backlog') or any(type(h.get(k)) is not int or h[k]<0 for k in ('generation','lag_lines','lag_bytes')):
            raise ValueError('invalid_readiness_evidence')
        proof = _boundary(payload)
        proof.update(raw_deferred=writer['spooled'] + writer['pending_in_memory'], bridge_pending=bridge_health['outbox']['pending'], bridge_inflight=bridge_health['queue']['inflight'], capture_ready=_capture_ready(payload))
        h = health.get('health', {})
        proof['all_committed'] = proof['all_committed'] and health.get('status') == 'ok' and h.get('status') == 'ready' and h.get('generation') == proof['generation'] and h.get('lag_lines') == h.get('lag_bytes') == 0
        proof['ok'] = bridge_health.get('whatsapp', {}).get('connected') is True and bridge_health.get('protocolVersion') == _protocol_version() and proof['all_committed'] and proof['capture_ready'] and writer['state'] == 'ok' and all(type(proof[k]) is int and proof[k] == 0 for k in ('raw_deferred', 'bridge_pending', 'bridge_inflight'))
        return proof

    def pause_bytes():
        path = _path(inventory['pause_path'])
        return path.read_bytes() if path.exists() else b''

    def execute(action: str, payload: dict) -> dict:
        nonlocal pause_before
        if action == 'stop-overseer-clean':
            alert_before = show('yeoman-overseer-alert.service', payload)
            since = int(clock.monotonic() * 1_000_000)
            run(['systemctl', '--user', 'stop', overseer], payload)
            state = show(overseer, payload)
            alert = show('yeoman-overseer-alert.service', payload)
            stamp = int(alert['ActiveEnterTimestampMonotonic'])
            fired = stamp >= since and stamp > int(alert_before['ActiveEnterTimestampMonotonic'])
            # A pre-existing alert activation after the sampled stop instant is also unsafe.
            fired = fired or stamp > since
            for key in ('ExecMainStartTimestampMonotonic', 'ExecMainExitTimestampMonotonic'):
                fired |= int(alert[key]) >= since and int(alert[key]) > int(alert_before[key])
            # An activating/deactivating alert oneshot is not quiescent; only the exact
            # inactive state is observed, and its disposition is recorded.
            alert_state = str(alert['ActiveState'])
            clean = state['ActiveState'] == 'inactive' and state['Result'] == 'success'
            quiet = alert_state == 'inactive'
            result = dict(ok=clean and quiet and not fired, clean=clean, alert_fired=fired,
                          alert_state=alert_state, alert_quiet=quiet,
                          alert_result=str(alert.get('Result', '')),
                          overseer_state=str(state['ActiveState']))
        elif action in ('stop-timers-and-manual-routes', 'stop-gateway', 'stop-bridge'):
            if action == 'stop-timers-and-manual-routes':
                selected = list(dict.fromkeys([*routes, *associated]))
            else:
                selected = [gateway if action == 'stop-gateway' else bridge]
            for u in selected:
                run(['systemctl', '--user', 'stop', u], payload)
            # Strictly inactive: activating/deactivating oneshots are not quiescent.
            inactive = stopped(selected, payload)
            result = dict(ok=inactive, units=selected, strictly_inactive=inactive,
                          timer_services=sorted(set(associated)))
        elif action == 'suppress-restarts':
            install_guards(payload)
            suppressed = suppression(payload)
            result = dict(ok=suppressed, suppressed=suppressed)
        elif action in ('verify-restart-suppression', 'verify-deploy-suppression', 'verify-no-writer-after-deploy'):
            suppressed = suppression(payload)
            result = dict(ok=suppressed, suppressed=suppressed)
            if action == 'verify-no-writer-after-deploy':
                result.update(quiescent(payload))
                result['ok'] &= result['writers_absent']
        elif action == 'verify-quiescent':
            result = quiescent(payload)
            result['ok'] = result['writers_absent'] and result['bridge_stopped']
        elif action == 'fence-effects':
            pause_before = pause_bytes()
            # Authenticate the pre-stop baseline: the owner's persistent, indefinite
            # global pause with an empty unrelated-chat baseline. Missing or drifted
            # state refuses before any writer is touched.
            baseline = _pause_facts(inventory['pause_path'], expectation='fenced')
            if baseline['sha256'] != hashlib.sha256(pause_before).hexdigest():
                raise ValueError('pause_baseline_drift')
            for phase in ('stop-overseer-clean', 'stop-timers-and-manual-routes', 'stop-gateway', 'stop-bridge'):
                if not execute(phase, payload)['ok']:
                    raise ValueError('stop_fence_unproven')
            if (_path(payload['record']['receipts'])/'restart-guards.json').exists():
                install_guards(payload)
                if not suppression(payload):
                    raise ValueError('stop_fence_unproven')
            fenced = stopped(all_units, payload)
            preserved = pause_bytes() == pause_before
            result = dict(ok=fenced and preserved, fenced=fenced, prior_pauses_preserved=preserved,
                          fence='verified-inactive', units=all_units,
                          pause_baseline_sha256=baseline['sha256'],
                          pause_global_until_ms=baseline['global_until_ms'],
                          pause_chat_keys=baseline['chat_keys'],
                          alert_state=show('yeoman-overseer-alert.service', payload)['ActiveState'])
        elif action == 'release-fence':
            if payload.get('operation') == 'restore':
                # A restore puts the authenticated owner-stop record back, so the release
                # permission names that restored state, not the pre-restore bytes.
                facts = _pause_facts(inventory['pause_path'], expectation='fenced')
                authenticated = _attempt_pause_digests(payload['record'])
                preserved = facts['sha256'] in authenticated
                name = facts['sha256']
            else:
                preserved = pause_before is not None and pause_bytes() == pause_before
                name = hashlib.sha256(pause_before or b'').hexdigest() if pause_before is not None else ''
                if pause_before is None:
                    raise ValueError('release_without_fence')
            active = all(show(u, payload)['ActiveState'] == 'active' for u in (bridge, gateway))
            result = dict(ok=preserved and active, prior_pauses_preserved=preserved,
                          prior_pauses_sha256=name)
        elif action.startswith('start-') or action == 'resume-vetted-manual-routes':
            starts = {'start-bridge': [bridge], 'start-gateway': [gateway], 'start-overseer': [overseer], 'start-timers': timers, 'resume-vetted-manual-routes': [u for u in routes if u not in timers]}
            if action not in starts:
                raise ValueError('unknown_host_action')
            selected = starts[action]
            # Releasing a timer's guard without releasing its associated oneshot would
            # leave that service permanently suppressed.
            released = [*selected, *[s for u in selected for s in associations.get(u, [])]]
            guards = release_guards(payload, released)
            for u in selected:
                run(['systemctl', '--user', 'start', u], payload)
            result = dict(ok=all(show(u, payload)['ActiveState'] == 'active' for u in selected),
                          units=selected, released=released, guards=guards)
        elif action == 'deploy':
            if not suppression(payload):
                raise ValueError('deploy_suppression_unproven')
            deployed = run([inventory['yeoman'], 'deploy'], payload)
            proof = execute('verify-no-writer-after-deploy', payload)
            result = dict(proof, deployed=deployed.returncode == 0)
            result['ok'] &= result['deployed']
        elif action in ('drain-durable-tails', 'all-committed-barrier'):
            result = wait_ready(tails,payload)
        elif action == 'health':
            result = wait_ready(health_sample,payload)
        elif action == 'verify-effect-deduplication':
            result = _effects(payload, inventory)
        elif action == 'capture-frozen-baseline':
            result = _capture_frozen_baseline(payload)
        elif action == 'frozen-watermarks':
            result = _verify_frozen_baseline(payload)
        elif action == 'configure-retirement' or action in SELECT_ACTIONS:
            result = _configure(payload, inventory)
        elif action == 'apply-prepared-texts':
            result = _apply_texts(payload, inventory, copy_home=None)
        elif action == 'verify-import-origins':
            code = "import json,yeoman_gateway,yeoman_shared,yeoman_overseer;print(json.dumps([m.__file__ for m in (yeoman_gateway,yeoman_shared,yeoman_overseer)]))"
            response = run([inventory['tool_python'], '-c', code], payload)
            paths = json.loads(response.stdout)
            root = inventory['prior_source_dir' if payload.get('operation') == 'restore' else 'source_dir']
            verified = len(paths) == 3 and all(_path(root) in _path(p).parents for p in paths)
            result = dict(ok=verified, imports_verified=verified, origins=paths)
        elif action == 'validate-capture-handover':
            def capture_sample(p):
                response=run([p['record']['python'],'-m','yeoman_gateway','raw','check-capture'],p)
                if response.returncode:
                    raise ValueError('raw_capture_failed')
                ready=_capture_ready(p)
                return dict(ok=ready,capture_ready=ready,capture_check_sha256=hashlib.sha256(response.stdout.encode()).hexdigest())
            result=wait_ready(capture_sample,payload)
            return dict(result,mode='live')

        elif action == 'preflight':
            pinned = inventory['pinned_files']
            verified = bool(pinned) and all(_hash(_path(p)) == digest for p, digest in pinned.items())
            source = _path(inventory['source_dir'])
            verified &= (source / 'pyproject.toml').is_file()
            result = dict(ok=verified, pins_verified=verified)
        elif action in COMMAND_ACTIONS:
            response = run(payload['argv'], payload)
            result = _command_result(action, payload, response)
            result['ok'] &= response.returncode == 0
        elif action == 'prepare-input-bundle':
            result = _prepare_inputs(payload, inventory)
        elif action in ACK_ACTIONS:
            result = _ack(action, payload, wait=True, clock=clock)
        elif action in ('capture-suppression-delta', 'reapply-suppression-delta', 'verify-current-denials'):
            result = _suppression_delta(action, payload, inventory)
        elif action == 'restore-software-install-config-units':
            # Whole-set restore already copied inventory files, including config/units/text.
            # The restored unit files are on disk but not loaded yet, so reload first: the
            # pre-deploy suppression proof must observe the manager that will run them.
            if run(['systemctl','--user','daemon-reload'],payload).returncode:
                raise ValueError('guard_reload_failed')
            if not suppression(payload):
                raise ValueError('restore_deploy_suppression_unproven')
            response = run([inventory['prior_yeoman'], 'deploy'], payload, source_dir=inventory['prior_source_dir'])
            run(['systemctl', '--user', 'daemon-reload'], payload)
            proof = execute('verify-no-writer-after-deploy', payload)
            pins = inventory['prior_pinned_files']
            restored = bool(pins) and all(_hash(_path(p)) == digest for p, digest in pins.items())
            result = dict(ok=response.returncode == 0 and proof['ok'] and restored, restored=restored)
        else:
            raise ValueError('unknown_host_action')
        return dict(result, mode='live')

    execute.mode = 'live'
    return execute


def _suppression_delta(action: str, payload: dict, inventory: Mapping) -> dict:
    """Retain effects and carry revocations into either the v3 ledger or prior v2 authority."""
    path = _path(inventory['knowledge_db'])
    root = _path(payload['record']['receipts'])
    delta_path = root / 'suppression-delta.json'
    processing = _path(inventory['processing_db']) if 'processing_db' in inventory else None
    if action == 'capture-suppression-delta':
        with closing(_read_db(path)) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            # knowledge_history_sources is created by the v3 upgrade: a cutover that failed
            # before publish-v3 leaves the prior schema, whose correct capture is no v3
            # sources at all (the prior statements below carry the rest of the delta).
            sources = (db.execute("SELECT event_id,revision,reason FROM knowledge_history_sources WHERE revoked=1").fetchall()
                       if 'knowledge_history_sources' in tables else [])
            statements = (db.execute("SELECT statement_id,status,revoked_at_ms FROM knowledge_statements WHERE revoked_at_ms IS NOT NULL").fetchall()
                          if 'knowledge_statements' in tables else [])
            delta = dict(sources=sources, statements=statements)
        if processing is not None:
            saved = root / 'processing-current.db'
            _path(str(saved))
            if saved.exists():
                raise ValueError('occupied_processing_delta')
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            with closing(_read_db(processing)) as source, closing(sqlite3.connect(saved)) as target:
                source.backup(target)
                if target.execute('PRAGMA integrity_check').fetchone() != ('ok',):
                    raise ValueError('processing_delta_corrupt')
            saved.chmod(0o600)
            with saved.open('rb') as stream:
                os.fsync(stream.fileno())
            delta['processing'] = dict(path=str(saved), sha256=_hash(saved))
        _write(delta_path, json.dumps(delta).encode(), exclusive=True)
        return dict(ok=True, rows=len(delta['sources']) + len(delta['statements']), sha256=_hash(delta_path))
    delta = json.loads(delta_path.read_bytes())
    for suffix in ('-wal', '-shm'):
        _path(str(path) + suffix)
    if action == 'reapply-suppression-delta':
        if 'processing' in delta:
            saved = _path(delta['processing']['path'])
            if saved.parent != root or _hash(saved) != delta['processing']['sha256'] or processing is None:
                raise ValueError('processing_delta_pin_mismatch')
            # A prior processing snapshot would revive ready effects or lose new receipts.
            for suffix in ('-wal', '-shm'):
                _path(str(processing) + suffix).unlink(missing_ok=True)
            _write(processing, saved.read_bytes())
        with closing(sqlite3.connect(path)) as db, db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for event_id, revision, reason in delta['sources']:
                if 'knowledge_history_sources' in tables:
                    db.execute('UPDATE knowledge_history_sources SET revoked=1,reason=COALESCE(reason,?) WHERE event_id=? AND revision=?', (reason, event_id, revision))
                if 'knowledge_statement_sources' in tables:
                    # Prior v2 statements retain their original source keys (issued aliases).
                    db.execute("UPDATE knowledge_statements SET status='revoked',revoked_at_ms=COALESCE(revoked_at_ms,?) WHERE statement_id IN (SELECT statement_id FROM knowledge_statement_sources WHERE event_id=? AND revision=?)", (int(time.time()*1000), event_id, revision))
                    db.execute("UPDATE knowledge_statement_sources SET status='revoked' WHERE event_id=? AND revision=?", (event_id, revision))
            for statement_id, status, stamp in delta['statements']:
                db.execute('UPDATE knowledge_statements SET status=?,revoked_at_ms=COALESCE(revoked_at_ms,?) WHERE statement_id=?', (status, stamp, statement_id))
        if processing is not None:
            from yeoman_gateway.processing.store import ProcessingStore
            store = ProcessingStore(processing)
            try:
                for event_id, revision, _ in delta['sources']:
                    store.revoke_event_source_authority(event_id, revision=revision)
            finally:
                store.close()
    with closing(_read_db(path)) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        current = True
        for event_id, revision, _ in delta['sources']:
            if 'knowledge_history_sources' in tables:
                row = db.execute('SELECT revoked FROM knowledge_history_sources WHERE event_id=? AND revision=?', (event_id, revision)).fetchone()
                current &= row is None or row == (1,)
            elif processing is None:
                current = False  # Prior v2 authority cannot be inferred from a v3 source ID.
            if 'knowledge_statement_sources' in tables:
                current &= db.execute("SELECT COUNT(*) FROM knowledge_statement_sources WHERE event_id=? AND revision=? AND status!='revoked'", (event_id, revision)).fetchone()[0] == 0
        for statement_id, _, _ in delta['statements']:
            row = db.execute('SELECT revoked_at_ms FROM knowledge_statements WHERE statement_id=?', (statement_id,)).fetchone()
            current &= row is None or row[0] is not None
    if processing is not None:
        with closing(_read_db(processing)) as db:
            for event_id, revision, _ in delta['sources']:
                row = db.execute('SELECT revoked_at_ms FROM event_source_authority WHERE event_id=? AND revision=?', (event_id, revision)).fetchone()
                current &= row is None or row[0] is not None
    # Absent post-cutover sources/statements have no row in the older set to revive.
    return dict(ok=current, delta_applied=current, current_denials=current)


def _rehearsal_command_argv(argv: Any, *, root: Path, interpreter: Any) -> None:
    """A rehearsal command may name only the pinned interpreter and the rehearsal root.

    Containment is decided lexically before any path is opened, so a command that
    names the live home is refused without touching it.
    """
    if (not isinstance(argv, list) or not argv
            or any(not isinstance(a, str) or not a for a in argv)):
        raise ValueError('invalid_command_argv')
    if interpreter is not None and argv[0] != interpreter:
        raise ValueError('rehearsal_command_interpreter')
    for argument in argv[1:]:
        if not argument.startswith('/'):
            continue
        candidate = Path(os.path.abspath(argument))
        if candidate != root and root not in candidate.parents:
            raise ValueError('rehearsal_command_outside_root')
        preflight_isolated_paths(candidate)


def _guard_body(root: Path, digest: str) -> bytes:
    """The exact body every procedure-owned cutover drop-in carries."""
    return f'[Unit]\nConditionPathExists=!{root}/.yeoman-cutover-{digest}.hold\n'.encode()


def _owned_guard(path: Path, *, kind: str, digest: str, root: Path, owner_uid: int) -> bool:
    """Only a strict name, the owner's uid and the exact body make a guard file ours.

    Anything else is foreign drift: the caller keeps it and reports it, never deletes it.
    """
    if path.is_symlink() or not path.is_file():
        return False
    try:
        if path.stat().st_uid != owner_uid:
            return False
        expected = _guard_body(root, digest) if kind == 'dropin' else (digest + '\n').encode()
        return path.read_bytes() == expected
    except OSError:
        return False


def _guard_candidates(root: Path) -> list[tuple[str, Path, str | None]]:
    """Every cutover-named candidate under the runtime dir as (kind, path, digest).

    The digest is None when the name is not exactly procedure-shaped, so the caller can
    report that file as retained drift instead of silently ignoring or deleting it.
    """
    candidates: list[tuple[str, Path, str | None]] = []
    if not root.is_dir():
        return candidates
    for name in sorted(os.listdir(root)):
        entry = root / name
        if 'cutover' not in name or entry.is_dir():
            continue
        match = re.fullmatch(r'\.yeoman-cutover-([0-9a-f]{64})\.hold', name)
        candidates.append(('marker', entry, match[1] if match else None))
    for name in sorted(os.listdir(root)):
        directory = root / name
        if (re.fullmatch(r'[A-Za-z0-9_.@-]+\.(?:service|timer)\.d', name) is None
                or directory.is_symlink() or not directory.is_dir()):
            continue
        for entry_name in sorted(os.listdir(directory)):
            entry = directory / entry_name
            if 'cutover' not in entry_name or entry.is_dir():
                continue
            match = re.fullmatch(r'zz-yeoman-cutover-([0-9a-f]{64})\.conf', entry_name)
            candidates.append(('dropin', entry, match[1] if match else None))
    return candidates


def _rehearsal_source_dir(inventory: Mapping[str, Any], source_dir: Any = None) -> Path:
    """The pinned checkout a rehearsal command runs from, or one bounded refusal.

    A runtime home is refused lexically before any path is stat'ed, so a source_dir
    that names the live home is never opened; every other shape (relative, not a
    directory, symlinked, or refused by the shared guard) gets the same bounded code.
    """
    source = Path(str(source_dir or inventory.get('source_dir') or ''))
    candidate = Path(os.path.abspath(str(source)))
    if (not source.is_absolute()
            or any(candidate == home or home in candidate.parents for home in runtime_homes())
            or not source.is_dir()
            or any(p.is_symlink() for p in (source, *source.parents))):
        raise ValueError('rehearsal_source_dir_unusable')
    try:
        preflight_isolated_paths(source)
    except ValueError:
        raise ValueError('rehearsal_source_dir_unusable') from None
    return source


def rehearsal_host_controls(*, copy_home: Path, inventory: Mapping[str, Any], rehearsal_root: Path | None = None, runner=None, **_):
    """Simulate service actions; data proofs come exclusively from the isolated copy.

    Commands are executed, never re-implemented: a rehearsal phase runs the same
    ``payload['argv']`` as live, through a runner that pins the copy home, the pinned
    source checkout and the rehearsal root.
    """
    preflight_isolated_paths(copy_home)
    require_isolated_paths(copy_home)
    copy_home = copy_home.resolve()
    root = _path(str(rehearsal_root)).resolve() if rehearsal_root is not None else copy_home
    preflight_isolated_paths(root)
    require_isolated_paths(root)
    if copy_home != root and root not in copy_home.parents:
        raise ValueError('rehearsal_layout_outside_root')
    local = dict(inventory)
    for key in ('processing_db', 'config_path', 'knowledge_db', 'pause_path'):
        if key in local:
            path = _path(local[key])
            if copy_home not in path.parents:
                raise ValueError('rehearsal_input_outside_copy')

    def run(argv, payload, *, source_dir=None):
        interpreter = payload['record'].get('python')
        _rehearsal_command_argv(argv, root=root, interpreter=interpreter)
        # The pinned checkout is validated for every runner, so an unusable source_dir is
        # a bounded control refusal instead of a late failure inside a live-like child.
        source = _rehearsal_source_dir(inventory, source_dir)
        if runner is None or runner is _subprocess:
            env = dict(os.environ, YEOMAN_HOME=str(copy_home), YEOMAN_SOURCE_DIR=str(source))
            env.pop('PYTHONPATH', None)
            if argv[:3] == [interpreter, '-m', 'yeoman_gateway']:
                env['PYTHONPATH'] = ':'.join(str(source / 'packages' / p) for p in ('gateway', 'shared', 'overseer'))
            return subprocess.run(argv, cwd=source, env=env, capture_output=True, text=True, check=False)
        return runner(argv)

    def execute(action: str, payload: dict) -> dict:
        if _path(payload['home']).resolve() != copy_home:
            raise ValueError('rehearsal_home_mismatch')
        record = payload['record']
        if 'rehearsal_root' in record and _path(record['rehearsal_root']).resolve() != root:
            raise ValueError('rehearsal_root_mismatch')
        paths = list(record.get('layout', {}).values())
        paths.extend(record[k] for k in ('output', 'receipts') if k in record)
        for value in paths:
            path = _path(value)
            preflight_isolated_paths(path)
            require_isolated_paths(path)
            if path.resolve() != root and root not in path.resolve().parents:
                raise ValueError('rehearsal_layout_outside_root')
        if action in SERVICE_ACTIONS:
            timers = list(inventory.get('timers', []))
            associations = inventory.get('timer_services') or {}
            associated = [service for timer in timers for service in associations.get(timer, [])]
            units = timers if action == 'start-timers' else [u['name'] for u in inventory.get('units', [])]
            result = dict(ok=True, simulated=True, units=units)
            if action == 'stop-overseer-clean':
                result.update(clean=True, alert_fired=False, alert_state='inactive', alert_quiet=True)
            if action == 'stop-timers-and-manual-routes':
                result.update(strictly_inactive=True, timer_services=sorted(set(associated)))
            if action in ('suppress-restarts', 'verify-restart-suppression', 'verify-deploy-suppression', 'verify-no-writer-after-deploy'):
                result['suppressed'] = True
            if action in ('verify-quiescent', 'verify-no-writer-after-deploy'):
                result.update(writers_absent=True, bridge_stopped=True)
            if action in ('fence-effects', 'release-fence'):
                # Rehearsal authenticates the same pause member from the isolated copy.
                facts = _pause_facts(inventory['pause_path'])
                result.update(fenced=True, prior_pauses_preserved=True,
                              pause_baseline_sha256=facts['sha256'],
                              prior_pauses_sha256=facts['sha256'])
        elif action in ('all-committed-barrier', 'drain-durable-tails', 'health'):
            # Rehearsal status snapshots must be acquired members, never host queries.
            status_paths = [_path(str(copy_home / inventory[k])) for k in ('raw_status_path', 'bridge_status_path')]
            if any(copy_home not in p.resolve().parents for p in status_paths):
                raise ValueError('rehearsal_status_outside_copy')
            raw, bridge = (json.loads(p.read_bytes()) for p in status_paths)
            if payload.get('operation') == 'restore':
                return dict(_restore_health(payload, local, raw, bridge), mode='rehearsal')
            result = _boundary(payload)
            writer = _raw_writer(raw)
            result.update(raw_deferred=writer['spooled'] + writer['pending_in_memory'], bridge_pending=bridge['outbox']['pending'], bridge_inflight=bridge['queue']['inflight'], capture_ready=_capture_ready(payload))
            result['ok'] = result['all_committed'] and result['capture_ready'] and writer['state'] == 'ok' and result['raw_deferred'] == result['bridge_pending'] == result['bridge_inflight'] == 0
        elif action == 'verify-effect-deduplication':
            result = _effects(payload, local)
        elif action == 'configure-retirement' or action in SELECT_ACTIONS:
            result = _configure(payload, local)
        elif action == 'apply-prepared-texts':
            result = _apply_texts(payload, local, copy_home=copy_home)
        elif action in ('capture-suppression-delta', 'reapply-suppression-delta', 'verify-current-denials'):
            result = _suppression_delta(action, payload, local)
        elif action == 'validate-capture-handover':
            result = dict(ok=_capture_ready(payload), capture_ready=_capture_ready(payload))
        elif action in ('preflight', 'verify-import-origins', 'restore-software-install-config-units'):
            result = dict(ok=True, simulated=True, imports_verified=True)
        elif action == 'capture-frozen-baseline':
            result = _capture_frozen_baseline(payload)
        elif action == 'frozen-watermarks':
            result = _verify_frozen_baseline(payload)
        elif action in COMMAND_ACTIONS:
            # Same argv, same interpreter and the same result interpretation as live;
            # only the pinned home and source differ, so a refusal is reproducible.
            result = _command_result(action, payload, run(payload['argv'], payload))
        elif action == 'prepare-input-bundle':
            result = _prepare_inputs(payload, local)
        elif action in ACK_ACTIONS:
            result = _ack(action, payload)
        else:
            raise ValueError('unknown_host_action')
        return dict(result, mode='rehearsal')
    execute.mode = 'rehearsal'
    return execute
