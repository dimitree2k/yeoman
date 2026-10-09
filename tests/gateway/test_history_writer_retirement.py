"""Synthetic fail-closed writer retirement witnesses."""
import hashlib
import json
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from yeoman_shared.config.schema import Config

from tests.gateway.test_history_source_compatibility import case  # noqa: F401


def configure(monkeypatch, tmp_path, disabled):
    root = tmp_path / 'synthetic-home'
    root.mkdir(exist_ok=True)
    monkeypatch.setenv('YEOMAN_HOME', str(root))
    (root / 'config.json').write_text(json.dumps({'configVersion': 2, 'history': {'legacyWritersDisabled': disabled}}))
    return root


def writer(kind, path, disabled=False):
    from yeoman_gateway.adapters.reply_archive_sqlite import SqliteReplyArchiveAdapter
    from yeoman_gateway.knowledge._contacts.service import ContactsService
    from yeoman_gateway.knowledge._contacts.store import ContactsStore
    from yeoman_gateway.knowledge._identity import IdentityEngine
    from yeoman_gateway.knowledge._memory.extraction_jobs import SharedFactExtractionQueue
    from yeoman_gateway.knowledge._memory.service import MemoryService
    from yeoman_gateway.knowledge._memory.store import MemoryStore
    from yeoman_gateway.knowledge._store import KnowledgeStore
    from yeoman_gateway.session.manager import SessionManager
    from yeoman_gateway.storage.chat_registry import ChatRegistry
    from yeoman_gateway.storage.inbound_archive import InboundArchive
    factories = {
        'session': lambda: SessionManager(path.parent, sessions_dir=path.parent / 'sessions'),
        'identity': lambda: IdentityEngine(KnowledgeStore(path), authority=MagicMock(), policy=MagicMock()),
        'reply-adapter': lambda: SqliteReplyArchiveAdapter(InboundArchive(path)),
        'archive': lambda: InboundArchive(path, legacy_history_disabled=disabled),
        'registry': lambda: ChatRegistry(path, legacy_history_disabled=disabled),
        'contacts-store': lambda: ContactsStore(path, legacy_history_disabled=disabled),
        'contacts-service': lambda: ContactsService(path, legacy_history_disabled=disabled),
        'memory-store': lambda: MemoryStore(path, legacy_history_disabled=disabled),
        'auto-semantic-notes': lambda: MemoryService(workspace=path.parent, config=Config(memory={'db_path': str(path)}).memory, legacy_history_disabled=disabled),
        'shared-facts': lambda: SharedFactExtractionQueue(store=MagicMock(), legacy_history_disabled=disabled),
    }
    return factories[kind]()


KINDS = ('archive', 'registry', 'contacts-store', 'contacts-service', 'memory-store', 'auto-semantic-notes', 'shared-facts')


@pytest.mark.parametrize('kind', KINDS)
@pytest.mark.parametrize('direct_file', [False, True])
def test_retired_writer_constructor_fails_before_file_creation(tmp_path, monkeypatch, kind, direct_file):
    from yeoman_gateway.history.writer_guard import LegacyHistoryWriterDisabled
    configure(monkeypatch, tmp_path, direct_file)
    path = tmp_path / 'uncreated' / 'legacy.db'
    threads = set(threading.enumerate())
    with pytest.raises(LegacyHistoryWriterDisabled):
        writer(kind, path, disabled=not direct_file)
    assert not path.parent.exists()
    assert set(threading.enumerate()) == threads


def mutation(kind, obj):
    if kind == 'session':
        from yeoman_gateway.session.manager import Session
        return obj.save(Session('whatsapp:synthetic', channel='telegram'))
    if kind == 'identity':
        from yeoman_gateway.knowledge.models import TrustedAdminContext
        return obj.set_preferred_name('synthetic', 'Changed', context=TrustedAdminContext('synthetic', 1, 'test', True))
    if kind == 'reply-adapter':
        from datetime import datetime

        from yeoman_gateway.core.models import InboundEvent
        return obj.record_inbound(InboundEvent(channel='whatsapp', chat_id='synthetic', sender_id='10001', content='Synthetic', timestamp=datetime.now(), message_id='m'))
    if kind == 'archive':
        return obj.record_inbound(channel='whatsapp', chat_id='synthetic', message_id='m', participant=None, sender_id='10001', text='Synthetic', timestamp=1)
    if kind == 'registry':
        return obj.register_chat(channel='whatsapp', chat_id='synthetic')
    if kind == 'contacts-store':
        return obj.create_contact(display_name='Synthetic')
    if kind == 'contacts-service':
        return obj.ensure_contact(channel='whatsapp', identifier='10001', kind='phone_jid', push_name='Synthetic')
    if kind == 'memory-store':
        return obj.index_canonical_event(SimpleNamespace(channel='whatsapp'))
    if kind == 'auto-semantic-notes':
        return obj.enqueue_background_note(channel='whatsapp', chat_id='synthetic', sender_id='10001', message_id='m', content='Synthetic', is_group=False)
    return obj.start()


