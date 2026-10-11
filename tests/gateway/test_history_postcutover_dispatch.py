"""Synthetic retired-mode capture and intent isolation witnesses."""
from unittest.mock import AsyncMock

import pytest
from yeoman_gateway.app.bootstrap import OrchestratorService
from yeoman_gateway.core.intents import (
    QueueMemoryNotesCaptureIntent,
    RecordManualMemoryIntent,
    SendOutboundIntent,
)
from yeoman_gateway.core.models import InboundEvent, OutboundEvent, PolicyDecision
from yeoman_gateway.core.orchestrator import Orchestrator
from yeoman_shared.config.schema import HistoryConfig
from yeoman_shared.telemetry import InMemoryTelemetry


def service(bus):
    return OrchestratorService(bus=bus, orchestrator=None, typing_adapter=AsyncMock(),
                               telemetry=InMemoryTelemetry(), memory=None)


def notes():
    return QueueMemoryNotesCaptureIntent(channel='whatsapp', chat_id='group@g.us',
        sender_id='10001', message_id='synthetic', content='synthetic', is_group=True,
        mode='adaptive', batch_interval_seconds=1800, batch_max_messages=100)


@pytest.mark.asyncio
async def test_retired_capture_does_not_lose_following_reply():
    bus = AsyncMock()
    await service(bus)._dispatch_intents([notes(), SendOutboundIntent(
        event=OutboundEvent(channel='whatsapp', chat_id='group@g.us', content='reply'))])
    assert bus.publish_outbound.call_args.args[0].content == 'reply'


@pytest.mark.asyncio
async def test_failed_intent_does_not_abort_remaining_intents():
    bus = AsyncMock()
    bus.publish_outbound.side_effect = [RuntimeError('synthetic failure'), None]
    await service(bus)._dispatch_intents([SendOutboundIntent(event=OutboundEvent(
        channel='telegram', chat_id='synthetic', content=text)) for text in ('first', 'second')])
    assert bus.publish_outbound.call_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['idea', 'backlog'])
async def test_stale_manual_intent_refuses_explicitly_without_crashing(kind):
    bus = AsyncMock()
    await service(bus)._dispatch_intents([RecordManualMemoryIntent(channel='whatsapp',
        chat_id='synthetic', sender_id='10001', content='synthetic', entry_kind=kind)])
    assert 'not available after the history migration' in bus.publish_outbound.call_args.args[0].content


@pytest.mark.asyncio
@pytest.mark.parametrize('accepted', [False, True])
async def test_retired_orchestrator_never_emits_notes(accepted):
    class Policy:
        def evaluate(self, event):
            return PolicyDecision(accept_message=accepted, should_respond=False,
                allowed_tools=frozenset(), reason='synthetic', notes_enabled=True,
                notes_allow_blocked_senders=True)
    orchestrator = Orchestrator(policy=Policy(), responder=AsyncMock(),
        reply_context_window_limit=0, reply_context_line_max_chars=100,
        history_secondary_config=HistoryConfig(legacyWritersDisabled=True))
    intents = await orchestrator.handle(InboundEvent(channel='whatsapp', chat_id='group@g.us',
        sender_id='10001', content='synthetic', message_id='m', is_group=True))
    assert not any(isinstance(i, QueueMemoryNotesCaptureIntent) for i in intents)


@pytest.mark.asyncio
@pytest.mark.parametrize('text', ['[idea] synthetic', '[backlog] synthetic'])
async def test_retired_idea_command_refuses_without_success_reaction(text):
    class Policy:
        def evaluate(self, event):
            return PolicyDecision(accept_message=True, should_respond=True,
                allowed_tools=frozenset(), reason='synthetic')
    orchestrator = Orchestrator(policy=Policy(), responder=AsyncMock(),
        reply_context_window_limit=0, reply_context_line_max_chars=100,
        history_secondary_config=HistoryConfig(legacyWritersDisabled=True))
    intents = await orchestrator.handle(InboundEvent(channel='whatsapp', chat_id='synthetic',
        sender_id='10001', content=text, message_id='m'))
    assert not any(isinstance(i, RecordManualMemoryIntent) for i in intents)
    outbound = [i for i in intents if isinstance(i, SendOutboundIntent)]
    assert len(outbound) == 1
    assert 'not available after the history migration' in outbound[0].event.content
    assert not outbound[0].event.metadata.get('reaction_only')


