"""Synthetic database and lease instrumentation for the reader wiring tests."""
import sqlite3
from types import SimpleNamespace

from yeoman_gateway.history.live import HistoryPaused
from yeoman_gateway.history.reader import HistorySnapshot
from yeoman_gateway.history.schema import FTS_ROWS, create


class Projector:
    def __init__(self):
        self.db = sqlite3.connect(':memory:')
        create(self.db)
        self.generation = 1
        self.status = 'ready'
        self.snapshots = []
        self.acquisitions = 0

    def add(self, mid, chat='a@g.us', text='synthetic text', direction='in'):
        self.db.execute("INSERT INTO messages VALUES (?, 'whatsapp', ?, ?, NULL, NULL,"
                        " 'unknown', ?, 1700000000000, 'native', ?, NULL, NULL, NULL, 'native', '[]')",
                        (mid, chat, mid, direction, text))
        self.db.execute('DELETE FROM messages_fts')
        self.db.execute('INSERT INTO messages_fts(message_id,chat_id,text) ' + FTS_ROWS)
        self.db.commit()

    async def read_turn(self):
        if self.status != 'ready':
            raise HistoryPaused(self.status)
        db = sqlite3.connect(':memory:')
        self.db.backup(db)
        snapshot = HistorySnapshot(self.generation, (), db)
        self.snapshots.append(snapshot)
        self.acquisitions += 1
        return snapshot

    def health(self):
        return {'status': self.status, 'generation': self.generation}

    @property
    def live_snapshot_count(self):
        return sum(not item._closed for item in self.snapshots)

    def close(self):
        for item in self.snapshots:
            item.close()
        self.db.close()


def history_config(**readers):
    return SimpleNamespace(live_projection_enabled=True, readers=SimpleNamespace(
        participation=readers.get('participation', False), whatsapp=readers.get('whatsapp', False),
        responder=readers.get('responder', False), tools=readers.get('tools', False)))


class FileProjector(Projector):
    """Use the real read lease on an atomically replaced synthetic SQLite file."""
    def __init__(self, root):
        super().__init__()
        self.path = root / 'synthetic-history.db'
        disk = sqlite3.connect(self.path)
        self.db.backup(disk)
        self.db.close()
        self.db = disk
        self.db.execute("INSERT INTO projector_state VALUES ('@runtime',0,0,'synthetic',4,?)", ('{"generation":1}',))
        self.db.commit()
        from yeoman_gateway.history.reader import HistoryReader
        self.reader = HistoryReader(self.path)

    async def read_turn(self):
        if self.status != 'ready':
            raise HistoryPaused(self.status)
        from yeoman_gateway.history.live import HistoryBoundary
        snapshot = self.reader.open_snapshot(HistoryBoundary(self.generation, ()))
        self.snapshots.append(snapshot)
        self.acquisitions += 1
        return snapshot

    def replace(self, phone):
        import json
        import os
        target = self.path.with_suffix('.replacement')
        db = sqlite3.connect(target)
        self.db.backup(db)
        self.generation += 1
        db.execute("UPDATE identifier_history SET value=?", (phone,))
        db.execute("UPDATE projector_state SET state_json=? WHERE file='@runtime'", (json.dumps({'generation': self.generation}),))
        db.commit()
        db.close()
        self.db.close()
        os.replace(target, self.path)
        self.db = sqlite3.connect(self.path)