@pytest.mark.parametrize('kind', (*KINDS, 'session', 'identity', 'reply-adapter'))
@pytest.mark.parametrize('transition', ['instance', 'explicit-after-file-change'])
def test_retired_writer_call_fails_on_existing_instance(tmp_path, monkeypatch, kind, transition):
    from yeoman_gateway.history.writer_guard import LegacyHistoryWriterDisabled
    root = configure(monkeypatch, tmp_path, False)
    path = tmp_path / 'legacy.db'
    obj = writer(kind, path)
    def files():
        return {str(p.relative_to(tmp_path)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in tmp_path.rglob('*') if p.is_file() and root not in p.parents}
    if kind == 'session':
        from yeoman_gateway.session.manager import Session
        obj.save(Session('whatsapp:synthetic'))
    before = files()
    threads = set(threading.enumerate())
    if transition == 'instance':
        obj.legacy_history_disabled = True
    else:
        (root / 'config.json').write_text(json.dumps({'history': {'legacyWritersDisabled': True}}))
        obj.legacy_history_disabled = True
    try:
        with pytest.raises(LegacyHistoryWriterDisabled):
            mutation(kind, obj)
        if kind == "identity":
            with pytest.raises(LegacyHistoryWriterDisabled):
                obj._resolve_verified_provider_pair(None, pair=(), evidence_ref="synthetic", context=None, create_stub=True)
        assert files() == before
        assert set(threading.enumerate()) == threads
    finally:
        if hasattr(obj, 'close'):
            obj.close()
        elif kind == 'identity':
            obj._store.close()
        elif kind == 'reply-adapter':
            obj._archive.close()


@pytest.mark.parametrize('missing', ['projection', 'adapter', 'knowledge', 'handover'])
def test_writer_off_requires_replacements_and_handover(tmp_path, monkeypatch, missing):
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
    config = Config(history={'liveProjectionEnabled': missing != 'projection', 'legacyWritersDisabled': True},
                    knowledge={'enabled': missing != 'knowledge', 'db_path': str(target)})
    if missing == 'adapter':
        import yeoman_gateway.adapters.reply_archive_history as adapters
        monkeypatch.setattr(adapters, 'HistoryReplyArchiveAdapter', None)
    def forbidden(*args, **kwargs):
        pytest.fail('preflight reached legacy writer constructor')
    monkeypatch.setattr(bootstrap, 'SessionManager', forbidden)
    monkeypatch.setattr(bootstrap, 'InboundArchive', forbidden)
    with pytest.raises(HistoryPaused):
        bootstrap.build_gateway_runtime(config=config, provider=MagicMock(), policy_engine=None,
                                        policy_path=None, workspace=tmp_path, bus=MessageBus())


def test_statement_writes_continue_while_identity_tables_are_frozen(case):  # noqa: F811 - fixture
    from yeoman_gateway.knowledge._history_upgrade import FROZEN_IDENTITY_TABLES
    from yeoman_gateway.knowledge.models import (
        StatementCandidate,
        TrustedAdminContext,
        TrustedCaptureContext,
    )

    from tests.gateway.test_history_knowledge_upgrade import digest
    service, queries, sources, _ = case
    service.legacy_history_disabled = True
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
    with pytest.raises(RuntimeError):
        service.set_preferred_name('a', 'Changed', context=TrustedAdminContext('synthetic', 1, 'test', True))
    for table in FROZEN_IDENTITY_TABLES:
        with pytest.raises(sqlite3.IntegrityError, match='history_identity_read_only'):
            service._store.connection.execute(f'INSERT INTO {table} DEFAULT VALUES')
    assert {t: digest(service._store.connection, t) for t in FROZEN_IDENTITY_TABLES} == before
    assert all(service._store.scalar(f'SELECT count(*) FROM {t}') > n for t, n in counts.items())
    assert {t: digest(service._store.connection, t) for t in legacy_before} == legacy_before
    assert service._store.query("SELECT * FROM memory2_nodes WHERE id NOT IN (SELECT statement_id FROM knowledge_statements)") == legacy_nodes
    assert service._store.scalar('PRAGMA query_only') == 0


def test_retirement_default_false_preserves_legacy_and_telegram(tmp_path, monkeypatch):
    from yeoman_gateway.history.writer_guard import require_legacy_history_writer
    from yeoman_gateway.session.manager import Session, SessionManager
    root = configure(monkeypatch, tmp_path, False)
    legacy = SessionManager(tmp_path, sessions_dir=tmp_path / 'sessions')
    legacy.save(Session('whatsapp:synthetic'))
    assert len(list(legacy.sessions_dir.glob('*.jsonl'))) == 1
    require_legacy_history_writer(disabled=MagicMock(), channel='whatsapp')
    retired = SessionManager(tmp_path, sessions_dir=tmp_path / 'retired', legacy_history_disabled=True)
    from yeoman_gateway.history.live import HistoryPaused
    with pytest.raises(HistoryPaused):
        retired.get_or_create('whatsapp:synthetic', channel='whatsapp', chat_id='synthetic')
    assert not retired.sessions_dir.exists()
    retired.save(retired.get_or_create('telegram:synthetic'))
    assert len(list(retired.sessions_dir.glob('*.jsonl'))) == 1
    assert not (root / 'data' / 'operational' / 'history').exists()
    assert not list(root.rglob('knowledge.db'))


def test_whatsapp_and_fact_bootstrap_skip_private_legacy_constructors(tmp_path, monkeypatch):
    from yeoman_gateway.app.bootstrap import build_shared_fact_runtime
    from yeoman_gateway.bus.queue import MessageBus
    from yeoman_gateway.channels.manager import ChannelManager
    from yeoman_gateway.storage import chat_registry
    configure(monkeypatch, tmp_path, False)
    def forbidden(*args, **kwargs):
        pytest.fail('retired bootstrap constructed a private registry')
    monkeypatch.setattr(chat_registry, 'ChatRegistry', forbidden)
    config = Config(history={'legacyWritersDisabled': True}, channels={'whatsapp': {'enabled': True}})
    channels = ChannelManager(config, MessageBus())
    assert channels.channels['whatsapp']._chat_registry is None
    assert channels.raw_archive is not None
    assert build_shared_fact_runtime(config, store=MagicMock(), memory=MagicMock()) is None


@pytest.mark.parametrize('retired', [False, True])
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
    config = Config(history={'liveProjectionEnabled': retired, 'legacyWritersDisabled': retired,
                             'readers': {k: retired for k in ('participation', 'whatsapp', 'responder', 'tools', 'secondary', 'knowledge')}},
                    knowledge={'enabled': retired, 'db_path': str(target), 'capture_enabled': retired},
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
        if retired:
            assert runtime.inbound_archive is None
            assert runtime.chat_registry is None
            assert runtime.contacts is None
            assert runtime.memory is None
            assert runtime.shared_facts is None
            from yeoman_gateway.knowledge._history_capture import HistoryCaptureWorker
            assert isinstance(runtime.statement_capture, HistoryCaptureWorker)
            assert not (root / 'data' / 'operational' / 'inbound' / 'reply_context.db').exists()
            assert not (root / 'data' / 'operational' / 'inbound' / 'chat_registry.db').exists()
            assert not (root / 'data' / 'operational' / 'contacts').exists()
            assert not (tmp_path / 'memory.db').exists()
        else:
            assert runtime.inbound_archive is not None
            assert runtime.chat_registry is not None
            assert runtime.contacts is not None
            assert runtime.memory is not None
            assert runtime.history_projector is None
            assert not target.exists()
            assert not (root / 'data' / 'operational' / 'history').exists()
        # The responder always receives the shared channel-aware session manager.
        manager = runtime.responder.sessions
        manager.save(manager.get_or_create('telegram:synthetic'))
        assert list(manager.sessions_dir.glob('telegram*.jsonl'))
        assert runtime.channels.document_cache is not None
    finally:
        for obj in (runtime.inbound_archive, runtime.chat_registry, runtime.contacts, runtime.memory,
                    runtime.processing, runtime.responder.knowledge):
            if obj is not None:
                obj.close()


def test_retired_canonical_fts_is_unreachable_but_journal_keeps_writing(tmp_path):
    from yeoman_gateway.processing.signals import SignalJournalSink
    from yeoman_gateway.processing.store import ProcessingStore
    store = ProcessingStore(tmp_path / 'processing.db')
    memory = MagicMock()
    sink = SignalJournalSink(store, memory=memory, legacy_history_disabled=True)
    try:
        sink.capture('message', {'chatJid': 'synthetic@g.us', 'messageId': 'm', 'text': 'Synthetic'})
        assert store.count_events() == 1
        assert sink.index_enrichments('m', ({'text': 'Synthetic enrichment'},)) == ()
        memory.index_canonical_event.assert_not_called()
    finally:
        store.close()


def test_memory_factory_uses_root_retirement_before_any_worker_or_file(tmp_path):
    from yeoman_gateway.history.writer_guard import LegacyHistoryWriterDisabled
    from yeoman_gateway.knowledge._memory.service import MemoryService
    config = Config(history={'legacyWritersDisabled': True}, memory={'db_path': str(tmp_path / 'uncreated' / 'legacy.db')})
    threads = set(threading.enumerate())
    with pytest.raises(LegacyHistoryWriterDisabled):
        MemoryService(workspace=tmp_path, config=config.memory, root_config=config)
    assert not (tmp_path / 'uncreated').exists()
    assert set(threading.enumerate()) == threads


@pytest.mark.parametrize('kind', (*KINDS, 'session', 'identity', 'reply-adapter'))
@pytest.mark.parametrize('retired', [False, True])
def test_guarded_calls_do_not_read_config_after_construction(tmp_path, monkeypatch, kind, retired):
    from yeoman_gateway.history.writer_guard import (
        LegacyHistoryWriterDisabled,
        require_legacy_history_writer,
    )
    configure(monkeypatch, tmp_path, False)
    obj = writer(kind, tmp_path / 'legacy.db')
    obj.legacy_history_disabled = retired
    reads = MagicMock(side_effect=AssertionError('guard must not read config'))
    monkeypatch.setattr(Path, 'read_text', reads)
    try:
        for _ in range(20):
            if retired:
                with pytest.raises(LegacyHistoryWriterDisabled):
                    require_legacy_history_writer(disabled=obj.legacy_history_disabled, channel='whatsapp')
            else:
                require_legacy_history_writer(disabled=obj.legacy_history_disabled, channel='whatsapp')
        reads.assert_not_called()
    finally:
        if hasattr(obj, 'close'):
            obj.close()
        elif kind == 'identity':
            obj._store.close()
        elif kind == 'reply-adapter':
            obj._archive.close()


@pytest.mark.parametrize('retired', [False, True])
def test_archive_and_signal_guards_use_cached_flag(tmp_path, monkeypatch, retired):
    from yeoman_gateway.adapters.reply_archive_sqlite import SqliteReplyArchiveAdapter
    from yeoman_gateway.history.live import HistoryPaused
    from yeoman_gateway.history.writer_guard import LegacyHistoryWriterDisabled
    from yeoman_gateway.processing.signals import SignalJournalSink
    from yeoman_gateway.storage.inbound_archive import InboundArchive
    configure(monkeypatch, tmp_path, False)
    archive = InboundArchive(tmp_path / 'legacy.db')
    adapter = SqliteReplyArchiveAdapter(archive)
    sink = SignalJournalSink(MagicMock(), memory=MagicMock())
    archive.legacy_history_disabled = adapter.legacy_history_disabled = sink.legacy_history_disabled = retired
    reads = MagicMock(side_effect=AssertionError('hot path must not read config'))
    monkeypatch.setattr(Path, 'read_text', reads)
    try:
        for _ in range(20):
            if retired:
                with pytest.raises(HistoryPaused):
                    adapter.lookup_message('whatsapp', 'synthetic', 'm')
                with pytest.raises(LegacyHistoryWriterDisabled):
                    mutation('archive', archive)
            else:
                adapter.lookup_message('whatsapp', 'synthetic', 'm')
                mutation('archive', archive)
            sink.index_enrichments('m', ())
        reads.assert_not_called()
    finally:
        archive.close()


@pytest.mark.parametrize('failure', ['parse', 'read'])
def test_unreadable_config_preserves_dormant_constructor_behavior(tmp_path, monkeypatch, failure):
    from yeoman_gateway.storage.inbound_archive import InboundArchive
    root = configure(monkeypatch, tmp_path, False)
    if failure == 'parse':
        (root / 'config.json').write_text('{')
    else:
        monkeypatch.setattr(Path, 'read_text', MagicMock(side_effect=OSError('synthetic unreadable config')))
    archive = InboundArchive(tmp_path / 'legacy.db')
    try:
        assert archive.legacy_history_disabled is False
        mutation('archive', archive)
    finally:
        archive.close()


def test_knowledge_composition_passes_resolved_flag_to_identity(case, tmp_path, monkeypatch):  # noqa: F811 - fixture
    from yeoman_gateway.history.writer_guard import LegacyHistoryWriterDisabled
    from yeoman_gateway.knowledge.api import KnowledgeService
    from yeoman_gateway.knowledge.models import TrustedAdminContext
    service, _, _, _ = case
    configure(monkeypatch, tmp_path, True)
    resolved = KnowledgeService(store=service._store, workspace_id='synthetic',
                                source_authority=service._legacy_authority, policy_authority=service._policy,
                                history_mode=True)
    assert resolved.legacy_history_disabled is True
    assert resolved._identity.legacy.legacy_history_disabled is True
    reads = MagicMock(side_effect=AssertionError('identity must use the resolved flag'))
    monkeypatch.setattr(Path, 'read_text', reads)
    with pytest.raises(LegacyHistoryWriterDisabled):
        resolved._identity.legacy.set_preferred_name('synthetic', 'Changed', context=TrustedAdminContext('synthetic', 1, 'test', True))
    reads.assert_not_called()
