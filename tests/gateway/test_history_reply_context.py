import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from history_turn_fixtures import Projector
from yeoman_gateway.app.bootstrap import OrchestratorService
from yeoman_gateway.core.intents import SendOutboundIntent
from yeoman_gateway.core.models import InboundEvent, OutboundEvent
from yeoman_gateway.history.queries import HistoryQueries


@pytest.fixture
async def reply_case(tmp_path, monkeypatch):
    from yeoman_gateway.adapters.responder_llm import LLMResponder
    from yeoman_gateway.bus.queue import MessageBus
    from yeoman_gateway.core.orchestrator import Orchestrator
    from yeoman_gateway.core.pipeline import Pipeline
    from yeoman_gateway.history.context import current_history_snapshot, history_turn
    from yeoman_gateway.pipeline.contacts import ContactsMiddleware
    from yeoman_gateway.pipeline.reply_context import ReplyContextMiddleware
    from yeoman_gateway.providers.base import LLMProvider, LLMResponse
    from yeoman_gateway.session.manager import SessionManager

    class ReplyCase(Projector):
        def __init__(self):
            super().__init__()
            self.reply_snapshot_ids = []
            self.prompt_transcript = ''
            self.responder_acquisitions = 0
            self.legacy_history_reads = 0
            self.bus = SimpleNamespace(publish_outbound=AsyncMock(), publish_reaction=AsyncMock())

        async def run_voice_reply(self):
            self.add('voice', text='[Voice Message]')
            async with history_turn(self) as admission:
                assert HistoryQueries(admission).native_message(chat_id='a@g.us', native_id='voice')['media_json'] is None
            # This derived observation commits only after the admission lease closes.
            self.db.execute("UPDATE messages SET media_json=? WHERE message_id='voice'", ('{"transcript":{"text":"synthetic transcript"}}',))
            self.db.commit()
            self.add('prior', text='prior synthetic text')
            event = InboundEvent(channel='whatsapp', chat_id='a@g.us', sender_id='sender',
                content='synthetic transcript', message_id='voice', is_group=True)
            await self.service._process_message(event)
            assert self.bus.publish_outbound.call_count == 1
            assert self.bus.publish_outbound.call_args.args[0].metadata['history_generation'] == self.generation

    case = ReplyCase()
    sessions = SessionManager(tmp_path, sessions_dir=tmp_path / 'sessions', history_selected=True,
                              legacy_history_disabled=True)
    def legacy_read(*args, **kwargs):
        case.legacy_history_reads += 1
        raise AssertionError('legacy history read')
    monkeypatch.setattr(sessions, '_load', legacy_read)

    class Provider(LLMProvider):
        async def chat(self, **kwargs):
            from yeoman_gateway.agent.tools.recall_conversation import RecallConversationTool
            from yeoman_gateway.processing.tool_context import current_tool_context
            context = current_tool_context()
            snapshot = current_history_snapshot()
            case.reply_snapshot_ids.append(id(context.history_snapshot))
            assert context.history_snapshot is snapshot
            assert 'synthetic transcript' in await RecallConversationTool(sessions).execute(query='synthetic')
            assert 'synthetic transcript' in str(kwargs['messages'])
            return LLMResponse(content='synthetic answer')

        def get_default_model(self):
            return 'synthetic/model'

    responder = LLMResponder(provider=Provider(), workspace=tmp_path, bus=MessageBus(), session_manager=sessions)
    responder._history_selected = True
    responder._history_tools_selected = True
    original = responder.context.build_messages
    def build_messages(**kwargs):
        case.prompt_transcript = kwargs['current_message']
        case.reply_snapshot_ids.append(id(current_history_snapshot()))
        return original(**kwargs)
    monkeypatch.setattr(responder.context, 'build_messages', build_messages)

    async def generate(ctx, next):
        before = case.acquisitions
        reply = await responder._generate(session_key='opaque', channel='whatsapp', chat_id=ctx.event.chat_id,
            content=ctx.event.content, sender_id=ctx.event.sender_id, media=(),
            metadata={'message_id': ctx.event.message_id}, allowed_tools=set(), persona_text=None)
        case.responder_acquisitions += case.acquisitions - before
        ctx.intents.append(SendOutboundIntent(event=OutboundEvent(channel='whatsapp', chat_id=ctx.event.chat_id, content=reply)))
        await next(ctx)

    orchestrator = object.__new__(Orchestrator)
    orchestrator._history_reply_selected = True
    orchestrator._pipeline = Pipeline([ContactsMiddleware(history_selected=True), ReplyContextMiddleware(history_selected=True), generate])
    case.service = OrchestratorService(bus=case.bus, orchestrator=orchestrator,
        typing_adapter=AsyncMock(), telemetry=SimpleNamespace(), memory=None,
        history_projector=case, history_selected=True)
    yield case
    await responder.aclose()
    case.close()


async def test_final_reply_snapshot_includes_enrichment_and_is_shared_by_tools(reply_case):
    await reply_case.run_voice_reply()
    assert reply_case.prompt_transcript == 'synthetic transcript'
    assert len(set(reply_case.reply_snapshot_ids)) == 1
    assert reply_case.responder_acquisitions == 0
    assert reply_case.legacy_history_reads == 0
    assert reply_case.live_snapshot_count == 0


async def test_reply_pause_before_effect_discards_draft_without_fallback(reply_case):
    from yeoman_gateway.history.context import current_history_snapshot
    async def handle(event):
        assert current_history_snapshot() is not None
        reply_case.status = 'rebuilding'
        return [SendOutboundIntent(event=OutboundEvent(channel='whatsapp', chat_id='a@g.us', content='draft'))]
    reply_case.service._orchestrator.handle = handle
    await reply_case.service._process_message(InboundEvent(channel='whatsapp', chat_id='a@g.us', sender_id='sender', content='synthetic'))
    reply_case.bus.publish_outbound.assert_not_called()
    reply_case.bus.publish_reaction.assert_not_called()
    assert reply_case.legacy_history_reads == 0
    assert reply_case.live_snapshot_count == 0


@pytest.mark.parametrize('failure', [RuntimeError, asyncio.CancelledError])
async def test_final_scope_closes_on_exception_and_cancel(reply_case, failure):
    from yeoman_gateway.history.context import current_history_snapshot
    reply_case.service._orchestrator.handle = AsyncMock(side_effect=failure())
    if failure is asyncio.CancelledError:
        with pytest.raises(asyncio.CancelledError):
            await reply_case.service._process_message(InboundEvent(channel='whatsapp', chat_id='a@g.us', sender_id='sender', content='synthetic'))
    else:
        await reply_case.service._process_message(InboundEvent(channel='whatsapp', chat_id='a@g.us', sender_id='sender', content='synthetic'))
    assert reply_case.live_snapshot_count == 0
    assert current_history_snapshot() is None
