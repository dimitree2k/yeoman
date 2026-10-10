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
    import_backfill,
    preview_import,
)

try:
    from scripts.history_maintenance_guard import preflight_isolated_paths
except ModuleNotFoundError:
    from history_maintenance_guard import preflight_isolated_paths

SHOW = '--property=ActiveState,Result,UnitFileState,Restart,ActiveEnterTimestampMonotonic,InactiveEnterTimestampMonotonic,ExecMainStartTimestampMonotonic,ExecMainExitTimestampMonotonic'
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


def gateway_socket_client(request: dict, *, socket_path: Path | None = None) -> dict:
    from yeoman_shared.config.loader import load_config
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
        stream.settimeout(360)
        stream.connect(str(socket_path or Path(load_config().ipc.gateway_socket_path).expanduser()))
        stream.sendall(json.dumps(request).encode() + b'\n')
        with stream.makefile('rb') as reader:
            line = reader.readline(1024 * 1024 + 1)
        if not line.endswith(b'\n') or len(line) > 1024 * 1024:
            raise ValueError('invalid_ipc_response')
        return json.loads(line)


def _bridge_probe(*, config_path: Path | None = None) -> dict:
    from yeoman_gateway.channels.whatsapp_runtime import WhatsAppRuntimeManager
    from yeoman_shared.config.loader import load_config
    config = load_config(config_path=config_path)
    # The existing probe generates a token if absent. Refuse that config mutation.
    if not config.channels.whatsapp.bridge_token:
        raise ValueError('bridge_token_required')
    return asyncio.run(WhatsAppRuntimeManager(config=config)._health_check_async(30))


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


def _read_db(path: Path):
    _path(str(path))
    for suffix in ('-wal', '-shm'):
        _path(str(path) + suffix)
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


