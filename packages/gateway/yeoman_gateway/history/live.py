"""Dormant Gateway-owned history writer and all-committed read admission."""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

from yeoman_shared.raw_archive.records import (
    CommittedLine,
    SourceBoundary,
    _acquire_exclusive,
    _import_receipt,
    _no_symlinks,
    acquire_projection_owner,
    enumerate_committed,
)
from yeoman_shared.raw_archive.writer import RawArchive
from yeoman_shared.utils.helpers import get_operational_data_path

from .incremental import ProjectionIndex, RebuildRequired, apply_committed
from .schema import PROJECTOR_VERSION, SCHEMA_VERSION

if TYPE_CHECKING:
    from .reader import HistoryReader, HistorySnapshot

T = TypeVar('T')


class HistoryPaused(RuntimeError):  # noqa: N818 - canonical history API name
    """No history-dependent context may be acquired while freshness is unproven."""


@dataclass(frozen=True, slots=True)
class HistoryBoundary:
    generation: int
    sources: tuple[SourceBoundary, ...]


def history_db_path(*, create: bool = False) -> Path:
    directory = get_operational_data_path() / 'history'
    if create:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return directory / 'history.db'


def acquire_history_writer(db_path: Path) -> int:
    # Resolve aliases to one stable lock; reject symlinks at the lock itself.
    destination = db_path.resolve()
    return _acquire_exclusive(destination.with_name(destination.name + '.writer.lock'))


