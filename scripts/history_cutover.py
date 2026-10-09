#!/usr/bin/env python3
"""Pinned cutover/restore operator. Host execution requires explicit live or rehearsal controls.

The coordinator injects a local executor with injected_controls. It receives an
ordered action, pinned argv (where applicable), prior receipts and selection.
It must perform the action, returning durable proof; an absent/failed proof stops
execution without retry. Offline reader smokes must use real selected adapters,
not a running Gateway. CLI defaults to a read-only plan.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing, contextmanager
from contextvars import ContextVar
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from yeoman_gateway.history.attestations import validate_owner_package
from yeoman_gateway.history.layer1 import canonical_json
from yeoman_gateway.history.live import HistoryBoundary
from yeoman_gateway.history.reader import HistoryReader
from yeoman_shared.raw_archive.records import enumerate_committed

READER_ORDER = ('knowledge', 'whatsapp', 'responder', 'tools', 'participation', 'secondary')
_CONTROLS: ContextVar[Callable[[str, dict[str, Any]], dict[str, Any]] | None] = ContextVar('cutover_controls', default=None)
_REF = re.compile(r'(?:whatsapp|backfill|derived|owner)/[^/\\#\s]+\.jsonl#[1-9][0-9]*(?:/(?:0|[1-9][0-9]*))?')
_PRESERVED = frozenset(('raw', 'spool', 'outbox', 'disposition'))


def record_digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json({k: v for k, v in value.items() if k != 'digest'}).encode()).hexdigest()


@contextmanager
def injected_controls(control: Callable[[str, dict[str, Any]], dict[str, Any]]):
    token = _CONTROLS.set(control)
    try:
        yield
    finally:
        _CONTROLS.reset(token)


def _paths(*paths: Path) -> None:
    for path in paths:
        if not path.is_absolute():
            raise ValueError('absolute_paths_required')
        for candidate in (path, *(Path(str(path) + s) for s in ('-wal', '-shm', '.lock'))):
            if any(p.is_symlink() for p in (candidate, *candidate.parents)):
                raise ValueError('symlink_refused')


def _member(home: Path, entry: Mapping[str, Any]) -> Path:
    relative = Path(entry['path'])
    if relative.is_absolute() or '..' in relative.parts or not relative.parts:
        raise ValueError('invalid_inventory_member')
    path = home / relative
    _paths(path)
    if entry['kind'] not in ('file', 'tree', 'sqlite') or type(entry['restore']) is not bool:
        raise ValueError('invalid_inventory_member')
    return path


def _private_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(canonical_json(value) + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    _fsync_dir(path.parent)


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _static_copy(source: Path, target: Path) -> str:
    before = source.stat()
    digest = _hash(source)
    target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    with source.open('rb') as src, target.open('xb') as dst:
        os.chmod(target, 0o600)
        shutil.copyfileobj(src, dst)
        dst.flush()
        os.fsync(dst.fileno())
    after = source.stat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns) or _hash(target) != digest or _hash(source) != digest:
        raise ValueError('acquisition_changed')
    _fsync_dir(target.parent)
    return digest


def acquire_cutover_snapshot(*, home: Path, output: Path,
                             inventory: Mapping[str, Any]) -> dict[str, Any]:
    """Caller proves writers stopped. WAL-aware SQLite and re-stat static copies."""
    _paths(home, output)
    if output.exists() or output == home or home in output.parents:
        raise ValueError('fresh_external_snapshot_required')
    bridge = inventory.get('bridge', {})
    if bridge.get('mode') != 'stopped':
        # No coherent multi-file accepting-Bridge API currently exists.
        raise ValueError('bridge_coherent_acquisition_unproven')
    members = inventory['members']
    if len({m['path'] for m in members}) != len(members) or any(Path(m['path']).is_absolute() or '..' in Path(m['path']).parts for m in members):
        raise ValueError('duplicate_inventory_member')
    for entry in members:
        _member(home, entry)
    paths = [Path(m['source']) if m.get('source') else _member(home, m) for m in members]
    _paths(*paths)
    for path in paths:
        if path.is_dir():
            for root, dirs, files in os.walk(path, followlinks=False):
                _paths(*(Path(root) / n for n in (*dirs, *files)))
    output.mkdir(parents=True, mode=0o700)
    receipt: dict[str, Any] = dict(version=1, ok=True, home=str(home), inventory_digest=record_digest(inventory), members=[], sources=[])
    for entry, source in zip(members, paths, strict=True):
        target = output / entry['path']
        info = dict(entry, exists=source.exists(), started_ns=time.time_ns())
        if not source.exists():
            if not entry.get('optional', False):
                raise ValueError('missing_inventory_member')
        elif entry['kind'] == 'sqlite':
            target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as src, closing(sqlite3.connect(target)) as dst:
                src.backup(dst)
                dst.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                if dst.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or dst.execute('PRAGMA foreign_key_check').fetchall():
                    raise ValueError('snapshot_integrity_failed')
                tables = [r[0] for r in dst.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
                info['counts'] = {t: dst.execute('SELECT count(*) FROM "' + t.replace('"', '""') + '"').fetchone()[0] for t in tables}
                info['integrity'] = True
            os.chmod(target, 0o600)
            with target.open('rb') as stream:
                os.fsync(stream.fileno())
            info['sha256'] = _hash(target)
        elif entry['kind'] == 'tree':
            target.mkdir(parents=True, mode=0o700)
            names = sorted(p.relative_to(source).as_posix() for p in source.rglob('*') if p.is_file())
            info['files'] = {name: _static_copy(source / name, target / name) for name in names}
            if names != sorted(p.relative_to(source).as_posix() for p in source.rglob('*') if p.is_file()):
                raise ValueError('acquisition_tree_changed')
            info['sha256'] = record_digest(info['files'])
        else:
            info['sha256'] = _static_copy(source, target)
        info['ended_ns'] = time.time_ns()
        receipt['members'].append(info)
    if inventory.get('raw_path'):
        raw = _member(output, dict(path=inventory['raw_path'], kind='tree', restore=False))
        receipt['sources'] = [asdict(s) for s in enumerate_committed(raw)]
    receipt['digest'] = record_digest(receipt)
    _private_json(output / 'manifest.json', receipt)
    return receipt


def prepare_final_owner_package(*, conversion_manifest: Mapping[str, Any],
                                reviewed_decisions: Sequence[Mapping[str, Any]], raw_root: Path,
                                output: Path) -> dict[str, Any]:
    """Rebind reviewed exact originals to the completed import, never old offsets."""
    _paths(raw_root, output)
    if output == raw_root or raw_root in output.parents:
        raise ValueError('owner_package_must_be_outside_layer1')
    receipt = conversion_manifest['import_receipt']
    if receipt.get('status') != 'complete' or receipt.get('package_digest') != conversion_manifest['package_digest']:
        raise ValueError('complete_final_import_required')
    candidates = [r for f in conversion_manifest['files'].values() for r in f['rows']]
    records = []
    for decision in reviewed_decisions:
        if decision.get('case') in (12023, 12024):
            if not decision.get('skipped') or 'record' in decision:
                raise ValueError('dropped_case_cannot_have_author')
            continue
        record = dict(decision['record'])
        locator = decision.get('locator')
        if locator is None:
            if record['type'] in ('author', 'message_author'):
                raise ValueError('original_locator_required')
            records.append(record)
            continue
        keys = ('source_ref', 'sha256') if 'source_ref' in locator else ('uuid', 'original_row_sha256')
        if any(not locator.get(k) for k in keys):
            raise ValueError('original_locator_required')
        matches = [r for r in candidates if all(r.get(k) == locator[k] for k in keys)]
        native_ref = locator.get('source_ref', '')
        if native_ref and _REF.fullmatch(native_ref) is None:
            raise ValueError('invalid_original_ref')
        if not matches and native_ref.startswith('whatsapp/') and 'sha256' in locator:
            matches = [dict(source_ref=native_ref, sha256=locator['sha256'], native=True)]
        if not matches or (len(matches) != 1 and not locator.get('all_copies')):
            raise ValueError('missing_or_nonunique_original_locator')
        for row in matches:
            staged_ref = row['source_ref']
            ref = staged_ref if row.get('native') else receipt['ref_map'].get(staged_ref)
            if ref is None:
                raise ValueError('missing_final_ref')
            if _REF.fullmatch(ref) is None:
                raise ValueError('invalid_final_ref')
            base, number = ref.split('#')
            path = raw_root / base
            _paths(path)
            blob = path.read_bytes().splitlines(keepends=True)[int(number)-1]
            expected_hash = row['sha256'] if row.get('native') else receipt['files'][base]['row_hashes'][int(staged_ref.split('#')[1])-1]
            if hashlib.sha256(blob).hexdigest() != expected_hash:
                raise ValueError('final_original_changed')
            actual = json.loads(blob)
            if 'uuid' in locator and (actual.get('origin', {}).get('row_sha256') != locator['original_row_sha256'] or actual.get('original', {}).get('uuid', actual.get('original', {}).get('id')) != locator['uuid']):
                raise ValueError('final_original_changed')
            if 'segment' in locator:
                segments = actual.get('payload', {}).get('segments', [])
                index = locator['segment']
                if (type(index) is not int or not 0 <= index < len(segments) or not locator.get('native_id')
                        or segments[index].get('messageId') != locator['native_id']
                        or actual['payload'].get('messageId') != locator['native_id']
                        or sum(s.get('messageId') == locator['native_id'] for s in segments) != 1):
                    raise ValueError('native_owning_segment_required')
                ref = receipt['ref_map'].get(staged_ref + '/' + str(index))
                if ref is None:
                    raise ValueError('missing_final_segment_ref')
            records.append({**record, 'source_ref': ref})
    # Bindings must exist before dependent merges; preserve order within each kind.
    priority = {'contact': 0, 'identifier': 1, 'identifier_ended': 1}
    records.sort(key=lambda r: priority.get(r['type'], 2))
    validate_owner_package(raw_root, records)
    output.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        for record in records:
            stream.write(canonical_json(record) + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    _fsync_dir(output.parent)
    return dict(ok=True, records=len(records), digest=hashlib.sha256(canonical_json(records).encode()).hexdigest())



def offline_reader_smoke(*, family: str, db_path: Path, raw_root: Path,
                         selection: Mapping[str, Any],
                         probe: Callable[[Any], Mapping[str, bool]]) -> dict[str, Any]:
    """A stopped-process probe borrows one verified ordinary reader lease.

    The injected family composition must exercise its actual adapters and prove
    these checks. Neither a Gateway lifecycle nor a provider/extractor is run.
    """
    checks = {
        'knowledge': ('mapped_source', 'curated_disclosure'),
        'whatsapp': ('reply_identity', 'canonical_mentions'),
        'responder': ('recent_window', 'operational_new'),
        'tools': ('fts', 'media', 'unauthorized_denial'),
        'participation': ('ambient', 'audience_generation'),
        'secondary': ('bounded_owner_export', 'in_turn_subprocess_refusal'),
    }
    if family not in checks or not selection['readers'].get(family):
        raise ValueError('reader_unselected')
    _paths(db_path, raw_root)
    vector = enumerate_committed(raw_root)
    with closing(sqlite3.connect(db_path.as_uri() + '?mode=ro', uri=True)) as db:
        runtime = json.loads(db.execute("SELECT state_json FROM projector_state WHERE file='@runtime'").fetchone()[0])
        checkpoints = {r[0]: tuple(r[1:]) for r in db.execute("SELECT file,lines,end_offset,sha256 FROM projector_state WHERE file<>'@runtime'")}
        if runtime.get('status') != 'ready' or any(checkpoints.get(s.relative_path) != (s.line_number, s.end_offset, s.prefix_sha256) for s in vector):
            raise ValueError('offline_generation_vector_unverified')
    reader = HistoryReader(db_path)
    snapshot = reader.open_snapshot(HistoryBoundary(runtime['generation'], vector))
    try:
        proof = probe(snapshot)
        if any(proof.get(k) is not True for k in (*checks[family], 'unselected_refused')):
            raise ValueError('family_composition_smoke_failed')
        if not snapshot._closed:
            snapshot.assert_current(runtime['generation'])
    finally:
        snapshot.close()
        reader.close()
    return dict(ok=True, adapter=True, lease_closed=snapshot._closed, unselected_refused=True,
                config_digest=record_digest(selection), generation=runtime['generation'],
                sources=[asdict(s) for s in vector])


def preparation_controls(*, host: Callable[[str, dict[str, Any]], dict[str, Any]],
                         probes: Mapping[str, Callable[[Any], Mapping[str, bool]]] | None = None,
                         decode: Any = None) -> Callable[[str, dict[str, Any]], dict[str, Any]]:
    """Existing data APIs plus explicit host controls; no default host subprocess.

    record.layout pins absolute staged/raw/history/verification/Knowledge/package
    paths. Host executes protected import/attest CLI and owns publication/config,
    curation, effects and unit proofs. It returns complete durable CLI receipts.
    """
    from argparse import Namespace

    from yeoman_gateway.history.convert.run import prepare_import_manifest, run_conversion
    from yeoman_gateway.history.project import project
    from yeoman_gateway.history.verify import verify

    try:
        from scripts.prepare_history_cutover import prepare
    except ModuleNotFoundError:
        from prepare_history_cutover import prepare

    def execute(action: str, payload: dict[str, Any]) -> dict[str, Any]:
        value = payload['record']
        layout = {k: Path(v) for k, v in value['layout'].items()}
        _paths(*layout.values())
        snapshot_home = Path(value['output'])
        if action == 'convert':
            result = run_conversion(snapshot_home, layout['staged'], decode=decode,
                extra_bridge_dirs=tuple(snapshot_home / p for p in value['inventory'].get('extra_bridge_dirs', [])))
            manifest = prepare_import_manifest(layout['staged'])
            _private_json(layout['conversion_manifest'], manifest)
            return dict(ok=True, complete=True, files=len(result['files']), manifest_digest=manifest['package_digest'])
        if action == 'prepare-final-owner':
            manifest = json.loads(layout['conversion_manifest'].read_bytes())
            manifest['import_receipt'] = next(p['receipt'] for p in payload['receipts'] if p['action'] == 'import')
            result = prepare_final_owner_package(conversion_manifest=manifest,
                reviewed_decisions=json.loads(layout['reviewed_decisions'].read_bytes()),
                raw_root=layout['raw'], output=layout['owner_package'])
            return dict(result, complete=True)
        if action == 'project-with-lineage':
            result = project([layout['raw']], layout['history'], publish_lineage_root=layout['raw'])
            return dict(ok=True, complete=True, project=result)
        if action == 'freeze-verification-prefix':
            result = acquire_cutover_snapshot(home=layout['raw'].parent, output=layout['verification_home'],
                inventory=dict(bridge=dict(mode='stopped'), members=[dict(path=layout['raw'].name, kind='tree', restore=False)], raw_path=layout['raw'].name))
            return dict(result, complete=True)
        if action == 'verify-history-twice':
            frozen_raw = layout['verification_home'] / layout['raw'].name
            result = verify([frozen_raw], layout['history'], scratch=layout['verify_scratch'], frozen=True)
            _private_json(layout['verification_report'], result)
            coverage = result['coverage']
            if (not result.get('accounting_ok') or not result.get('deterministic')
                    or (coverage['with_contact'] / coverage['messages_with_identifier'] if coverage['messages_with_identifier'] else 1) < .95):
                raise ValueError('history_verification_failed')
            live = verify([layout['raw']], layout['history'], scratch=None, frozen=False)
            if live['digests'] != result['digests']:
                raise ValueError('live_history_changed')
            return dict(ok=True, complete=True, coverage={k: coverage[k] for k in ('messages_with_identifier', 'with_contact', 'with_confirmed_contact', 'confirmed_ratio', 'meets_95_percent')}, digests=result['digests'])
        if action == 'prepare-v3':
            bundle = json.loads((layout['preparation_home'] / 'cutover-inputs.json').read_bytes())
            acquisition = next(p['receipt'] for p in payload['receipts'] if p['action'] == 'acquire')
            conversion = next(p['receipt'] for p in payload['receipts'] if p['action'] == 'convert')
            if (bundle['snapshot_digest'] != acquisition['digest']
                    or bundle['conversion_digest'] != conversion['manifest_digest']
                    or snapshot_home not in layout['knowledge_source'].parents):
                raise ValueError('fresh_final_preparation_inputs_required')
            return dict(prepare(Namespace(snapshot_home=layout['preparation_home'], history_db=layout['history'],
                knowledge_source=layout['knowledge_source'], knowledge_target=layout['knowledge_target'],
                policy_snapshot=layout['policy_snapshot'], output_root=layout['alias_output'])), complete=True)
        if action == 'publish-v3':
            source, target = layout['knowledge_target'], layout['knowledge_live']
            with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as db:
                version = db.execute("SELECT value FROM knowledge_meta WHERE key='schema_version'").fetchone()
                handover = db.execute("SELECT value_json FROM knowledge_history_capture_state WHERE key='handover'").fetchone()
                if not version or version[0] != '3' or not handover or json.loads(handover[0]).get('version') != 1:
                    raise ValueError('verified_v3_handover_required')
            staging = target.with_name(target.name + '.cutover-new')
            _paths(staging)
            digest = _static_copy(source, staging)
            for suffix in ('-wal', '-shm'):
                Path(str(target) + suffix).unlink(missing_ok=True)
            os.replace(staging, target)
            _fsync_dir(target.parent)
            return dict(ok=True, complete=True, sha256=digest, handover=True)
        if action.startswith('smoke-reader-') and probes and action.removeprefix('smoke-reader-') in probes:
            family = action.removeprefix('smoke-reader-')
            return offline_reader_smoke(family=family, db_path=layout['history'], raw_root=layout['raw'],
                selection=payload['selection'], probe=probes[family])
        return host(action, payload)
    execute.mode = getattr(host, "mode", None)
    return execute


def check_window_timing(inventory: Mapping[str, Any]) -> None:
    """Inventory supplies local cron danger minutes; this never edits crontab."""
    cron = inventory['host_crontab']
    if cron.get('window_safe') is not True:
        raise ValueError('unsafe_host_crontab_window')
    zone = ZoneInfo(cron['timezone'])
    start = datetime.fromtimestamp(cron['window_start_ms']/1000, zone)
    end = datetime.fromtimestamp(cron['window_end_ms']/1000, zone)
    if end <= start or end-start > timedelta(days=7):
        raise ValueError('invalid_host_crontab_window')
    minutes = cron['danger_minutes']
    if not isinstance(minutes, list) or any(type(m) is not int or not 0 <= m < 1440 for m in minutes):
        raise ValueError('invalid_host_crontab_window')
    day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    while day <= end:
        if any(start <= day + timedelta(minutes=m) <= end for m in minutes):
            raise ValueError('unsafe_host_crontab_window')
        day += timedelta(days=1)

def _load(record: Path, home: Path, *, apply: bool) -> dict[str, Any]:
    _paths(record, home)
    value = json.loads(record.read_bytes())
    if value.get('version') != 1 or value.get('home') != str(home) or value.get('digest') != record_digest(value):
        raise ValueError('record_pin_mismatch')
    if apply and (value.get('approved') is not True or not value.get('approval') or _CONTROLS.get() is None):
        raise ValueError('approved_record_and_local_controls_required')
    mode = value.get('mode')
    if mode not in ('live', 'rehearsal'):
        raise ValueError('explicit_record_mode_required')
    if mode == 'rehearsal':
        try:
            from scripts.history_maintenance_guard import preflight_isolated_paths
        except ModuleNotFoundError:
            from history_maintenance_guard import preflight_isolated_paths
        isolated = [home, Path(value['output']), Path(value['receipts'])]
        isolated.extend(Path(v) for v in value.get('layout', {}).values())
        isolated.extend(Path(m['source']) for m in value['inventory']['members'] if 'source' in m)
        preflight_isolated_paths(*isolated)
    controls = _CONTROLS.get()
    if apply and getattr(controls, 'mode', None) != mode:
        raise ValueError('control_record_mode_mismatch')
    if apply and mode == 'live':
        confirmation = value.get('confirmation_token')
        if not confirmation:
            raise ValueError('live_confirmation_token_required')
        token = Path(confirmation)
        _paths(token)
        if not token.is_file() or token.read_text().strip() != value['digest']:
            raise ValueError('live_confirmation_token_required')
    expected_jobs = value['inventory'].get('expected_gateway_jobs')
    if type(expected_jobs) is not int or expected_jobs < 0 or value['inventory'].get('gateway_jobs') != expected_jobs:
        raise ValueError('cron_inventory_drift')
    check_window_timing(value['inventory'])
    _paths(Path(value['output']), Path(value['receipts']))
    return value


def command_for(action: str, record: Mapping[str, Any]) -> list[str]:
    """Exact argv, without a shell. Host-only controls are supplied by inventory."""
    commands = record.get('commands', {})
    command = commands.get(action)
    if command is None and action in ('import-preview', 'import', 'owner-preview', 'owner-append') and 'layout' in record:
        layout = record['layout']
        prefix = [record['python'], '-m', 'yeoman_gateway', 'history']
        if action.startswith('import'):
            command = [*prefix, 'import-backfill', '--staged', layout['staged'], '--manifest', layout['conversion_manifest'], '--dry-run' if action.endswith('preview') else '--confirm']
        else:
            command = [*prefix, 'attest', '--file', layout['owner_package'], '--dry-run' if action.endswith('preview') else '--confirm']
    if command is None:
        command = []
    if not isinstance(command, list) or any(not isinstance(a, str) or not a for a in command):
        raise ValueError('invalid_command_argv')
    return command


def _sequence(value: Mapping[str, Any]) -> list[str]:
    return ['preflight', 'fence-effects', 'stop-overseer-clean', 'stop-timers-and-manual-routes',
            'stop-gateway', 'suppress-restarts', 'stop-bridge', 'verify-restart-suppression',
            'verify-quiescent', 'acquire', 'convert', 'import-preview', 'import',
            'prepare-final-owner', 'owner-preview', 'owner-append', 'project-with-lineage',
            'freeze-verification-prefix', 'verify-history-twice', 'prepare-input-bundle', 'prepare-v3', 'publish-v3',
            'configure-retirement', 'apply-prepared-texts', 'verify-deploy-suppression', 'deploy', 'verify-no-writer-after-deploy',
            *[action for f in READER_ORDER for action in (f'select-{f}', f'smoke-reader-{f}')],
            'validate-capture-handover', 'start-bridge', 'start-gateway', 'health',
            'drain-durable-tails', 'all-committed-barrier', 'verify-effect-deduplication',
            'release-fence', 'functional-smoke', 'frozen-watermarks', 'resume-vetted-manual-routes', 'start-overseer', 'start-timers']


def _execute(action: str, payload: dict[str, Any]) -> dict[str, Any]:
    control = _CONTROLS.get()
    if control is None:
        raise ValueError('local_controls_required')
    result = control(action, payload)
    if not isinstance(result, dict) or result.get('ok') is not True:
        raise ValueError('phase_proof_failed')
    required = {
        'stop-overseer-clean': ('clean',), 'verify-restart-suppression': ('suppressed',),
        'verify-deploy-suppression': ('suppressed',), 'fence-effects': ('fenced', 'prior_pauses_preserved'),
        'verify-quiescent': ('writers_absent', 'bridge_stopped'),
        'release-fence': ('prior_pauses_preserved',),
        'verify-no-writer-after-deploy': ('writers_absent', 'suppressed'),
        'import': ('complete',), 'owner-append': ('complete',),
        'verify-history-twice': ('complete',), 'publish-v3': ('complete',),
        'all-committed-barrier': ('all_committed', 'reopened', 'capture_ready'),
        'reapply-suppression-delta': ('delta_applied',),
        'verify-current-denials': ('current_denials',),
        'verify-effect-deduplication': ('no_duplicate_effects', 'unknown_effects_held'),
        'verify-import-origins': ('imports_verified',),
    }
    if any(result.get(k) is not True for k in required.get(action, ())):
        raise ValueError('phase_proof_incomplete')
    if action == 'stop-overseer-clean' and result.get('alert_fired') is not False:
        raise ValueError('planned_stop_triggered_alert')
    if action == 'all-committed-barrier':
        if any(result.get(k) != 0 for k in ('raw_deferred', 'bridge_pending', 'bridge_inflight')):
            raise ValueError('durable_tail_not_drained')
        if type(result.get('generation')) is not int or result['generation'] < 1:
            raise ValueError('reader_generation_unproven')
        actual = {s['relative_path']: s for s in result.get('sources', [])}
        for source in payload['sources']:
            observed = actual.get(source['relative_path'])
            if observed is None or observed['line_number'] < source['line_number'] or observed['end_offset'] < source['end_offset']:
                raise ValueError('committed_source_not_covered')
            if observed['end_offset'] == source['end_offset'] and observed['prefix_sha256'] != source['prefix_sha256']:
                raise ValueError('committed_prefix_changed')
    if action.startswith('smoke-reader-'):
        if (any(result.get(k) is not True for k in ('adapter', 'lease_closed', 'unselected_refused'))
                or result.get('config_digest') != record_digest(payload['selection'])
                or result.get('sources') != payload['sources'] or type(result.get('generation')) is not int):
            raise ValueError('offline_adapter_smoke_unproven')
    return result


def _run(value: dict[str, Any], home: Path, actions: list[str], *, restore: bool = False,
         failed_snapshot: Path | None = None) -> dict[str, Any]:
    receipt_root = Path(value['receipts'])
    receipt_path = receipt_root / ('restore.json' if restore else 'cutover.json')
    if receipt_path.exists() or any(receipt_root.glob(('restore' if restore else 'cutover') + '-*.json')):
        raise ValueError('attempt_already_recorded_no_retry')
    receipt_root.mkdir(parents=True, mode=0o700, exist_ok=True)
    journal: dict[str, Any] = dict(version=1, mode=value['mode'], record_digest=value['digest'], phases=[], ok=False, fenced=True)
    selection: dict[str, Any] = dict(legacyWritersDisabled=True, liveProjectionEnabled=True, readers=dict.fromkeys(READER_ORDER, False))
    sources = []
    generation = None
    try:
        for action in actions:
            started = time.time_ns()
            phase: dict[str, Any] = dict(action=action, started_ns=started)
            journal['phases'].append(phase)
            _private_json(receipt_root / f'{"restore" if restore else "cutover"}-{len(journal["phases"]):02}-started.json', phase)
            if action.startswith('select-'):
                selection['readers'][action.removeprefix('select-')] = True
            payload = dict(record=value, home=str(home), argv=command_for(action, value),
                           selection=json.loads(canonical_json(selection)), sources=sources, receipts=journal['phases'][:-1])
            if action == 'acquire':
                result = acquire_cutover_snapshot(home=home, output=Path(value['output']), inventory=value['inventory'])
                sources = result['sources']
            elif action == 'acquire-failed-state':
                result = acquire_cutover_snapshot(home=home, output=failed_snapshot, inventory=value['inventory'])
            elif action == 'restore-files':
                result = _restore_files(value, home)
            else:
                result = _execute(action, payload)
            if action.startswith('smoke-reader-'):
                if generation is not None and result['generation'] != generation:
                    raise ValueError('offline_reader_generation_changed')
                generation = result['generation']
            if action == 'freeze-verification-prefix' and 'sources' in result:
                sources = result['sources']
            phase.update(ended_ns=time.time_ns(), receipt=result)
            # Persist each receipt before the next phase, including release proof.
            _private_json(receipt_root / f'{"restore" if restore else "cutover"}-{len(journal["phases"]):02}.json', phase)
            if action == 'release-fence':
                journal['fenced'] = False
        journal['ok'] = True
    except Exception:
        journal['failed_phase'] = action
        journal['fenced'] = True
        if not any(p['action'] == 'fence-effects' and 'receipt' in p for p in journal['phases']):
            journal['fence_unverified'] = True
        # A post-release failure must re-establish the same fence, never resume.
        if any(p['action'] == 'release-fence' and 'receipt' in p for p in journal['phases']):
            try:
                journal['refence'] = _execute('fence-effects', dict(record=value, home=str(home), argv=command_for('fence-effects', value)))
            except Exception:
                journal['fence_unverified'] = True
        journal['phases'][-1]['failed'] = True
    _private_json(receipt_path, journal)
    return dict(ok=journal['ok'], fenced=journal['fenced'], receipt=str(receipt_path), failed_phase=journal.get('failed_phase'), fence_verified=not journal.get('fence_unverified', False))


def run_cutover(*, record: Path, home: Path, apply: bool = False) -> dict[str, Any]:
    value = _load(record, home, apply=apply)
    actions = _sequence(value)
    if not apply:
        return dict(ok=True, planned=True, actions=[dict(action=a, argv=command_for(a, value)) for a in actions])
    return _run(value, home, actions)


def _restore_files(value: Mapping[str, Any], home: Path) -> dict[str, Any]:
    snapshot = Path(value['output'])
    manifest = json.loads((snapshot / 'manifest.json').read_bytes())
    if manifest.get('digest') != record_digest(manifest) or manifest['home'] != str(home) or manifest.get('inventory_digest') != record_digest(value['inventory']):
        raise ValueError('prior_snapshot_pin_mismatch')
    # Verify the entire prior set before replacing its first member.
    preserved = [Path(e['source']) if e.get('source') else home / e['path'] for e in manifest['members'] if not e['restore']]
    for entry in manifest['members']:
        candidate = Path(entry['source']) if entry.get('source') else home / entry['path']
        if entry['restore'] and any(candidate == p or candidate in p.parents or p in candidate.parents for p in preserved):
            raise ValueError('restore_overlaps_preserved_tail')
    for entry in manifest['members']:
        if not entry['restore']:
            continue
        if entry.get('role') in _PRESERVED or Path(entry['path']).parts[0] in _PRESERVED:
            raise ValueError('append_only_restore_refused')
        source = _member(snapshot, entry)
        target = Path(entry['source']) if entry.get('source') else _member(home, entry)
        _paths(target)
        if entry['exists']:
            if entry['kind'] == 'tree':
                actual = {p.relative_to(source).as_posix(): _hash(p) for p in source.rglob('*') if p.is_file()}
                if actual != entry['files']:
                    raise ValueError('prior_snapshot_changed')
            elif _hash(source) != entry['sha256']:
                raise ValueError('prior_snapshot_changed')
    for entry in manifest['members']:
        if not entry['restore']:
            continue
        source = _member(snapshot, entry)
        target = Path(entry['source']) if entry.get('source') else _member(home, entry)
        if not entry['exists']:
            if target.exists():
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink()
            continue
        staging = target.with_name(target.name + '.restore-staging')
        _paths(staging)
        if staging.exists():
            raise ValueError('occupied_restore_staging')
        if entry['kind'] == 'tree':
            staging.mkdir(parents=True, mode=0o700)
            for name in entry['files']:
                _static_copy(source / name, staging / name)
            if target.exists():
                shutil.rmtree(target)
        else:
            _static_copy(source, staging)
            # Stopped old SQLite sidecars cannot belong to the replacement inode.
            if entry['kind'] == 'sqlite':
                for suffix in ('-wal', '-shm'):
                    Path(str(target) + suffix).unlink(missing_ok=True)
        os.replace(staging, target)
        _fsync_dir(target.parent)
    return dict(ok=True, complete=True)


def restore_prior_set(*, record: Path, home: Path, failed_snapshot: Path,
                      apply: bool = False) -> dict[str, Any]:
    value = _load(record, home, apply=apply)
    _paths(failed_snapshot)
    if value['mode'] == 'rehearsal':
        try:
            from scripts.history_maintenance_guard import preflight_isolated_paths
        except ModuleNotFoundError:
            from history_maintenance_guard import preflight_isolated_paths
        preflight_isolated_paths(failed_snapshot)
    actions = ['fence-effects', 'stop-overseer-clean', 'stop-timers-and-manual-routes',
               'stop-gateway', 'suppress-restarts', 'stop-bridge', 'verify-restart-suppression',
               'verify-quiescent', 'acquire-failed-state', 'capture-suppression-delta',
               'restore-files', 'restore-software-install-config-units', 'reapply-suppression-delta',
               'verify-current-denials', 'verify-effect-deduplication', 'verify-import-origins',
               'start-bridge', 'start-gateway', 'health', 'release-fence', 'resume-vetted-manual-routes', 'start-overseer', 'start-timers']
    if not apply:
        return dict(ok=True, planned=True, actions=[dict(action=a, argv=command_for(a, value)) for a in actions])
    return _run(value, home, actions, restore=True, failed_snapshot=failed_snapshot)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('cutover', 'restore'))
    parser.add_argument('--record', type=Path, required=True)
    parser.add_argument('--home', type=Path, required=True)
    parser.add_argument('--failed-snapshot', type=Path)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--controls', choices=('rehearsal', 'live'))
    args = parser.parse_args()
    try:
        if args.controls:
            try:
                from scripts.history_cutover_host import live_host_controls, rehearsal_host_controls
                from scripts.history_cutover_probes import build_probes
            except ModuleNotFoundError:
                from history_cutover_host import live_host_controls, rehearsal_host_controls
                from history_cutover_probes import build_probes
            value = _load(args.record, args.home, apply=False)
            if value['mode'] != args.controls:
                raise ValueError('control_record_mode_mismatch')
            host = (live_host_controls(inventory=value['inventory']) if args.controls == 'live'
                    else rehearsal_host_controls(copy_home=args.home, inventory=value['inventory']))
            controls = preparation_controls(host=host,probes=build_probes(record=value,home=args.home))
        else:
            controls = None
        with injected_controls(controls):
            result = _cli_run(args)
        print(canonical_json(dict(ok=result['ok'], planned=result.get('planned', False), fenced=result.get('fenced', False), fence_verified=result.get('fence_verified', False), phases=len(result.get('actions', [])))))
        return 0 if result['ok'] else 1
    except Exception:
        print('{"ok":false,"error":"cutover_refused"}')
        return 1


def _cli_run(args):
    if args.operation == 'restore':
        if args.failed_snapshot is None:
            raise ValueError('failed_snapshot_required')
        return restore_prior_set(record=args.record, home=args.home, failed_snapshot=args.failed_snapshot, apply=args.apply)
    return run_cutover(record=args.record, home=args.home, apply=args.apply)


if __name__ == '__main__':
    raise SystemExit(main())