def _ack(action: str, payload: dict) -> dict:
    root = _path(payload['record']['receipts'])
    path = _path(str(root / f'{action}.owner_ack.json'))
    proof = json.loads(path.read_bytes())
    if proof.get('record_digest') != payload['record']['digest'] or proof.get('action') != action or proof.get('owner_ack') is not True:
        raise ValueError('owner_ack_required')
    return dict(ok=True, owner_ack=str(path), **proof.get('proof', {}))


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
    routes = [*inventory.get('timers', []), *inventory.get('manual_routes', []), *[u['name'] for u in units if u.get('role') == 'a2a' or u['name'] == 'yeoman-a2a.service']]
    all_units = list(dict.fromkeys([*names, *routes]))
    if any(not re.fullmatch(r'[A-Za-z0-9_.@-]+\.(service|timer)', u) for u in [*all_units, gateway, bridge, overseer]):
        raise ValueError('invalid_unit_name')
    pause_before: bytes | None = None

    def run(argv, payload, *, source_dir=None):
        if runner is _subprocess:
            source = source_dir or inventory['source_dir']
            env = dict(os.environ, YEOMAN_HOME=payload['home'], YEOMAN_SOURCE_DIR=source)
            env.pop('PYTHONPATH', None)
            if argv[:3] == [payload['record'].get('python'), '-m', 'yeoman_gateway']:
                env['PYTHONPATH'] = ':'.join(str(Path(source) / 'packages' / p) for p in ('gateway', 'shared', 'overseer'))
            return subprocess.run(argv, cwd=source, env=env, capture_output=True, text=True, check=False)
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

    def suppression(payload):
        before = {u: show(u, payload) for u in all_units}
        # Inventory drift cannot leave an unmasked Restart=always writer.
        matches = all((s['Restart'] == 'always') == (u in restart) for u, s in before.items() if u in names)
        clock.sleep(30)
        after = {u: show(u, payload) for u in all_units}
        return matches and all(before[u]['UnitFileState'] == after[u]['UnitFileState'] == 'masked-runtime' and before[u]['ActiveState'] == after[u]['ActiveState'] == 'inactive' for u in restart) and all(s['ActiveState'] == 'inactive' for s in after.values())

    def quiescent(payload):
        absent = stopped(all_units, payload)
        configured = {Path(u['executable']) for u in units}
        if any(not p.is_absolute() for p in configured):
            raise ValueError('absolute_writer_executables_required')
        executables = configured | {p.resolve() for p in configured}
        for entry in proc_root.iterdir():
            if entry.name.isdecimal():
                try:
                    executable = (entry / 'exe').resolve(strict=True)
                    arguments = (entry / 'cmdline').read_bytes().split(b'\0')
                except FileNotFoundError:
                    continue  # Process exited between observations.
                if executable in executables or any(os.fsencode(p) in arguments for p in executables):
                    absent = False
        return dict(writers_absent=absent, bridge_stopped=stopped([bridge], payload))

    def probe_bridge():
        return (_bridge_probe(config_path=_path(inventory['config_path']))
                if bridge_probe is _bridge_probe else bridge_probe())

    def tails(payload):
        if payload.get('operation') == 'restore':
            raw = json.loads(run([payload['record']['python'], '-m', 'yeoman_gateway', 'raw', 'status', '--json'], payload).stdout)
            return _restore_health(payload, inventory, raw, probe_bridge())
        request = {'cmd': 'history_control', 'args': {'operation': 'status'}}
        health = (gateway_socket_client(request, socket_path=_path(inventory['gateway_socket']))
                  if ipc is gateway_socket_client else ipc(request))
        # The response is health-only; persisted vector and a reopened lease supply the rest.
        raw = json.loads(run([payload['record']['python'], '-m', 'yeoman_gateway', 'raw', 'status', '--json'], payload).stdout)
        bridge_health = probe_bridge()
        writer = _raw_writer(raw)
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
            clean = state['ActiveState'] == 'inactive' and state['Result'] == 'success'
            result = dict(ok=clean and not fired, clean=clean, alert_fired=fired)
        elif action in ('stop-timers-and-manual-routes', 'stop-gateway', 'stop-bridge'):
            selected = routes if action == 'stop-timers-and-manual-routes' else [gateway if action == 'stop-gateway' else bridge]
            for u in selected:
                run(['systemctl', '--user', 'stop', u], payload)
            result = dict(ok=stopped(selected, payload), units=selected)
        elif action == 'suppress-restarts':
            for u in restart:
                run(['systemctl', '--user', 'mask', '--runtime', u], payload)
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
            for phase in ('stop-overseer-clean', 'stop-timers-and-manual-routes', 'stop-gateway', 'stop-bridge'):
                if not execute(phase, payload)['ok']:
                    raise ValueError('stop_fence_unproven')
            fenced = stopped(all_units, payload)
            preserved = pause_bytes() == pause_before
            result = dict(ok=fenced and preserved, fenced=fenced, prior_pauses_preserved=preserved, fence='verified-inactive', units=all_units)
        elif action == 'release-fence':
            preserved = pause_before is not None and pause_bytes() == pause_before
            active = all(show(u, payload)['ActiveState'] == 'active' for u in (bridge, gateway))
            result = dict(ok=preserved and active, prior_pauses_preserved=preserved)
        elif action.startswith('start-') or action == 'resume-vetted-manual-routes':
            starts = {'start-bridge': [bridge], 'start-gateway': [gateway], 'start-overseer': [overseer], 'start-timers': inventory.get('timers', []), 'resume-vetted-manual-routes': [u for u in routes if u not in inventory.get('timers', [])]}
            if action not in starts:
                raise ValueError('unknown_host_action')
            selected = starts[action]
            for u in selected:
                run(['systemctl', '--user', 'unmask', '--runtime', u], payload)
                run(['systemctl', '--user', 'start', u], payload)
            result = dict(ok=all(show(u, payload)['ActiveState'] == 'active' for u in selected), units=selected)
        elif action == 'deploy':
            if not suppression(payload):
                raise ValueError('deploy_suppression_unproven')
            deployed = run([inventory['yeoman'], 'deploy'], payload)
            proof = execute('verify-no-writer-after-deploy', payload)
            result = dict(proof, deployed=deployed.returncode == 0)
            result['ok'] &= result['deployed']
        elif action in ('drain-durable-tails', 'all-committed-barrier'):
            result = tails(payload)
        elif action == 'health':
            from yeoman_shared.whatsapp_protocol import PROTOCOL_VERSION
            health = probe_bridge()
            raw = json.loads(run([payload['record']['python'], '-m', 'yeoman_gateway', 'raw', 'status', '--json'], payload).stdout)
            writer = _raw_writer(raw)
            ready = health['whatsapp']['connected'] is True and health['protocolVersion'] == PROTOCOL_VERSION and writer['state'] == 'ok'
            result = dict(ok=ready, connected=health['whatsapp']['connected'], protocol=health['protocolVersion'], bridge_pending=health['outbox']['pending'], bridge_inflight=health['queue']['inflight'], writer_ok=writer['state'] == 'ok')
            if payload.get('operation') == 'restore':
                result = _restore_health(payload, inventory, raw, health)
        elif action == 'verify-effect-deduplication':
            result = _effects(payload, inventory)
        elif action == 'frozen-watermarks':
            watermarks = {str(_path(p)): _hash(_path(p)) for p in inventory['frozen_files']}
            expected = inventory['frozen_watermarks']
            result = dict(ok=watermarks == expected, watermarks=watermarks)
        elif action == 'configure-retirement' or action in SELECT_ACTIONS:
            result = _configure(payload, inventory)
        elif action == 'apply-prepared-texts':
            result = _apply_texts(payload, inventory, copy_home=None)
        elif action == 'verify-import-origins':
            code = "import json,yeoman_gateway,yeoman_shared,yeoman_overseer;print(json.dumps([m.__file__ for m in (yeoman_gateway,yeoman_shared,yeoman_overseer)]))"
            response = run([inventory['tool_python'], '-c', code], payload)
            paths = json.loads(response.stdout)
            verified = len(paths) == 3 and all(_path(inventory['source_dir']) in _path(p).parents for p in paths)
            result = dict(ok=verified, imports_verified=verified, origins=paths)
        elif action == 'validate-capture-handover':
            response = run([payload['record']['python'], '-m', 'yeoman_gateway', 'raw', 'check-capture'], payload)
            ready = _capture_ready(payload)
            result = dict(ok=response.returncode == 0 and ready, capture_ready=ready, capture_check_sha256=hashlib.sha256(response.stdout.encode()).hexdigest())
        elif action == 'preflight':
            pinned = inventory['pinned_files']
            verified = bool(pinned) and all(_hash(_path(p)) == digest for p, digest in pinned.items())
            source = _path(inventory['source_dir'])
            verified &= (source / 'pyproject.toml').is_file()
            result = dict(ok=verified, pins_verified=verified)
        elif action in COMMAND_ACTIONS:
            response = run(payload['argv'], payload)
            result = json.loads(response.stdout)
            if action == 'import':
                result = _import_proof(payload, result)
            elif action == 'import-preview':
                result['ok'] = result.get('status') == 'dry-run'
            else:
                count = len(_path(payload['record']['layout']['owner_package']).read_text().splitlines())
                complete = result.get('validated') == count and result.get('committed', -1) + result.get('suppressed', -1) == (count if action == 'owner-append' else 0)
                result.update(ok=complete, complete=complete)
            result['ok'] &= response.returncode == 0
        elif action == 'prepare-input-bundle':
            result = _prepare_inputs(payload, inventory)
        elif action in ACK_ACTIONS:
            result = _ack(action, payload)
        elif action in ('capture-suppression-delta', 'reapply-suppression-delta', 'verify-current-denials'):
            result = _suppression_delta(action, payload, inventory)
        elif action == 'restore-software-install-config-units':
            # Whole-set restore already copied inventory files, including config/units/text.
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
            delta = dict(sources=db.execute("SELECT event_id,revision,reason FROM knowledge_history_sources WHERE revoked=1").fetchall(),
                         statements=db.execute("SELECT statement_id,status,revoked_at_ms FROM knowledge_statements WHERE revoked_at_ms IS NOT NULL").fetchall())
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


