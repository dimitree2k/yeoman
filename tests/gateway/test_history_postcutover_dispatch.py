"""Synthetic dispatch isolation and live runtime continuity witnesses."""
from unittest.mock import AsyncMock

import pytest
from history_turn_fixtures import Projector
from yeoman_gateway.app.bootstrap import OrchestratorService
from yeoman_gateway.core.intents import SendOutboundIntent
from yeoman_gateway.core.models import InboundEvent, OutboundEvent, PolicyDecision
from yeoman_gateway.core.orchestrator import Orchestrator
from yeoman_shared.telemetry import InMemoryTelemetry


def service(bus):
    return OrchestratorService(bus=bus, orchestrator=None, typing_adapter=AsyncMock(),
                               telemetry=InMemoryTelemetry())


@pytest.mark.asyncio
async def test_failed_intent_does_not_abort_remaining_intents():
    bus = AsyncMock()
    bus.publish_outbound.side_effect = [RuntimeError('synthetic failure'), None]
    await service(bus)._dispatch_intents([SendOutboundIntent(event=OutboundEvent(
        channel='telegram', chat_id='synthetic', content=text)) for text in ('first', 'second')])
    assert bus.publish_outbound.call_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('text', ['[idea] synthetic', '[backlog] synthetic'])
async def test_retired_idea_command_refuses_without_success_reaction(text):
    from yeoman_gateway.history.context import history_turn

    class Policy:
        def evaluate(self, event):
            return PolicyDecision(accept_message=True, should_respond=True,
                allowed_tools=frozenset(), reason='synthetic')
    orchestrator = Orchestrator(policy=Policy(), responder=AsyncMock(),
        reply_context_window_limit=0, reply_context_line_max_chars=100)
    projector = Projector()
    async with history_turn(projector):
        intents = await orchestrator.handle(InboundEvent(channel='whatsapp', chat_id='synthetic',
            sender_id='10001', content=text, message_id='m'))
    projector.close()
    outbound = [i for i in intents if isinstance(i, SendOutboundIntent)]
    assert len(outbound) == 1
    assert 'not available after the history migration' in outbound[0].event.content
    assert not outbound[0].event.metadata.get('reaction_only')


@pytest.mark.asyncio
async def test_bootstrap_retired_runtime_completes_six_synthetic_turns(tmp_path, monkeypatch):
    from unittest.mock import MagicMock

    from loguru import logger
    from yeoman_gateway.app.bootstrap import build_gateway_runtime
    from yeoman_gateway.bus.queue import MessageBus
    from yeoman_gateway.policy.engine import PolicyEngine
    from yeoman_gateway.policy.schema import PolicyConfig
    from yeoman_gateway.providers.base import LLMResponse
    from yeoman_shared.config.schema import Config

    config = Config(
        knowledge={"enabled": False},
        processing={'enabled': False}, security={'enabled': False}, personaEvolution={'enabled': False})
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    policy = PolicyConfig.model_validate({'owners': {'whatsapp': ['10001@s.whatsapp.net']},
        'defaults': {'allowedTools': {'mode': 'allowlist', 'tools': []}},
        'memoryNotes': {'enabled': True, 'defaults': {'groupsEnabled': True, 'dmsEnabled': True}},
        'channels': {'whatsapp': {'default': {'whoCanTalk': {'mode': 'everyone'},
            'whenToReply': {'mode': 'all'}}}}})
    policy_path = tmp_path / 'synthetic-policy.json'
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
        assert runtime.history_projector is not None
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