@pytest.mark.asyncio
async def test_bootstrap_retired_runtime_completes_six_synthetic_turns(tmp_path, monkeypatch):
    import json
    import sqlite3
    from unittest.mock import MagicMock

    from loguru import logger
    from yeoman_gateway.app.bootstrap import build_gateway_runtime
    from yeoman_gateway.bus.queue import MessageBus
    from yeoman_gateway.knowledge._history_upgrade import upgrade_history_knowledge
    from yeoman_gateway.policy.engine import PolicyEngine
    from yeoman_gateway.policy.schema import PolicyConfig
    from yeoman_gateway.providers.base import LLMResponse
    from yeoman_shared.config.schema import Config

    from tests.gateway.test_history_knowledge_upgrade import v2
    from tests.gateway.test_history_writer_retirement import configure

    root = configure(monkeypatch, tmp_path, False)
    target = tmp_path / 'v3.db'
    upgrade_history_knowledge(source=v2(tmp_path / 'v2.db'), target=target)
    with sqlite3.connect(target) as db:
        db.execute("INSERT INTO knowledge_history_capture_state VALUES ('handover',?,1)",
            (json.dumps({'version': 1, 'generation': 1, 'sources': [], 'pending': [],
                         'processed': [], 'legacy_boundary': [0, '']}),))
    config = Config(history={'liveProjectionEnabled': True, 'legacyWritersDisabled': True,
        'readers': {k: True for k in ('participation', 'whatsapp', 'responder', 'tools', 'secondary', 'knowledge')}},
        knowledge={'enabled': True, 'db_path': str(target), 'capture_enabled': True},
        processing={'enabled': False}, security={'enabled': False}, personaEvolution={'enabled': False})
    workspace = root / 'workspace'
    workspace.mkdir()
    policy = PolicyConfig.model_validate({'owners': {'whatsapp': ['10001@s.whatsapp.net']},
        'defaults': {'allowedTools': {'mode': 'allowlist', 'tools': []}},
        'memoryNotes': {'enabled': True, 'defaults': {'groupsEnabled': True, 'dmsEnabled': True}},
        'channels': {'whatsapp': {'default': {'whoCanTalk': {'mode': 'everyone'},
            'whenToReply': {'mode': 'all'}}}}})
    policy_path = root / 'synthetic-policy.json'
    policy_path.write_text(policy.model_dump_json(by_alias=True))
    provider = MagicMock()
    provider.get_default_model.return_value = 'synthetic'
    provider.chat = AsyncMock(return_value=LLMResponse(content='synthetic reply'))
    # Keep provider routing synthetic while using the real responder and pipeline.
    monkeypatch.setattr('yeoman_gateway.providers.factory.ProviderFactory.create_chat_provider',
                        lambda *args, **kwargs: provider)
    monkeypatch.setattr('yeoman_gateway.knowledge._capture_worker.StatementExtractor',
                        lambda **kwargs: MagicMock())
    bus = MessageBus()
    runtime = build_gateway_runtime(config=config, provider=provider,
        policy_engine=PolicyEngine(policy, workspace=workspace), policy_path=policy_path,
        workspace=workspace, bus=bus)
    failures = []
    log_id = logger.add(lambda msg: failures.append(str(msg)), level='ERROR')
    try:
        assert runtime.memory is runtime.contacts is runtime.chat_registry is runtime.inbound_archive is None
        projector = runtime.history_projector
        from yeoman_gateway.history.project import project
        project([runtime.channels.raw_archive.root], projector.db_path,
                publish_lineage_root=runtime.channels.raw_archive.root)
        await projector.start()
        if projector._startup_task is not None:
            await projector._startup_task
        assert projector.health()['status'] == 'ready'
        completed = []
        original_finish = runtime.orchestrator._finish_direct_event
        monkeypatch.setattr(runtime.orchestrator, '_finish_direct_event',
            lambda event: (original_finish(event), completed.append(event.message_id)))
        cases = [('whatsapp', '10001@s.whatsapp.net', 'hello', False),
                 ('whatsapp', 'synthetic@g.us', 'group hello', True),
                 ('telegram', '10001', 'telegram hello', False),
                 ('whatsapp', '10001@s.whatsapp.net', '/new', False),
                 ('whatsapp', '10001@s.whatsapp.net', '[idea] synthetic manual memory', False),
                 ('whatsapp', '10001@s.whatsapp.net', '[backlog] synthetic backlog', False)]
        for index, (channel, chat, text, group) in enumerate(cases):
            await runtime.orchestrator._process_message(InboundEvent(channel=channel,
                chat_id=chat, sender_id='10001@s.whatsapp.net', content=text,
                message_id=f'synthetic-{index}', is_group=group))
        assert completed == [f'synthetic-{i}' for i in range(6)]
        assert not failures
        messages = []
        while not bus.outbound.empty():
            messages.append(bus.outbound.get_nowait())
        assert sum('not available after the history migration' in m.content for m in messages) == 2
        assert sum(m.content == 'synthetic reply' for m in messages) == 3
        assert runtime.responder.sessions.operational_store.boundary(
            channel='whatsapp', chat_id='10001@s.whatsapp.net') is not None
    finally:
        logger.remove(log_id)
        await runtime.responder.aclose()
        await runtime.history_projector.stop()
        for obj in (runtime.processing, runtime.responder.knowledge, runtime.channels.document_cache,
                    runtime.responder.sessions.operational_store):
            if obj is not None and hasattr(obj, 'close'):
                obj.close()


