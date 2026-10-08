"""Consumer-thread SQLite snapshots with explicit generation leases."""
from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from yeoman_shared.raw_archive.records import SourceBoundary

from .live import HistoryBoundary, HistoryPaused


@dataclass(eq=False)
class HistorySnapshot:
    generation: int
    sources: tuple[SourceBoundary, ...]
    connection: sqlite3.Connection
    _closed: bool = field(default=False, init=False)
    _release: Callable[[], None] | None = field(default=None, init=False, repr=False)

    def close(self) -> None:
        if self._closed:
            return
        self.connection.close()
        self._closed = True
        if self._release is not None:
            self._release()

    def assert_current(self, generation: int) -> None:
        if self._closed or self.generation != generation:
            raise HistoryPaused('generation_invalidated')


class HistoryReader:
    def __init__(self, db_path: Path):
        self.db_path = db_path.resolve()
        self._generation: int | None = None
        self._inode: tuple[int, int] | None = None
        self._snapshots: set[HistorySnapshot] = set()
        self._drained = asyncio.Event()
        self._drained.set()

    def open_snapshot(self, boundary: HistoryBoundary) -> HistorySnapshot:
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(f'{self.db_path.as_uri()}?mode=ro', uri=True)
            conn.execute('PRAGMA query_only=ON')
            conn.execute('BEGIN')
            # BEGIN alone is lazy: this real read pins committed WAL and generation together.
            row = conn.execute("SELECT state_json FROM projector_state WHERE file='@runtime'").fetchone()
            state = json.loads(row[0]) if row is not None else None
            if not isinstance(state, dict) or state.get('generation') != boundary.generation:
                raise HistoryPaused('generation_invalidated')
            info = self.db_path.stat()
            identity = info.st_dev, info.st_ino
            if self._generation is not None and boundary.generation < self._generation:
                raise HistoryPaused('generation_invalidated')
            if self._inode is not None and identity != self._inode and boundary.generation == self._generation:
                raise HistoryPaused('generation_invalidated')
            self._generation, self._inode = boundary.generation, identity
            snapshot = HistorySnapshot(boundary.generation, boundary.sources, conn)
            def release() -> None:
                self._snapshots.discard(snapshot)
                if not self._snapshots:
                    self._drained.set()

            snapshot._release = release
            self._snapshots.add(snapshot)
            self._drained.clear()
            return snapshot
        except BaseException as exc:
            if conn is not None:
                conn.close()
            if isinstance(exc, (sqlite3.Error, OSError, ValueError, TypeError)):
                raise HistoryPaused('reader_unavailable') from exc
            raise

    async def _wait_for_snapshots(self) -> None:
        await self._drained.wait()

    def close(self) -> None:
        for snapshot in tuple(self._snapshots):
            snapshot.close()