class HistoryProjector:
    def __init__(self, raw_root: Path, db_path: Path, archive: RawArchive):
        if not isinstance(archive, RawArchive) or archive.root.resolve() != raw_root.resolve():
            raise ValueError('history requires the enabled raw archive')
        self.raw_root, self.db_path, self.archive = raw_root, db_path.resolve(), archive
        self._operation_lock = asyncio.Lock()
        self._notification_lock = threading.Lock()
        self._high_water: dict[str, CommittedLine] = {}
        self._wake_scheduled = False
        self._event = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._executor: ThreadPoolExecutor | None = None
        self._worker: asyncio.Task[None] | None = None
        self._startup_task: asyncio.Task[None] | None = None
        self._callback_registered = False
        self._connection: sqlite3.Connection | None = None
        self._index: ProjectionIndex | None = None
        self._reader: HistoryReader | None = None
        self._writer_fd: int | None = None
        self._projection_owner_fd: int | None = None
        self._stopping = False
        self._status = 'disabled'
        self._reason: str | None = None
        self._generation = 0
        self._lag_lines = self._lag_bytes = 0
        self._lag_target: tuple[SourceBoundary, ...] = ()
        self._oldest_wait: float | None = None

    async def _submit(self, function: Callable[..., T], *args: Any) -> T:
        if self._executor is None:
            raise HistoryPaused('disabled')
        future = asyncio.get_running_loop().run_in_executor(self._executor, function, *args)
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            # SQLite/fsync cannot be cancelled; retain operation ownership until it settles.
            await asyncio.shield(future)
            raise

    async def start(self) -> None:
        if self._status != 'disabled':
            return  # Startup failures require repair or an explicit stop/start, never an automatic retry.
        self._stopping = False
        self._status, self._reason = 'starting', None
        collision_reason = 'writer_lock_held'
        try:
            self._writer_fd = acquire_history_writer(self.db_path)
            collision_reason = 'projection_owner_held'
            self._projection_owner_fd = acquire_projection_owner(self.raw_root)
            collision_reason = 'startup_failed'
            self._loop = asyncio.get_running_loop()
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='history-projector')
            self.archive.set_commit_callback(self.notify_committed)
            self._callback_registered = True
            self._startup_task = asyncio.create_task(self._start_background())
        except Exception as exc:
            reason = collision_reason if isinstance(exc, BlockingIOError) else 'startup_failed'
            await self._stop()
            self._status, self._reason = 'failed', reason

    async def _start_background(self) -> None:
        async with self._operation_lock:
            try:
                await self._submit(self._startup)
                if self._stopping:
                    return
                from .reader import HistoryReader

                self._reader = HistoryReader(self.db_path)
                self._status, self._reason = 'ready', None
                self._worker = asyncio.create_task(self._run_notifications())
            except RebuildRequired as exc:
                self._failed(exc)
            except Exception:
                self._status, self._reason = 'failed', 'startup_failed'

    def _startup(self) -> None:
        # Recovery may rewrite bytes. Validate the stored prefix after settling it.
        self._lag(enumerate_committed(self.raw_root))
        _import_receipt(self.raw_root, '')
        _no_symlinks(self.db_path)
        if not self.db_path.exists():
            raise RebuildRequired('history database absent')
        with closing(sqlite3.connect(f'{self.db_path.as_uri()}?mode=ro', uri=True)) as probe:
            if probe.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION:
                raise RebuildRequired('unsupported history schema')
            states = probe.execute('SELECT file, lines, end_offset, sha256, projector_version, state_json FROM projector_state').fetchall()
        if any(row[4] != PROJECTOR_VERSION for row in states):
            raise RebuildRequired('unsupported projector version')
        runtime = [row for row in states if row[0] == '@runtime']
        if len(runtime) != 1:
            raise RebuildRequired('missing runtime checkpoint')
        boundaries = tuple(SourceBoundary(*row[:4]) for row in states if row[0] != '@runtime')
        self._index = ProjectionIndex.from_prefix(self.raw_root, boundaries)
        if any(item.contact_id not in self._index._reserved_ids
               for item in self._index._resolution.generated_ids):
            raise RebuildRequired('unpublished generated IDs require fenced repair')
        self._connection = sqlite3.connect(self.db_path)
        self._connection.execute('PRAGMA foreign_keys=ON')
        self._connection.execute('PRAGMA journal_mode=WAL')
        self._connection.execute('PRAGMA synchronous=FULL')
        self._catch_up(self._index.target(self.raw_root))
        row = self._connection.execute("SELECT state_json FROM projector_state WHERE file='@runtime'").fetchone()
        state = json.loads(row[0])
        state['generation'] = int(state['generation']) + 1
        self._connection.execute("UPDATE projector_state SET state_json=? WHERE file='@runtime'", (json.dumps(state, sort_keys=True),))
        self._connection.commit()
        self._generation = state['generation']

    def notify_committed(self, line: CommittedLine) -> None:
        if Path(line.relative_path).parts[:1] not in [('whatsapp',), ('backfill',), ('derived',), ('owner',)]:
            return
        with self._notification_lock:
            if self._stopping or self._loop is None:
                return
            previous = self._high_water.get(line.relative_path)
            if previous is None or previous.end_offset < line.end_offset:
                self._high_water[line.relative_path] = line
            if self._oldest_wait is None:
                self._oldest_wait = time.monotonic()
            if not self._wake_scheduled:
                self._wake_scheduled = True
                self._loop.call_soon_threadsafe(self._event.set)

    async def _run_notifications(self) -> None:
        while True:
            await self._event.wait()
            self._event.clear()
            with self._notification_lock:
                if self._status in ('ready', 'backlog'):
                    self._high_water.clear()
                self._wake_scheduled = False
            if self._stopping:
                return
            if self._status not in ('ready', 'backlog'):
                continue
            async with self._operation_lock:
                try:
                    await self._submit(self._fence)
                except HistoryPaused:
                    pass
                except Exception as exc:
                    self._failed(exc)

    def _lag(self, target: Sequence[SourceBoundary]) -> None:
        self._lag_target = tuple(target)
        prior = {b.relative_path: b for b in self._index._boundaries} if self._index else {}
        self._lag_lines = sum(max(0, b.line_number - (prior[b.relative_path].line_number if b.relative_path in prior else 0)) for b in target)
        self._lag_bytes = sum(max(0, b.end_offset - (prior[b.relative_path].end_offset if b.relative_path in prior else 0)) for b in target)

    def _catch_up(self, target: Sequence[SourceBoundary]) -> None:
        self._lag(target)
        if self._connection is None or self._index is None:
            raise HistoryPaused(self._reason or 'starting')
        if tuple(target) != self._index._boundaries:
            apply_committed(self._connection, self._index, self.raw_root, target)
        self._lag(self._index._boundaries)
        self._oldest_wait = None

    def _failed(self, exc: Exception) -> None:
        self._status = 'rebuilding' if isinstance(exc, RebuildRequired) else 'failed'
        self._reason = 'rebuild_required' if isinstance(exc, RebuildRequired) else 'projection_failed'

    def _fence(self) -> HistoryBoundary:
        if self._status not in ('ready', 'backlog') or self._index is None:
            raise HistoryPaused(self._reason or self._status)
        self.archive.drain_spool()
        status = self.archive.status()
        if status.spooled or status.pending_in_memory:
            self._status, self._reason = 'backlog', 'retained_backlog'
            raise HistoryPaused('retained_backlog')
        while True:
            target = self._index.target(self.raw_root)
            self._catch_up(target)
            if self._index._boundaries == target:
                break
            # Own reservations changed the vector: select every destination again before pinning.
        self._status, self._reason = 'ready', None
        return HistoryBoundary(self._generation, self._index._boundaries)

    async def catch_up(self, target: Sequence[SourceBoundary]) -> None:
        self._require_ready()
        async with self._operation_lock:
            if self._status not in ('ready', 'backlog'):
                raise HistoryPaused(self._reason or self._status)
            try:
                await self._submit(self._catch_up, target)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._failed(exc)
                raise HistoryPaused(self._reason) from exc

    async def _admit(self) -> HistoryBoundary:
        try:
            return await self._submit(self._fence)
        except HistoryPaused:
            raise
        except Exception as exc:
            self._failed(exc)
            raise HistoryPaused(self._reason) from exc

    def _require_ready(self) -> None:
        if self._status not in ('ready', 'backlog') or self._stopping:
            raise HistoryPaused(self._reason or self._status)

    async def barrier(self) -> HistoryBoundary:
        self._require_ready()
        async with self._operation_lock:
            return await self._admit()

    async def read_turn(self) -> HistorySnapshot:
        self._require_ready()
        async with self._operation_lock:
            boundary = await self._admit()
            if self._reader is None:
                raise HistoryPaused('starting')
            # No await between fence completion and the real read pin on the consumer thread.
            try:
                return self._reader.open_snapshot(boundary)
            except HistoryPaused:
                self._status, self._reason = 'failed', 'reader_unavailable'
                raise

    def health(self) -> dict[str, Any]:
        with self._notification_lock:
            index = self._index
            prior = {b.relative_path: b for b in index._boundaries} if index else {}
            observed = {b.relative_path: b for b in self._lag_target}
            for rel, line in self._high_water.items():
                if rel not in observed or observed[rel].end_offset < line.end_offset:
                    observed[rel] = SourceBoundary(rel, line.line_number, line.end_offset, '')
            notified_lines = sum(max(0, line.line_number - (prior[rel].line_number if rel in prior else 0))
                                 for rel, line in observed.items())
            notified_bytes = sum(max(0, line.end_offset - (prior[rel].end_offset if rel in prior else 0))
                                 for rel, line in observed.items())
            return {'status': self._status, 'reason': self._reason, 'generation': self._generation,
                    'lag_lines': max(self._lag_lines, notified_lines), 'lag_bytes': max(self._lag_bytes, notified_bytes),
                    'retry_policy': 'none',
                    'oldest_wait_age_ms': int((time.monotonic() - self._oldest_wait) * 1000) if self._oldest_wait is not None else 0}

    def _close_writer(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None
        self._index = None

    async def _stop(self) -> None:
        self._stopping = True
        if self._callback_registered:
            self.archive.set_commit_callback(None)
            self._callback_registered = False
        if self._startup_task is not None:
            await self._startup_task
            self._startup_task = None
        self._event.set()
        if self._worker is not None:
            await self._worker
            self._worker = None
        async with self._operation_lock:
            if self._reader is not None:
                self._reader.close()
                self._reader = None
            if self._executor is not None:
                await self._submit(self._close_writer)
                self._executor.shutdown(wait=True)
                self._executor = None
            for name in ('_projection_owner_fd', '_writer_fd'):
                fd = getattr(self, name)
                if fd is not None:
                    os.close(fd)
                    setattr(self, name, None)
            self._loop = None
            with self._notification_lock:
                self._high_water.clear()
                self._wake_scheduled = False
            self._lag_target = ()
            self._lag_lines = self._lag_bytes = 0
            self._oldest_wait = None
            self._status, self._reason = 'disabled', None

    async def stop(self) -> None:
        task = asyncio.create_task(self._stop())
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await asyncio.shield(task)
            raise