@pytest.mark.asyncio
async def test_dormant_projection_keeps_legacy_source_registration(tmp_path, monkeypatch):
    from unittest.mock import MagicMock

    from yeoman_gateway.app.bootstrap import build_gateway_runtime
    from yeoman_gateway.bus.queue import MessageBus
    from yeoman_shared.config.schema import Config

    from tests.gateway.test_history_knowledge_upgrade import v2
    from tests.gateway.test_history_writer_retirement import configure

    configure(monkeypatch, tmp_path, False)
    config = Config(history={'liveProjectionEnabled': True},
        knowledge={'enabled': True, 'db_path': str(v2(tmp_path / 'legacy-knowledge.db'))},
        processing={'enabled': False}, security={'enabled': False}, personaEvolution={'enabled': False})
    provider = MagicMock()
    provider.get_default_model.return_value = 'synthetic'
    runtime = build_gateway_runtime(config=config, provider=provider, policy_engine=None,
        policy_path=None, workspace=tmp_path / 'workspace', bus=MessageBus())
    try:
        assert runtime.history_projector is not None
        assert runtime.responder.knowledge._store.schema_version == 2
        from yeoman_gateway.history.project import project
        project([runtime.channels.raw_archive.root], runtime.history_projector.db_path,
                publish_lineage_root=runtime.channels.raw_archive.root)
        await runtime.history_projector.start()
        if runtime.history_projector._startup_task is not None:
            await runtime.history_projector._startup_task
        assert runtime.history_projector.health()['status'] == 'ready'
        sink = runtime.channels.processing_signals
        event_id = sink.capture('message', {'chatJid': '10001@s.whatsapp.net',
            'messageId': 'native-staged', 'senderId': '10001@s.whatsapp.net',
            'text': 'Synthetic staged source', 'timestamp': 1_791_000_000})
        # The selected-reader hook is inert here; legacy registration already proved it.
        await sink.register_observed_source_live(event_id)
        authority = runtime.processing.get_event_source_authority(event_id)
        assert authority is not None and authority['audience_status'] == 'author_only'
    finally:
        await runtime.responder.aclose()
        await runtime.history_projector.stop()
        for obj in (runtime.inbound_archive, runtime.chat_registry, runtime.contacts,
                    runtime.memory, runtime.processing, runtime.responder.knowledge,
                    runtime.channels.document_cache):
            if obj is not None and hasattr(obj, 'close'):
                obj.close()
