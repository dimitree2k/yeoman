"""Synthetic fail-closed writer retirement witnesses."""
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from yeoman_shared.config.schema import Config

from tests.gateway.test_history_source_compatibility import case  # noqa: F401


def configure(monkeypatch, tmp_path, disabled):
    root = tmp_path / 'synthetic-home'
    root.mkdir(exist_ok=True)
    monkeypatch.setenv('YEOMAN_HOME', str(root))
    if disabled:
        (root / 'config.json').write_text(json.dumps({'configVersion': 3}))
    return root








def mutation(kind, obj):
    if kind == 'session':
        from yeoman_gateway.session.manager import Session
        return obj.save(Session('whatsapp:synthetic', channel='telegram'))
    if kind == 'identity':
        from yeoman_gateway.knowledge.models import TrustedAdminContext
        return obj.set_preferred_name('synthetic', 'Changed', context=TrustedAdminContext('synthetic', 1, 'test', True))
    if kind == 'contacts-store':
        return obj.create_contact(display_name='Synthetic')
    if kind == 'contacts-service':
        return obj.ensure_contact(channel='whatsapp', identifier='10001', kind='phone_jid', push_name='Synthetic')
    if kind == 'memory-store':
        return obj.index_canonical_event(SimpleNamespace(channel='whatsapp'))
    if kind == 'auto-semantic-notes':
        return obj.enqueue_background_note(channel='whatsapp', chat_id='synthetic', sender_id='10001', message_id='m', content='Synthetic', is_group=False)
    return obj.start()




@pytest.mark.parametrize('missing', ['adapter', 'knowledge', 'handover'])
def test_startup_requires_knowledge_adapter_and_handover(tmp_path, monkeypatch, missing):
    from yeoman_gateway.app import bootstrap
    from yeoman_gateway.bus.queue import MessageBus
    from yeoman_gateway.history.live import HistoryPaused
    from yeoman_gateway.knowledge._history_upgrade import upgrade_history_knowledge

    from tests.gateway.test_history_knowledge_upgrade import v2
    source = v2(tmp_path / 'v2.db')
    target = tmp_path / 'v3.db'
    upgrade_history_knowledge(source=source, target=target)
    with sqlite3.connect(target) as db:
        if missing != 'handover':
            db.execute("INSERT INTO knowledge_history_capture_state VALUES ('handover',?,1)",
                       (json.dumps({'version': 1, 'generation': 1, 'sources': [], 'pending': [], 'processed': [], 'legacy_boundary': [0, '']}),))
    config = Config(knowledge={'enabled': missing != 'knowledge', 'db_path': str(target)})
    if missing == 'adapter':
        import yeoman_gateway.adapters.reply_archive_history as adapters
        monkeypatch.setattr(adapters, 'HistoryReplyArchiveAdapter', None)
    def forbidden(*args, **kwargs):
        pytest.fail('preflight reached legacy writer constructor')
    monkeypatch.setattr(bootstrap, 'SessionManager', forbidden)
    with pytest.raises(HistoryPaused):
        bootstrap.build_gateway_runtime(config=config, provider=MagicMock(), policy_engine=None,
                                        policy_path=None, workspace=tmp_path, bus=MessageBus())


def test_statement_writes_continue_while_identity_tables_are_frozen(case):  # noqa: F811 - fixture
    from yeoman_gateway.knowledge._history_upgrade import FROZEN_IDENTITY_TABLES
    from yeoman_gateway.knowledge.models import (
        StatementCandidate,
        TrustedCaptureContext,
    )

    from tests.gateway.test_history_knowledge_upgrade import digest
    service, queries, sources, _ = case
    before = {t: digest(service._store.connection, t) for t in FROZEN_IDENTITY_TABLES}
    legacy_before = {t: digest(service._store.connection, t) for t in ('memory2_meta', 'memory2_fact_jobs', 'idea_backlog_items')}
    legacy_nodes = service._store.query("SELECT * FROM memory2_nodes WHERE id NOT IN (SELECT statement_id FROM knowledge_statements)")
    counts = {t: service._store.scalar(f'SELECT count(*) FROM {t}') for t in (
        'knowledge_statements', 'knowledge_jobs', 'knowledge_statement_audit')}
    source = sources.issue('m')
    context = TrustedCaptureContext('synthetic', 1, 'native', (source,))
    with service.history_scope(queries, sources):
        assert service.capture(StatementCandidate('Synthetic new statement', (source,)), context=context).statement_ids
        service.enqueue_capture((source,), context=context)
    assert not hasattr(service, 'set_preferred_name')
    for table in FROZEN_IDENTITY_TABLES:
        with pytest.raises(sqlite3.IntegrityError, match='history_identity_read_only'):
            service._store.connection.execute(f'INSERT INTO {table} DEFAULT VALUES')
    assert {t: digest(service._store.connection, t) for t in FROZEN_IDENTITY_TABLES} == before
    assert all(service._store.scalar(f'SELECT count(*) FROM {t}') > n for t, n in counts.items())
    assert {t: digest(service._store.connection, t) for t in legacy_before} == legacy_before
    assert service._store.query("SELECT * FROM memory2_nodes WHERE id NOT IN (SELECT statement_id FROM knowledge_statements)") == legacy_nodes
    assert service._store.scalar('PRAGMA query_only') == 0


