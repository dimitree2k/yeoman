import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.mark.parametrize('name', ['history.db', 'reply_context.db', 'chat_registry.db', 'memory.db', 'knowledge.db', 'contacts.db'])
def test_overseer_cannot_query_or_prune_retired_history(tmp_path, monkeypatch, name):
    from yeoman_overseer.agent.tools import prune_memory, query_db, query_memory
    path = tmp_path / 'data' / name
    ctx = SimpleNamespace(yeoman_home=tmp_path)
    opened = Mock(side_effect=AssertionError('sqlite opened'))
    copied = Mock(side_effect=AssertionError('snapshot created'))
    monkeypatch.setattr(query_db.sqlite3, 'connect', opened)
    monkeypatch.setattr(prune_memory.shutil, 'copy2', copied)
    assert 'refus' in query_db.execute({'db_path': str(path), 'query': 'SELECT 1'}, ctx).lower()
    assert not prune_memory.prune_memory(age_days=1, ctx=ctx)['ok']
    assert 'unavailable' in query_memory.execute({'query': 'synthetic'}, ctx).lower()
    opened.assert_not_called()
    copied.assert_not_called()


def test_processing_metrics_remain_readonly_and_history_aliases_are_refused(tmp_path):
    import sqlite3

    from yeoman_overseer.agent.tools import query_db
    data = tmp_path / 'data'
    data.mkdir()
    path = data / 'processing.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE effects (state TEXT)')
        db.execute("INSERT INTO effects VALUES ('confirmed')")
    ctx = SimpleNamespace(yeoman_home=tmp_path)
    assert '1' in query_db.execute({'db_path': str(path), 'query': 'SELECT count(*) FROM effects'}, ctx)
    assert 'ERROR' in query_db.execute({'db_path': str(path), 'query': 'DELETE FROM effects'}, ctx)
    alias = data / 'alias.db'
    alias.symlink_to(data / 'history.db')
    assert 'refus' in query_db.execute({'db_path': str(alias), 'query': 'SELECT 1'}, ctx)
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT count(*) FROM effects').fetchone()[0] == 1


def test_query_memory_always_uses_scoped_gateway_read(tmp_path, monkeypatch):
    from yeoman_overseer.agent.tools import query_memory

    sent = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def readline(self, _limit):
            return b'{"messages": []}\n'

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def settimeout(self, _seconds):
            pass

        def connect(self, path):
            assert path == str(tmp_path / 'gateway.sock')

        def sendall(self, payload):
            sent.append(json.loads(payload))

        def makefile(self, _mode):
            return Response()

    monkeypatch.setattr(query_memory.socket, 'socket', lambda *_args: Client())
    ctx = SimpleNamespace(yeoman_home=tmp_path, gateway_socket_path=tmp_path / 'gateway.sock')
    result = query_memory.execute({'chat_id': 'g@g.us', 'query': 'synthetic', 'limit': 4}, ctx)
    assert json.loads(result) == {'messages': []}
    assert sent == [{'cmd': 'knowledge_read', 'args': {
        'chat_id': 'g@g.us', 'query': 'synthetic', 'limit': 4}}]
