"""Gateway service: periodic people sync from history, with one directory snapshot per local day."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from yeoman_gateway.history.context import history_turn
from yeoman_gateway.history.live import HistoryPaused

from .backup import snapshot_people_db
from .history_source import ObservedPerson, read_observed_persons
from .schema import open_people_db
from .store import PeopleStore
from .sync import SyncReport, sync_from_history


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _local_day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000).astimezone().date().isoformat()


def _is_lock_error(exc: sqlite3.OperationalError) -> bool:
    text = str(exc).lower()
    return "locked" in text or "busy" in text


class PeopleSyncService:
    def __init__(self, *, projector: Any, db_path: Path, interval_seconds: float = 300,
                 contact_group_max_members: int = 100, clock: Callable[[], int] = _now_ms,
                 backup_dir: Path | None = None) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self.projector, self.db_path = projector, db_path
        self.interval_seconds = interval_seconds
        self.contact_group_max_members = contact_group_max_members
        self.clock = clock
        self.backup_dir = backup_dir
        self._lock = threading.Lock()
        self._task: asyncio.Task[None] | None = None

    async def run_once(self) -> SyncReport | None:
        """One pass; None when skipped (history paused or the directory is locked)."""
        try:
            async with history_turn(self.projector) as snapshot:
                observed = read_observed_persons(
                    snapshot.connection, contact_group_max_members=self.contact_group_max_members)
        except HistoryPaused:
            logger.debug("people sync skipped: history paused")
            return None
        report = await asyncio.to_thread(self._apply, observed)
        if report is not None:
            logger.info("people sync {}", report.as_dict())
        return report

    def _apply(self, observed: list[ObservedPerson]) -> SyncReport | None:
        with self._lock:
            now = self.clock()
            try:
                conn = open_people_db(self.db_path)
            except sqlite3.OperationalError as exc:
                if _is_lock_error(exc):
                    logger.warning("people sync skipped: directory locked")
                    return None
                raise
            try:
                store = PeopleStore(conn)
                day = _local_day(now)
                row = conn.execute("SELECT value FROM meta WHERE key = 'last_backup_day'").fetchone()
                backed_up = row is not None and row[0] == day
                if not backed_up:
                    snapshot_people_db(self.db_path, directory=self.backup_dir,
                                       now=datetime.fromtimestamp(now / 1000).astimezone())
                with store.transaction("sync"):
                    report = sync_from_history(store, observed, now)
                    meta = {"last_sync_ms": str(now),
                            "last_sync_report": json.dumps(report.as_dict(), sort_keys=True)}
                    if not backed_up:
                        meta["last_backup_day"] = day
                    conn.executemany("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                                     list(meta.items()))
                return report
            except sqlite3.OperationalError as exc:
                if _is_lock_error(exc):
                    logger.warning("people sync skipped: directory locked")
                    return None
                raise
            finally:
                conn.close()

    async def _run(self) -> None:
        while True:
            if self.projector.health()["status"] == "starting":
                await asyncio.sleep(1)
                continue
            try:
                await self.run_once()
            except Exception as exc:
                logger.warning("people sync pass failed: {}", type(exc).__name__)
            await asyncio.sleep(self.interval_seconds)

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
