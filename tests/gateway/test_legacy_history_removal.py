"""Executable receipts for the unconditional conversation-history runtime."""
import ast
import importlib
import os
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

REMOVED_MODULES = (
    'yeoman_gateway.adapters.reply_archive_sqlite',
    'yeoman_gateway.agent.tools.summarize_history',
    'yeoman_gateway.consciousness.taste',
    'yeoman_gateway.history.writer_guard',
    'yeoman_gateway.knowledge._contacts.service',
    'yeoman_gateway.knowledge._contacts.store',
    'yeoman_gateway.knowledge._identity_candidates',
    'yeoman_gateway.knowledge._memory.archive_backfill',
    'yeoman_gateway.knowledge._memory.disclosure',
    'yeoman_gateway.knowledge._memory.disclosure_backfill',
    'yeoman_gateway.knowledge._memory.extraction_jobs',
    'yeoman_gateway.knowledge._memory.extractor',
    'yeoman_gateway.knowledge._memory.fact_extractor',
    'yeoman_gateway.knowledge._memory.models',
    'yeoman_gateway.knowledge._memory.service',
    'yeoman_gateway.knowledge._memory.session_state',
    'yeoman_gateway.storage.chat_registry',
    'yeoman_gateway.storage.inbound_archive',
)
LEGACY_PATHS = (
    'data/inbound', 'data/memory/memory.db', 'data/memory/session-state',
    'data/contacts', 'data/sessions', 'reader-operational.db',
)


def test_legacy_history_modules_are_gone():
    for module in REMOVED_MODULES:
        with pytest.raises(ModuleNotFoundError) as error:
            importlib.import_module(module)
        assert error.value.name == module
    packages = Path(__file__).parents[2] / 'packages'
    removed = re.compile(r'\b(?:legacyWritersDisabled|legacy_writers_disabled|liveProjectionEnabled|live_projection_enabled|HistoryReaderConfig|HistoryConfig)\b')
    for path in packages.rglob('*.py'):
        source = path.read_text()
        assert not removed.search(source), str(path)
        tree = ast.parse(source)
        assert not any(isinstance(node, ast.Attribute) and node.attr == 'readers'
                       and isinstance(node.value, ast.Attribute) and node.value.attr == 'history'
                       for node in ast.walk(tree)), str(path)


@pytest.mark.asyncio
async def test_fresh_start_creates_no_legacy_paths(tmp_path, monkeypatch):
    from loguru import logger
    from yeoman_gateway.app.bootstrap import build_gateway_runtime
    from yeoman_gateway.bus.queue import MessageBus
    from yeoman_gateway.core.models import InboundEvent
    from yeoman_gateway.history.project import project
    from yeoman_gateway.knowledge._history_upgrade import upgrade_history_knowledge
    from yeoman_gateway.knowledge._store import KnowledgeStore
    from yeoman_gateway.providers.base import LLMResponse
    from yeoman_shared.config.schema import Config

    root = Path(os.environ['YEOMAN_HOME'])
    workspace = root / 'workspace'
    workspace.mkdir(exist_ok=True)
    source = tmp_path / 'verified-v2.db'
    store = KnowledgeStore(source)
    store.set_meta('migration_complete', '1')
    store.close()
    target = tmp_path / 'knowledge-v3.db'
    upgrade_history_knowledge(source=source, target=target)
    provider = MagicMock()
    provider.get_default_model.return_value = 'synthetic'
    provider.chat = AsyncMock(return_value=LLMResponse(content='synthetic reply'))
    monkeypatch.setattr('yeoman_gateway.providers.factory.ProviderFactory.create_chat_provider',
                        lambda *args, **kwargs: provider)
    config = Config(knowledge={'enabled': True, 'db_path': str(target)},
                    processing={'enabled': False}, security={'enabled': False},
                    personaEvolution={'enabled': False})
    bus = MessageBus()
    runtime = build_gateway_runtime(config=config, provider=provider, policy_engine=None,
                                    policy_path=None, workspace=workspace, bus=bus)
    projector = runtime.history_projector
    assert projector is not None
    errors = []
    log_id = logger.add(lambda message: errors.append(str(message)), level='ERROR')
    try:
        project([runtime.channels.raw_archive.root], projector.db_path,
                publish_lineage_root=runtime.channels.raw_archive.root)
        await projector.start()
        if projector._startup_task is not None:
            await projector._startup_task
        assert projector.health()['status'] == 'ready'
        for channel, chat in (('whatsapp', '10001@s.whatsapp.net'), ('telegram', '10001')):
            await runtime.orchestrator._process_message(InboundEvent(
                channel=channel, chat_id=chat, sender_id='10001', content='hello',
                message_id=f'{channel}-synthetic'))
        assert not errors
        assert provider.chat.await_count >= 2
        assert bus.outbound.qsize() >= 2
        for home in (root, Path.home() / '.yeoman'):
            assert not any((home / relative).exists() for relative in LEGACY_PATHS)
            assert not list(home.rglob('reader-operational.db'))
        assert (root / 'data/ops/session-metadata.db').is_file()
        assert list((root / 'data/ops/telegram-sessions').glob('*.jsonl'))
    finally:
        logger.remove(log_id)
        await projector.stop()
        runtime.responder.sessions.operational_store.close()
        if runtime.responder.knowledge is not None:
            runtime.responder.knowledge.close()