def test_session_whatsapp_files_are_refused_and_telegram_persists(tmp_path, monkeypatch):
    from yeoman_gateway.session.manager import Session, SessionManager
    root = configure(monkeypatch, tmp_path, False)
    manager = SessionManager(tmp_path, sessions_dir=tmp_path / 'sessions')
    monkeypatch.setattr(manager, '_get_session_path', lambda key: (_ for _ in ()).throw(AssertionError(key)))
    from yeoman_gateway.history.live import HistoryPaused
    with pytest.raises(HistoryPaused, match='jsonl_retired'):
        manager._load('whatsapp:synthetic')
    with pytest.raises(HistoryPaused, match='jsonl_retired'):
        manager.save(Session('whatsapp:synthetic'))
    with pytest.raises(HistoryPaused, match='jsonl_retired'):
        manager.delete('whatsapp:synthetic')
    telegram = SessionManager(tmp_path, sessions_dir=tmp_path / 'telegram-sessions')
    telegram.save(Session('telegram:synthetic'))
    assert len(list(telegram.sessions_dir.glob('*.jsonl'))) == 1
    assert not (root / 'data' / 'operational' / 'history').exists()
    assert not list(root.rglob('knowledge.db'))


def test_whatsapp_bootstrap_builds_raw_archive(tmp_path, monkeypatch):
    from yeoman_gateway.bus.queue import MessageBus
    from yeoman_gateway.channels.manager import ChannelManager
    configure(monkeypatch, tmp_path, False)
    config = Config(channels={'whatsapp': {'enabled': True}})
    channels = ChannelManager(config, MessageBus())
    assert channels.raw_archive is not None


@pytest.mark.parametrize('retired', [True])
def test_bootstrap_writer_routes_and_operational_exceptions(tmp_path, monkeypatch, retired):
    from yeoman_gateway.app.bootstrap import build_gateway_runtime
    from yeoman_gateway.bus.queue import MessageBus
    from yeoman_gateway.knowledge._history_upgrade import upgrade_history_knowledge

    from tests.gateway.test_history_knowledge_upgrade import v2
    root = configure(monkeypatch, tmp_path, False)
    target = tmp_path / 'v3.db'
    if retired:
        upgrade_history_knowledge(source=v2(tmp_path / 'v2.db'), target=target)
        with sqlite3.connect(target) as db:
            db.execute("INSERT INTO knowledge_history_capture_state VALUES ('handover',?,1)",
                       (json.dumps({'version': 1, 'generation': 1, 'sources': [], 'pending': [], 'processed': [], 'legacy_boundary': [0, '']}),))
    config = Config(knowledge={'enabled': retired, 'db_path': str(target), 'capture_enabled': retired},
                    processing={'enabled': True}, security={'enabled': False},
                    memory={'db_path': str(tmp_path / 'memory.db')},
                    personaEvolution={'enabled': False})
    from yeoman_gateway.knowledge import _capture_worker
    monkeypatch.setattr(_capture_worker, 'StatementExtractor', lambda **kwargs: MagicMock())
    provider = MagicMock()
    provider.get_default_model.return_value = 'synthetic'
    runtime = build_gateway_runtime(config=config, provider=provider, policy_engine=None, policy_path=None,
                                    workspace=tmp_path / 'workspace', bus=MessageBus())
    try:
        assert runtime.channels.raw_archive is not None
        assert runtime.processing is not None
        from yeoman_gateway.knowledge._history_capture import HistoryCaptureWorker
        assert isinstance(runtime.statement_capture, HistoryCaptureWorker)
        assert not (root / 'data' / 'operational' / 'inbound' / 'reply_context.db').exists()
        assert not (root / 'data' / 'operational' / 'inbound' / 'chat_registry.db').exists()
        assert not (root / 'data' / 'operational' / 'contacts').exists()
        assert not (tmp_path / 'memory.db').exists()
        # The responder always receives the shared channel-aware session manager.
        manager = runtime.responder.sessions
        manager.save(manager.get_or_create('telegram:synthetic'))
        assert list(manager.sessions_dir.glob('telegram*.jsonl'))
        assert runtime.channels.document_cache is not None
    finally:
        for obj in (runtime.processing, runtime.responder.knowledge):
            if obj is not None:
                obj.close()


def test_signal_sink_only_journals_canonical_event(tmp_path):
    from yeoman_gateway.processing.signals import SignalJournalSink
    from yeoman_gateway.processing.store import ProcessingStore
    store = ProcessingStore(tmp_path / 'processing.db')
    sink = SignalJournalSink(store)
    try:
        sink.capture('message', {'chatJid': 'synthetic@g.us', 'messageId': 'm', 'text': 'Synthetic'})
        assert store.count_events() == 1
        assert not hasattr(sink, 'index_enrichments')
        assert not hasattr(sink, '_memory')
    finally:
        store.close()






def test_signal_sink_has_no_retired_memory_cache_or_guard():
    from yeoman_gateway.processing.signals import SignalJournalSink
    sink = SignalJournalSink(MagicMock())
    assert not hasattr(sink, 'index_enrichments')
    assert not hasattr(sink, '_memory')