def rehearsal_host_controls(*, copy_home: Path, inventory: Mapping[str, Any], rehearsal_root: Path | None = None, runner=None, **_):
    """Simulate service actions; data proofs come exclusively from the isolated copy."""
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
            units = inventory.get('timers', []) if action == 'start-timers' else [u['name'] for u in inventory.get('units', [])]
            result = dict(ok=True, simulated=True, units=units)
            if action == 'stop-overseer-clean':
                result.update(clean=True, alert_fired=False)
            if action in ('suppress-restarts', 'verify-restart-suppression', 'verify-deploy-suppression', 'verify-no-writer-after-deploy'):
                result['suppressed'] = True
            if action in ('verify-quiescent', 'verify-no-writer-after-deploy'):
                result.update(writers_absent=True, bridge_stopped=True)
            if action in ('fence-effects', 'release-fence'):
                result.update(fenced=True, prior_pauses_preserved=True)
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
        elif action == 'frozen-watermarks':
            paths = [_path(p) for p in inventory['frozen_files']]
            if any(copy_home not in p.parents for p in paths):
                raise ValueError('rehearsal_input_outside_copy')
            watermarks = {str(p): _hash(p) for p in paths}
            result = dict(ok=watermarks == inventory['frozen_watermarks'], watermarks=watermarks)
        elif action in ('import', 'import-preview'):
            layout = payload['record']['layout']
            manifest = json.loads(_path(layout['conversion_manifest']).read_bytes())
            operation = import_backfill if action == 'import' else preview_import
            result = operation(_path(layout['raw']), _path(layout['staged']), manifest)
            result.update(ok=True, complete=action == 'import')
        elif action in ('owner-preview', 'owner-append'):
            from yeoman_gateway.history.attestations import validate_owner_package
            from yeoman_gateway.history.control import _parse_owner_package
            from yeoman_shared.raw_archive.records import (
                PURGE_DISPOSITION_LOCK,
                append_owner_record_locked,
                lock_file,
            )
            layout = payload['record']['layout']
            raw = _path(layout['raw'])
            records = _parse_owner_package(_path(layout['owner_package']).read_bytes())
            validate_owner_package(raw, records)
            if action == 'owner-append':
                fd = lock_file(raw / PURGE_DISPOSITION_LOCK, create=True)
                try:
                    for record in records:
                        append_owner_record_locked(raw, record)
                finally:
                    os.close(fd)
            result = dict(ok=True, complete=True, validated=len(records))
        elif action == 'prepare-input-bundle':
            result = _prepare_inputs(payload, local)
        elif action in ACK_ACTIONS:
            result = _ack(action, payload)
        else:
            raise ValueError('unknown_host_action')
        return dict(result, mode='rehearsal')
    execute.mode = 'rehearsal'
    return execute
