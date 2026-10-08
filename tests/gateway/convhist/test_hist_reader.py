"""Generation leases, inode reopen and real WAL snapshot witnesses."""
import json
import os
import sqlite3

import pytest


def database(path, generation, text):
    conn = sqlite3.connect(path)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.executescript('CREATE TABLE messages(text TEXT); CREATE TABLE projector_state(file TEXT, state_json TEXT);')
    conn.execute('INSERT INTO messages VALUES (?)', (text,))
    conn.execute('INSERT INTO projector_state VALUES (?, ?)', ('@runtime', json.dumps({'generation': generation})))
    conn.commit()
    return conn


def test_reader_reopens_after_atomic_replace(tmp_path):
    from yeoman_gateway.history.live import HistoryBoundary, HistoryPaused
    from yeoman_gateway.history.reader import HistoryReader
    path = tmp_path / 'history.db'
    writer = database(path, 1, 'old')
    reader = HistoryReader(path)
    old = reader.open_snapshot(HistoryBoundary(1, ()))
    assert old.connection.execute('SELECT text FROM messages').fetchall() == [('old',)]
    writer.execute("UPDATE messages SET text='later'")
    writer.commit()
    assert old.connection.execute('SELECT text FROM messages').fetchall() == [('old',)]
    assert reader._snapshots  # Task 7 must wait this lease before replacement.
    old.close()
    assert not reader._snapshots
    writer.close()
    replacement = tmp_path / 'replacement.db'
    database(replacement, 2, 'new').close()
    os.replace(replacement, path)
    new = reader.open_snapshot(HistoryBoundary(2, ()))
    assert new.generation == 2
    assert new.connection is not old.connection
    assert new.connection.execute('SELECT text FROM messages').fetchall() == [('new',)]
    with pytest.raises(HistoryPaused):
        old.assert_current(2)
    with pytest.raises(sqlite3.ProgrammingError):
        old.connection.execute('SELECT text FROM messages')
    with pytest.raises(HistoryPaused):
        reader.open_snapshot(HistoryBoundary(1, ()))
    new.close()
    reader.close()


def test_reader_snapshot_sees_wal_and_excludes_later_commits(tmp_path):
    from yeoman_gateway.history.live import HistoryBoundary
    from yeoman_gateway.history.reader import HistoryReader
    path = tmp_path / 'history.db'
    writer = database(path, 1, 'first')
    writer.execute("INSERT INTO messages VALUES ('wal')")
    writer.commit()
    reader = HistoryReader(path)
    snapshot = reader.open_snapshot(HistoryBoundary(1, ()))
    writer.execute("INSERT INTO messages VALUES ('future')")
    writer.commit()
    assert snapshot.connection.execute('SELECT text FROM messages').fetchall() == [('first',), ('wal',)]
    snapshot.assert_current(1)
    snapshot.close()
    new = reader.open_snapshot(HistoryBoundary(1, ()))
    assert len(new.connection.execute('SELECT text FROM messages').fetchall()) == 3
    new.close()
    reader.close()
    writer.close()


@pytest.mark.asyncio
async def test_reader_replacement_waits_for_open_generation_lease(tmp_path):
    import asyncio

    from yeoman_gateway.history.live import HistoryBoundary
    from yeoman_gateway.history.reader import HistoryReader
    path = tmp_path / 'history.db'
    writer = database(path, 1, 'old')
    reader = HistoryReader(path)
    old = reader.open_snapshot(HistoryBoundary(1, ()))
    async def replacement():
        await reader._wait_for_snapshots()
        writer.close()
        candidate = tmp_path / 'candidate.db'
        database(candidate, 2, 'new').close()
        os.replace(candidate, path)
    task = asyncio.create_task(replacement())
    await asyncio.sleep(0.01)
    try:
        assert not task.done()
        assert old.connection.execute('SELECT text FROM messages').fetchall() == [('old',)]
    finally:
        old.close()
        await task
    new = reader.open_snapshot(HistoryBoundary(2, ()))
    assert new.connection.execute('SELECT text FROM messages').fetchall() == [('new',)]
    new.close()
    reader.close()


@pytest.mark.parametrize('case', ['absent', 'invalid_runtime'])
def test_unavailable_reader_pauses_without_open_snapshot(tmp_path, case):
    from yeoman_gateway.history.live import HistoryBoundary, HistoryPaused
    from yeoman_gateway.history.reader import HistoryReader
    path = tmp_path / 'history.db'
    if case == 'invalid_runtime':
        writer = database(path, 1, 'old')
        writer.execute("UPDATE projector_state SET state_json='invalid-json'")
        writer.commit()
        writer.close()
    reader = HistoryReader(path)
    with pytest.raises(HistoryPaused):
        reader.open_snapshot(HistoryBoundary(1, ()))
    assert not reader._snapshots
    reader.close()
