"""Standalone, synthetic-only Pi acceptance runner; not an automatically collected test.

Run with the worktree PYTHONPATH and a fresh directory under the approved cache.
The 20 real-time reconnect bursts take 200 seconds; other schedules are explicitly
compressed correctness replay. No provider, service or runtime-data access.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import platform
import resource
import sqlite3
import subprocess
import threading
from contextlib import closing
from pathlib import Path
from time import perf_counter_ns, thread_time_ns
from typing import Any

from hist_fixtures import T0, _bf, write_jsonl
from test_hist_incremental import PN, G, paired
from test_hist_incremental import observation as fixture_observation
from yeoman_gateway.history import live
from yeoman_gateway.history.attestations import make
from yeoman_gateway.history.project import project
from yeoman_gateway.history.verify import table_digest, verify
from yeoman_shared.raw_archive.records import append_owner_record, enumerate_committed
from yeoman_shared.raw_archive.writer import RawArchive, RawEvent
from yeoman_shared.whatsapp_protocol import valid_group_metadata

CACHE = Path('/home/dm/.cache/yeoman-tests/history-cutover-4b')
SHORT_CACHE = Path('/home/dm/.cache/yt7')
TABLES = {'contacts', 'identifier_history', 'messages', 'message_events'}
CHECKS = {'baseline_accounting', 'oracle_parity', 'no_lost_observations',
          'bounded_resources', 'steady_lag_drained'}


def distribution(values: list[float], definition: str) -> dict[str, Any]:
    ordered = sorted(values)
    def percentile(q: float) -> float | None:
        return ordered[max(0, math.ceil(len(ordered) * q) - 1)] if ordered else None
    return {'p50': percentile(.5), 'p95': percentile(.95), 'p99': percentile(.99),
            'max': max(ordered) if ordered else None, 'sample_count': len(ordered),
            'definition': definition, 'percentile_method': 'nearest rank'}


def summarize(*, incremental: list[float], barriers: dict[str, list[float]],
              checks: dict[str, bool]) -> dict[str, Any]:
    definition = ('Request after durable archival/enrichment to acquired pinned read snapshot; '
                  'includes enumeration, locks and projection wait; excludes raw fsync, '
                  'enrichment, model and transport.')
    peak = barriers.get('event_burst', []) + barriers.get('message_burst', [])
    metrics = {name: distribution(values, definition) for name, values in barriers.items()}
    metrics['peak'] = distribution(peak, definition)
    passed = bool(barriers.get('event_burst') and barriers.get('message_burst')) and metrics['peak']['p95'] <= 1000
    verified = {key: checks.get(key, False) for key in sorted(CHECKS)}
    return {
        'incremental_ms_per_line': distribution(incremental,
            'Worker wall read/normalize/apply/checkpoint interval divided by newly supplied '
            'physical input lines, including automatic lineage work; batches are amortized and flagged separately; excludes '
            'raw append/fsync, target enumeration and fenced rebuilds.'),
        'barrier_wait_ms': metrics,
        'sample_count': {'incremental_lines': len(incremental),
                         'barrier': sum(len(items) for items in barriers.values()),
                         'peak_barrier': len(peak)},
        'budget_verdict': {'limit_ms': 1000, 'peak_barrier_pass': passed,
                           'checks': verified, 'accepted': passed and all(verified.values()),
                           'owner_acceptance': 'pending', 'production_latency': 'unverified'},
    }


def prepare_output(out: Path) -> Path:
    if not out.is_absolute() or '..' in out.parts:
        raise ValueError('absolute isolated cache output required')
    if not any(out.is_relative_to(base) and out != base for base in (CACHE, SHORT_CACHE)):
        raise ValueError('output must be below an approved synthetic cache root')
    if any(path.is_symlink() for path in (out, *out.parents)):
        raise ValueError('symlink output refused')
    out.mkdir(parents=True, exist_ok=False, mode=0o700)
    return out


def host_info(out: Path) -> dict[str, Any]:
    cpu = next((line.split(':', 1)[1].strip() for line in Path('/proc/cpuinfo').read_text().splitlines()
                if line.startswith(('Model', 'model name'))), platform.machine())
    memory = int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines()
                      if line.startswith('MemTotal:'))) * 1024
    disk = os.statvfs(out)
    mount = max((line.split()[:3] for line in Path('/proc/mounts').read_text().splitlines()
                 if out.is_relative_to(Path(line.split()[1]))), key=lambda fields: len(fields[1]))
    wt = Path(__file__).resolve().parents[3]
    pin = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=wt, text=True).strip()
    return {'cpu': cpu, 'cores': os.cpu_count(), 'ram_bytes': memory,
            'platform': platform.platform(), 'python': platform.python_version(),
            'storage': {'device': mount[0], 'filesystem': mount[2],
                        'available_bytes': disk.f_bavail * disk.f_frsize},
            'source_pin': pin, 'fd_limit': resource.getrlimit(resource.RLIMIT_NOFILE)[0],
            'cache_state': 'warm OS cache after fixture creation; no drop_caches; cold unmeasured',
            'isolation': 'new synthetic raw/DB/spool under approved cache; no live data'}


class Measurements:
    def __init__(self):
        self.apply: list[dict[str, Any]] = []
        self.barriers: dict[str, list[float]] = {}
        self.append: list[float] = []
        self.rebuilds: list[dict[str, Any]] = []
        self.lag: list[int] = []
        self.notified_lag: list[int] = []
        self.schedules: list[dict[str, Any]] = []
        self.endpoints: list[dict[str, Any]] = []
        self.receipts = []
        self.fd_baseline = len(list(Path('/proc/self/fd').iterdir()))
        self.fd_peak = self.fd_baseline
        self._done = threading.Event()
        self._sampler = threading.Thread(target=self._sample, daemon=True)

    def _sample(self):
        while not self._done.wait(.02):
            self.fd_peak = max(self.fd_peak, len(list(Path('/proc/self/fd').iterdir())))

    def start(self):
        self._sampler.start()

    def stop(self):
        self._done.set()
        self._sampler.join()

    def apply_committed(self, conn, index, root, target):
        prior = {b.relative_path: b.line_number for b in index._boundaries}
        lines = sum(max(0, b.line_number - prior.get(b.relative_path, 0)) for b in target)
        wall, cpu = perf_counter_ns(), thread_time_ns()
        try:
            result = self._original_apply(conn, index, root, target)
        except live.RebuildRequired as exc:
            self.rebuilds.append({'reason': str(exc), 'kind': 'rebuild_triggered',
                                 'attempt_seconds': (perf_counter_ns() - wall) / 1e9})
            raise
        if lines:
            self.apply.append({'lines': lines, 'batch': lines > 1,
                               'wall_ms_per_line': (perf_counter_ns() - wall) / 1e6 / lines,
                               'cpu_ms_per_line': (thread_time_ns() - cpu) / 1e6 / lines})
        return result

    def subscribe(self, projector):
        def committed(line):
            self.receipts.append(line)
            projector.notify_committed(line)
            self.notified_lag.append(projector.health()['lag_lines'])
        projector.archive.set_commit_callback(committed)

    async def turn(self, p, phase):
        start = perf_counter_ns()
        try:
            snapshot = await p.read_turn()
        except live.HistoryPaused:
            start_rebuild = perf_counter_ns()
            await p.rebuild(reason='synthetic_late_evidence')
            self.rebuilds.append({'reason': 'synthetic_late_evidence',
                                 'seconds': (perf_counter_ns() - start_rebuild) / 1e9})
            snapshot = await p.read_turn()
        elapsed = (perf_counter_ns() - start) / 1e6
        snapshot.close()
        self.barriers.setdefault(phase, []).append(elapsed)
        self.lag.append(p.health()['lag_lines'])
        assert p.health()['status'] == 'ready'

    def append_native(self, archive, row):
        start = perf_counter_ns()
        assert archive.append(RawEvent(channel='whatsapp', kind=row['kind'],
            direction=row['direction'], native=row['native'], chat_id=G, account='default',
            correlation_id=row.get('correlation_id', ''), received_ms=row['received_ms']))
        self.append.append((perf_counter_ns() - start) / 1e6)

    def derived(self, archive, native_id, *, transcript=False):
        row = {'kind': 'media_transcript' if transcript else 'media_description',
               'native_message_id': native_id, 'chat_id': G, 'channel': 'whatsapp',
               'text': 'synthetic derived', 'generated_ms': T0}
        start = perf_counter_ns()
        assert (archive.append_media_transcript(row) if transcript else archive.append_media_description(row))
        self.append.append((perf_counter_ns() - start) / 1e6)

    async def parity(self, p, out, phase):
        # Quiescent replay endpoint: the independent oracle reads exactly this prefix.
        await self.turn(p, 'idle')
        before = enumerate_committed(p.raw_root)
        started = perf_counter_ns()
        report = project([p.raw_root], out / 'oracle.db')
        assert report['accounting_ok']
        assert before == enumerate_committed(p.raw_root)
        actual, expected = table_digest(p.db_path), table_digest(out / 'oracle.db')
        assert set(actual) == TABLES and actual == expected
        snapshot = await p.read_turn()
        try:
            with closing(sqlite3.connect(out / 'oracle.db')) as conn:
                assert snapshot.connection.execute('SELECT * FROM messages_current ORDER BY message_id').fetchall() == conn.execute('SELECT * FROM messages_current ORDER BY message_id').fetchall()
            assert snapshot.sources == before
            observed = {b.relative_path: b for b in snapshot.sources}
            assert all(observed[line.relative_path].line_number >= line.line_number and
                       observed[line.relative_path].end_offset >= line.end_offset for line in self.receipts)
        finally:
            snapshot.close()
        self.endpoints.append({'phase': phase, 'four_table_digest_equal': True,
            'full_current_rows_equal': True, 'prefix_equal': True, 'observations_checkpointed': True,
            'lag_lines': p.health()['lag_lines'],
            'full_project_seconds': (perf_counter_ns() - started) / 1e9})
        print(f'endpoint {phase}: oracle parity, lag={p.health()["lag_lines"]}', flush=True)


async def start(p, samples):
    started = perf_counter_ns()
    await p.start()
    if p._startup_task is not None:
        await p._startup_task
    assert p.health()['status'] == 'ready', p.health()
    samples.subscribe(p)
    return (perf_counter_ns() - started) / 1e9


def observation(*args, **kwargs):
    row = fixture_observation(*args, **kwargs)
    row['chat_id'] = row['native']['payload']['chatJid']
    return row


def base_fixture(out):
    root = out / 'raw'
    write_jsonl(root / 'owner/attestations.jsonl', [
        make('contact', T0, 'synthetic role', identifiers=[f'{4915559990001 + n}@s.whatsapp.net'],
             name=f'Synthetic {role}', role=role) for n, role in enumerate(('assistant', 'owner'))])
    rows = []
    for number in range(6700):
        kind = 'message' if number % 3 == 0 else 'receipt'
        rows.append(observation(kind, native_id=f'B{number}', text=f'synthetic {number}',
                    sender=f'{4915550000000 + number % 64}@s.whatsapp.net',
                    chat=f'synthetic-{number % 32}@g.us', ms=T0 + number * 1000))
    write_jsonl(root / 'whatsapp/2026-10.jsonl', rows)
    write_jsonl(root / 'derived/media-descriptions.jsonl', [
        {'kind': 'media_description', 'native_message_id': f'B{n * 3}',
         'chat_id': f'synthetic-{n * 3 % 32}@g.us', 'text': 'synthetic description'} for n in range(84)])
    return root


async def scheduled_phase(p, samples, *, phase, count, duration, repetition=0, metadata=False):
    origin = perf_counter_ns()
    offsets = []
    async def produce():
        for number in range(count):
            due = duration * number / max(1, count - 1)
            await asyncio.sleep(max(0, due - (perf_counter_ns() - origin) / 1e9))
            native_id = f'{phase}-{repetition}-{number}'
            if metadata:
                kind = 'membership_snapshot' if number % 2 else 'group_subject'
                row = observation(kind, native_id=native_id,
                    sender=f'{4915552000000 + repetition * 41 + number}@s.whatsapp.net',
                    ms=T0 + 10_000_000 + repetition * 60_000 + number * 1000)
                if kind == 'group_subject':
                    row['native']['payload'] = {'chatJid': G, 'value': 'synthetic subject',
                        'snapshot': True, 'observedAtMs': row['received_ms']}
                    assert valid_group_metadata(row['native']['payload'])
                else:
                    row['native']['payload'].update(complete=True, participants=[{
                        'pn': row['native']['payload']['senderId'],
                        'lid': f'{980000 + repetition * 41 + number}@lid'}])
            else:
                row = observation(native_id=native_id, text=f'synthetic {native_id}',
                    sender=PN, ms=T0 + 20_000_000 + repetition * 60_000 + number * 1000)
                # Do not prewarm every contact/provider pair.
                if number == 0:
                    row['native']['payload'].update(participantJid=f'{970000 + repetition}@lid', senderPhoneJid=PN)
            offsets.append((perf_counter_ns() - origin) / 1e9)
            await asyncio.to_thread(samples.append_native, p.archive, row)
            if phase == 'message_burst' and number in (3, 8):
                await asyncio.to_thread(samples.derived, p.archive, native_id, transcript=number == 8)
    async def turns():
        for offset in (.05, duration / 2, duration + .02, duration + .10):
            await asyncio.sleep(max(0, offset - (perf_counter_ns() - origin) / 1e9))
            await samples.turn(p, phase)
    await asyncio.gather(produce(), turns())
    await samples.turn(p, phase)
    samples.schedules.append({'phase': phase, 'commits': count, 'scheduled_seconds': duration,
        'first_to_last_seconds': offsets[-1] - offsets[0],
        'max_start_lateness_ms': max(max(0, value - duration * n / max(1, count - 1))
                                     for n, value in enumerate(offsets)) * 1000})
    print(f'phase {phase} repetition {repetition + 1}: {count} commits', flush=True)


async def replay(out, repetitions, samples):
    root, db = base_fixture(out), out / 'history.db'
    began = perf_counter_ns()
    baseline = project([root], db, publish_lineage_root=root)
    initial_seconds = (perf_counter_ns() - began) / 1e9
    checked = verify([root], db, scratch=None)
    assert baseline['accounting_ok'] and checked['accounting_ok']
    archive = RawArchive(root, spool=out / 'spool', status_path=out / 'raw-status.json', clock=lambda: T0)
    p = live.HistoryProjector(root, db, archive)
    starts = []
    try:
        starts.append(await start(p, samples))
        await samples.parity(p, out, 'baseline')
        # Compressed replay checks cardinality/correctness; these are not observed-rate latency.
        for phase, count in (('steady_compressed_9_per_min', 9), ('spread_compressed_43_per_min', 43)):
            await scheduled_phase(p, samples, phase=phase, count=count, duration=.5)
            await samples.parity(p, out, phase)
        for rep in range(repetitions):
            await scheduled_phase(p, samples, phase='event_burst', count=41,
                                  duration=10, repetition=rep, metadata=True)
        await samples.parity(p, out, 'event_burst')
        await scheduled_phase(p, samples, phase='message_burst', count=12, duration=10)
        await samples.parity(p, out, 'message_burst')
        samples.append_native(archive, paired('outbound_request', corr='late-month'))
        await samples.turn(p, 'late_result')
        await samples.parity(p, out, 'pending_result')
        await p.stop()
        result = paired('outbound_result', corr='late-month', native_id='LATE')
        result['received_ms'] = T0 + 32 * 86400_000
        samples.append_native(archive, result)
        for number in range(41):
            samples.append_native(archive, observation(native_id=f'BACKLOG-{number}', text=f'backlog {number}'))
        starts.append(await start(p, samples))
        await samples.turn(p, 'restart_backlog')
        await samples.parity(p, out, 'restart_backlog_month_result')
        began = perf_counter_ns()
        await p.rebuild(reason='synthetic_owner_identity', mutation=lambda fd: append_owner_record(root,
            make('identifier', T0, 'synthetic binding', anchor=PN, identifier='979999@lid'),
            projection_owner_fd=fd, on_committed=p.notify_committed))
        samples.rebuilds.append({'reason': 'owner_identity', 'seconds': (perf_counter_ns() - began) / 1e9})
        await samples.parity(p, out, 'fenced_identity')
    finally:
        await p.stop()
    # Larger frozen-shaped synthetic source: copies, batches, UUID bindings and pairs.
    write_jsonl(root / 'backfill/copies.jsonl', [
        _bf('reply_context', 'message', {'messageId': f'B{n * 3}',
            'senderId': f'{4915550000000 + n * 3 % 64}@s.whatsapp.net', 'text': f'synthetic {n * 3}'},
            chat=f'synthetic-{n * 3 % 32}@g.us', ms=T0 + n * 3000) for n in range(1000)])
    write_jsonl(root / 'backfill/batches.jsonl', [_bf('memory', 'message', {'messageId': f'F{n}',
        'segments': [{'text': f'batch {n}', 'senderId': PN},
                     {'text': f'batch tail {n}', 'senderId': PN, 'messageId': f'F{n}'}]}, chat=G) for n in range(20)])
    write_jsonl(root / 'backfill/contacts.jsonl', [
        _bf('knowledge', 'contact_record', {'contactRef': '00000000-0000-4000-8000-000000000001',
            'createdMs': 1, 'displayName': 'Synthetic UUID'}, channel='any'),
        _bf('knowledge', 'identifier_record', {'contactRef': '00000000-0000-4000-8000-000000000001',
            'identifier': PN})])
    p = live.HistoryProjector(root, db, archive)
    try:
        await p.start()
        if p._startup_task is not None:
            await p._startup_task
        # Adding this global UUID/binding source requires the real fenced repair.
        began = perf_counter_ns()
        await p.rebuild(reason='synthetic_frozen_shape')
        samples.rebuilds.append({'reason': 'frozen_shape_identity', 'seconds': (perf_counter_ns() - began) / 1e9})
        samples.subscribe(p)
        await samples.parity(p, out, 'frozen_shaped')
    finally:
        await p.stop()
    return {'baseline_accounting': checked['accounting_ok'], 'initial_project_seconds': initial_seconds,
            'startup_seconds': starts, 'final_physical_lines': sum(b.line_number for b in enumerate_committed(root))}


def run_benchmark(out: Path, *, repetitions: int = 20) -> dict[str, Any]:
    if repetitions < 1:
        raise ValueError('positive repetitions required')
    out = prepare_output(out)
    samples = Measurements()
    samples._original_apply = live.apply_committed
    live.apply_committed = samples.apply_committed
    samples.start()
    started = perf_counter_ns()
    try:
        replay_result = asyncio.run(replay(out, repetitions, samples))
    finally:
        live.apply_committed = samples._original_apply
        samples.stop()
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
    fd_final = len(list(Path('/proc/self/fd').iterdir()))
    checks = {'baseline_accounting': replay_result['baseline_accounting'],
              'oracle_parity': bool(samples.endpoints) and all(e['four_table_digest_equal'] and
                              e['full_current_rows_equal'] and e['prefix_equal'] for e in samples.endpoints),
              'no_lost_observations': all(e['observations_checkpointed'] for e in samples.endpoints),
              'bounded_resources': samples.fd_peak < 1024 and fd_final <= samples.fd_baseline and rss < 2 * 1024**3,
              'steady_lag_drained': bool(samples.endpoints) and all(e['lag_lines'] == 0 for e in samples.endpoints)}
    report = summarize(incremental=[s['wall_ms_per_line'] for s in samples.apply for _ in range(s['lines'])],
                       barriers=samples.barriers, checks=checks)
    report['sample_count'].update(native_event_burst_commits=repetitions * 41,
        raw_append=len(samples.append), successful_incremental_calls=len(samples.apply),
        rebuild_triggered_calls=sum('attempt_seconds' in s for s in samples.rebuilds))
    report.update({'host': host_info(out), 'source_shape': {
        'base_native_lines': 6700, 'base_descriptions': 84, 'frozen_extra_copies': 1000,
        'frozen_batches': 20, 'frozen_uuid_records': 2, 'reconnect_event_commits': repetitions * 41,
        'message_burst_commits': 12, 'burst_schedule_seconds': 10,
        'coordinator_30_day_aggregates': {'events': 6638, 'peak_per_min': 43, 'peak_per_10s': 41,
            'message_peak_per_min': 13, 'message_peak_per_10s': 12, 'active_minute_p99': 9, 'peak_day': 518},
        'compressed_phases': ['steady_compressed_9_per_min', 'spread_compressed_43_per_min'],
        'rate_latency_phases': ['event_burst', 'message_burst']},
        'raw_append_fsync_ms': distribution(samples.append, 'Caller wall for real RawArchive append including fsync; separate from worker cost.'),
        'incremental_cpu_ms_per_line': distribution([s['cpu_ms_per_line'] for s in samples.apply for _ in range(s['lines'])],
            'Worker thread CPU for successful apply divided by new physical lines; batches amortized.'),
        'apply_batches': samples.apply, 'lag_lines': {'after_turn_max': max(samples.lag), 'after_notification_max': max(samples.notified_lag, default=0),
            'definition': 'health after pinned turn; restart backlog drained before readiness; not continuous lag'},
        'rebuild_seconds': {**distribution([s['seconds'] for s in samples.rebuilds if 'seconds' in s],
            'Wall time for real fenced rebuild through verified publication and tail release; failed incremental attempts listed separately.'),
            'samples': samples.rebuilds}, 'rss_peak_bytes': rss, 'fd_peak': samples.fd_peak,
        'resource_measurement': {'rss': 'process lifetime getrusage high-water; Linux KiB converted to bytes',
            'fd': 'process /proc/self/fd sampled every 20 ms; subinterval peaks may be missed',
            'fd_baseline': samples.fd_baseline, 'fd_final': fd_final,
            'bounds': 'RSS <2 GiB; FD <1024 and final <= baseline'},
        'endpoints': samples.endpoints, 'schedules': samples.schedules, 'baseline': replay_result,
        'wall_seconds': (perf_counter_ns() - started) / 1e9})
    (out / 'benchmark.json').write_text(json.dumps(report, indent=2) + '\n')
    assert checks['oracle_parity'] and checks['no_lost_observations']
    print(json.dumps({'budget_verdict': report['budget_verdict'], 'wall_seconds': report['wall_seconds']}), flush=True)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('out', type=Path)
    parser.add_argument('--repetitions', type=int, default=20)
    args = parser.parse_args()
    run_benchmark(args.out, repetitions=args.repetitions)
