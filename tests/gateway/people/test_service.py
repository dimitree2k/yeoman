from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from datetime import datetime

import pytest
from yeoman_gateway.history.live import HistoryPaused
from yeoman_gateway.people.schema import open_people_db
from yeoman_gateway.people.service import PeopleSyncService

from tests.gateway.people.people_fixtures import level_history

DAY_MS = 86_400_000


@pytest.fixture
def projected_history(tmp_path):
    history = level_history(tmp_path / "history")
    yield history
    history.close()


def count_persons(db):
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT count(*) FROM persons").fetchone()[0]
    finally:
        conn.close()


def service_for(tmp_path, projector, **kwargs):
    return PeopleSyncService(projector=projector, db_path=tmp_path / "people" / "people.db",
                             backup_dir=tmp_path / "backups", **kwargs)


async def test_first_pass_fills_directory_and_writes_meta(tmp_path, projected_history):
    service = service_for(tmp_path, projected_history)
    report = await service.run_once()
    assert report is not None and report.persons_created == 8
    conn = open_people_db(service.db_path)
    meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
    assert json.loads(meta["last_sync_report"])["persons_created"] == 8
    assert int(meta["last_sync_ms"]) > 0
    second = await service.run_once()
    assert second is not None and set(second.as_dict().values()) == {0}


async def test_sync_skips_when_owner_holds_write_lock(tmp_path, projected_history):
    db = tmp_path / "people" / "people.db"
    open_people_db(db).close()
    holder = sqlite3.connect(db, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")                       # owner in DB Browser
    service = service_for(tmp_path, projected_history)
    started = time.monotonic()
    assert await service.run_once() is None
    assert time.monotonic() - started < 10
    assert count_persons(db) == 0                           # no partial writes
    holder.execute("ROLLBACK")
    holder.close()
    assert (await service.run_once()) is not None
    assert count_persons(db) > 0


class PausedProjector:
    async def read_turn(self):
        raise HistoryPaused("generation_invalidated")

    def health(self):
        return {"status": "ready", "generation": 1}


async def test_history_paused_skips_quietly(tmp_path):
    service = service_for(tmp_path, PausedProjector())
    assert await service.run_once() is None
    assert not service.db_path.exists()


async def test_daily_backup_once_per_day(tmp_path, projected_history):
    now = [int(datetime(2026, 10, 13, 12).timestamp() * 1000)]
    service = service_for(tmp_path, projected_history, clock=lambda: now[0])
    await service.run_once()
    await service.run_once()
    assert len(list((tmp_path / "backups").iterdir())) == 1
    now[0] += DAY_MS
    await service.run_once()
    snapshots = sorted((tmp_path / "backups").iterdir())
    assert len(snapshots) == 2
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in snapshots)
    restored = sqlite3.connect(snapshots[-1])
    assert restored.execute("SELECT count(*) FROM persons").fetchone()[0] == 8


async def test_start_stop_loop(tmp_path, projected_history):
    service = service_for(tmp_path, projected_history, interval_seconds=3600)
    await service.start()
    # The file appears before its schema; under parallel load the first pass can take seconds.
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            if service.db_path.exists() and count_persons(service.db_path) > 0:
                break
        except sqlite3.OperationalError:
            pass
        await asyncio.sleep(0.05)
    await service.stop()
    assert count_persons(service.db_path) == 8
