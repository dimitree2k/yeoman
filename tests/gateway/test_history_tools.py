import asyncio
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from history_turn_fixtures import FileProjector, Projector
from test_a2a_invoke import _AliasPolicy, _input, _invoke
from yeoman_gateway.agent.tools.recall_conversation import RecallConversationTool
from yeoman_gateway.ipc.a2a_invoke import resolve_whatsapp_recipient
from yeoman_gateway.processing.tool_context import (
    ToolInvocationContext,
    reset_tool_context,
    set_tool_context,
)


async def test_concurrent_tool_turns_keep_snapshot_and_chat_separate():
    from yeoman_gateway.history.context import history_turn
    p = Projector()
    p.add('a', text='synthetic alpha')
    p.add('b', chat='b@g.us', text='synthetic beta')
    tool = RecallConversationTool(SimpleNamespace(get_or_create=Mock(side_effect=AssertionError('legacy'))))
    both = asyncio.Event()
    entered = 0
    async def run(chat):
        nonlocal entered
        async with history_turn(p) as snapshot:
            token = set_tool_context(ToolInvocationContext(channel='whatsapp', chat_id=chat, history_snapshot=snapshot))
            entered += 1
            if entered == 2:
                both.set()
            try:
                await asyncio.wait_for(both.wait(), 30)
                return id(snapshot), await tool.execute(query='synthetic')
            finally:
                reset_tool_context(token)
    a, b = await asyncio.gather(run('a@g.us'), run('b@g.us'))
    assert a[0] != b[0]
    assert 'alpha' in a[1] and 'beta' not in a[1]
    assert 'beta' in b[1] and 'alpha' not in b[1]
    assert p.live_snapshot_count == 0
    p.close()


class ScopedKnowledge:
    def __init__(self, p):
        self.p = p
        self.released = True
        self.generations = []
        self.history_source_ledger = SimpleNamespace()
        self._legacy_authority = SimpleNamespace()

    @contextmanager
    def history_scope(self, queries, sources):
        assert sources.ledger is self.history_source_ledger
        self.queries = queries
        try:
            yield
        finally:
            self.queries = None

    def delivery_identifiers_for_alias(self, alias, *, channel, scope_key):
        from yeoman_gateway.history.context import current_history_snapshot
        from yeoman_gateway.knowledge.models import GLOBAL_SCOPE_KEY
        assert scope_key == GLOBAL_SCOPE_KEY and channel == 'whatsapp'
        assert current_history_snapshot() is self.queries.snapshot
        self.generations.append(current_history_snapshot().generation)
        if not self.released or alias != 'synthetic-contact':
            return ()
        contact = self.queries.contact('person')
        if not contact:
            return ()
        rows = self.queries.snapshot.connection.execute("SELECT value FROM identifier_history WHERE contact_id=? AND kind='pn_jid' AND ended_ms IS NULL", (contact['contact_id'],)).fetchall()
        return tuple(SimpleNamespace(value=row[0]) for row in rows)


def contact_case(tmp_path):
    p = FileProjector(tmp_path)
    p.db.execute("INSERT INTO contacts VALUES ('person','person',NULL,'synthetic','confirmed',NULL,'[]')")
    p.db.execute("INSERT INTO identifier_history(contact_id,channel,kind,value,strength,evidence,source_refs) VALUES ('person','whatsapp','pn_jid','491000000001@s.whatsapp.net','strong','owner_attested','[]')")
    p.db.commit()
    return p, ScopedKnowledge(p)


async def test_a2a_recipient_resolution_reopens_after_replace_and_rechecks_before_send(tmp_path):
    p, knowledge = contact_case(tmp_path)
    policy = _AliasPolicy()
    policy.evaluate = lambda _: SimpleNamespace(accept_message=True, should_respond=True, allowed_tools={'message'})
    def resolver(kind, alias):
        return resolve_whatsapp_recipient(kind, alias, policy_adapter=policy, knowledge=knowledge)
    kwargs = dict(input=_input(recipient={'type': 'contact', 'alias': 'synthetic-contact'}), policy=policy,
                  resolver=resolver, history_projector=p, history_knowledge=knowledge)
    result, _, _, effects = await _invoke(**kwargs)
    assert result['status'] == 'completed'
    assert effects.calls[-1]['chat_id'] == '491000000001@s.whatsapp.net'
    before_inode = p.path.stat().st_ino
    p.replace('491000000002@s.whatsapp.net')
    assert p.path.stat().st_ino != before_inode
    result, _, _, effects = await _invoke(**kwargs)
    assert effects.calls[-1]['chat_id'] == '491000000002@s.whatsapp.net'
    assert knowledge.generations == [1, 2]
    def replace_before_send(kind, alias):
        target = resolver(kind, alias)
        p.replace('491000000003@s.whatsapp.net')
        return target
    result, _, _, effects = await _invoke(**{**kwargs, 'resolver': replace_before_send})
    assert effects.calls == []
    assert result['status'] == 'rejected'
    assert p.live_snapshot_count == 0
    p.close()


async def test_a2a_pause_and_retired_contacts_fail_closed(tmp_path):
    p, knowledge = contact_case(tmp_path)
    policy = _AliasPolicy()
    policy.evaluate = lambda _: SimpleNamespace(accept_message=True, should_respond=True, allowed_tools={'message'})
    legacy = SimpleNamespace(store=SimpleNamespace(search_by_alias=Mock(side_effect=AssertionError('legacy'))))
    def resolver(kind, alias):
        return resolve_whatsapp_recipient(kind, alias, policy_adapter=policy, knowledge=knowledge, contacts_service=legacy)
    kwargs = dict(input=_input(recipient={'type': 'contact', 'alias': 'synthetic-contact'}), policy=policy,
                  resolver=resolver, history_projector=p, history_knowledge=knowledge)
    p.status = 'rebuilding'
    result, _, _, effects = await _invoke(**kwargs)
    assert effects.calls == [] and knowledge.generations == []
    p.status = 'ready'
    result, _, _, effects = await _invoke(**{**kwargs, 'peer': 'unauthorized'})
    assert effects.calls == [] and p.acquisitions == 0
    knowledge.released = False
    result, _, _, effects = await _invoke(**kwargs)
    assert effects.calls == []
    legacy.store.search_by_alias.assert_not_called()
    assert set(tmp_path.iterdir()) == {p.path} and p.live_snapshot_count == 0
    p.close()


@pytest.mark.parametrize('kind', ['send_text', 'send_media'])
def test_history_mentions_ignore_bridge_cache_and_refuse_unresolved_lid(kind):
    from yeoman_shared.whatsapp_protocol import PROTOCOL_VERSION, valid_history_mentions
    assert PROTOCOL_VERSION == 6
    assert valid_history_mentions({'historyMentionsResolved': True, 'mentions': ['491000000001@s.whatsapp.net']})
    assert not valid_history_mentions({'historyMentionsResolved': True, 'mentions': ['100000000001@lid']})
    assert not valid_history_mentions({'historyMentionsResolved': True, 'mentions': ['100000000001']})
    assert not valid_history_mentions({'historyMentionsResolved': 'true'})
    assert valid_history_mentions({'mentions': ['100000000001@lid']})


async def test_queued_history_generation_is_checked_after_transport_wait():
    from unittest.mock import AsyncMock

    from yeoman_gateway.channels.whatsapp import _SEND_HISTORY_GENERATION, WhatsAppChannel
    from yeoman_gateway.history.live import HistoryPaused
    p = Projector()
    channel = object.__new__(WhatsAppChannel)
    channel._history_projector = p
    channel._ws = SimpleNamespace(send=AsyncMock())
    channel.config = SimpleNamespace(max_payload_bytes=100000)
    channel._pending = {}
    channel._send_lock = asyncio.Lock()
    channel._require_token = lambda: 'synthetic-token'
    async def archive(*args, **kwargs):
        p.generation += 1
    channel._raw_archive_outbound = archive
    token = _SEND_HISTORY_GENERATION.set(p.generation)
    try:
        with pytest.raises(HistoryPaused):
            await channel._send_command('send_text', {'to': 'a@g.us', 'text': 'synthetic'}, 30)
        channel._ws.send.assert_not_called()
        assert not channel._pending
    finally:
        _SEND_HISTORY_GENERATION.reset(token)
        p.close()


async def test_background_subagent_clears_selected_lease_and_keeps_dormant_context():
    from contextvars import ContextVar

    from yeoman_gateway.agent.subagent import SubagentManager
    from yeoman_gateway.history.context import current_history_snapshot, history_turn
    from yeoman_gateway.processing.tool_context import current_tool_context
    marker = ContextVar('synthetic-marker', default=False)
    manager = object.__new__(SubagentManager)
    manager._running_tasks = {}
    observed = []
    async def run(*args):
        context = current_tool_context()
        observed.append((current_history_snapshot(), context.history_snapshot, marker.get()))
    manager._run_subagent = run
    p = Projector()
    token = marker.set(True)
    try:
        async with history_turn(p) as snapshot:
            tool_token = set_tool_context(ToolInvocationContext(channel='whatsapp', chat_id='a@g.us', history_snapshot=snapshot))
            try:
                await manager.spawn('synthetic task')
                tasks = tuple(manager._running_tasks.values())
            finally:
                reset_tool_context(tool_token)
        await asyncio.wait_for(asyncio.gather(*tasks), 30)
        assert observed == [(None, None, False)] and p.live_snapshot_count == 0
        tool_token = set_tool_context(ToolInvocationContext(channel='whatsapp', chat_id='a@g.us'))
        try:
            await manager.spawn('synthetic dormant task')
            tasks = tuple(manager._running_tasks.values())
        finally:
            reset_tool_context(tool_token)
        await asyncio.wait_for(asyncio.gather(*tasks), 30)
        assert observed[-1] == (None, None, True)
    finally:
        marker.reset(token)
        p.close()


@pytest.mark.parametrize('has_mention', [False, True])
async def test_scope_less_bus_send_resolves_mentions_and_closes_before_transport(has_mention):
    import json
    from unittest.mock import AsyncMock

    from yeoman_gateway.bus.events import OutboundMessage
    from yeoman_gateway.bus.queue import MessageBus
    from yeoman_gateway.channels.manager import ChannelManager
    from yeoman_gateway.channels.whatsapp import WhatsAppChannel
    from yeoman_gateway.history.context import current_history_snapshot
    from yeoman_shared.config.schema import WhatsAppConfig
    p = Projector()
    phone, lid = '491000000001@s.whatsapp.net', '100000000001@lid'
    p.db.execute("INSERT INTO contacts VALUES ('person','person',NULL,'synthetic','confirmed',NULL,'[]')")
    for kind, value in (('pn_jid', phone), ('lid', lid)):
        p.db.execute("INSERT INTO identifier_history(contact_id,channel,kind,value,strength,evidence,source_refs) VALUES ('person','whatsapp',?,?,'strong','owner_attested','[]')", (kind, value))
    p.db.execute("INSERT INTO message_events(event_id,channel,chat_id,kind,occurred_ms,time_certainty,payload_json,provenance,actor_basis,source_refs) VALUES ('roster','whatsapp','a@g.us','member_snapshot',1700000000000,'native',?,'native','unknown','[]')", (json.dumps({'complete': True, 'participants': [[phone, lid]]}),))
    p.db.commit()
    bus = MessageBus()
    channel = WhatsAppChannel(WhatsAppConfig(bridge_token='synthetic-token'), bus)
    channel._history_projector = p
    channel._history_mentions_selected = True
    channel._connected = True
    channel._stop_typing = AsyncMock()
    channel._raw_archive_outbound = AsyncMock()
    sent = []
    completed = asyncio.Event()
    original_send = channel.send
    async def send(message):
        try:
            assert current_history_snapshot() is None
            return await original_send(message)
        finally:
            completed.set()
    channel.send = send
    async def ws_send(encoded):
        assert p.live_snapshot_count == 0 and current_history_snapshot() is None
        frame = json.loads(encoded)
        sent.append(frame['payload'])
        channel._pending[frame['requestId']].set_result({'providerMessageId': 'synthetic-sent'})
    channel._ws = SimpleNamespace(send=ws_send)
    manager = object.__new__(ChannelManager)
    manager.bus, manager.channels = bus, {'whatsapp': channel}
    dispatcher = asyncio.create_task(manager._dispatch_outbound())
    text = 'synthetic @100000000001@lid' if has_mention else 'synthetic notice'
    message = OutboundMessage(channel='whatsapp', chat_id='a@g.us', content=text)
    try:
        await bus.publish_outbound(message)
        await asyncio.wait_for(completed.wait(), 30)
        assert len(sent) == 1
        assert sent[0]['historyMentionsResolved'] is True
        assert sent[0].get('mentions', []) == ([phone] if has_mention else [])
        assert sent[0]['text'] == ('synthetic @491000000001' if has_mention else text)
        assert p.acquisitions == 1 and p.live_snapshot_count == 0
        completed.clear()
        p.status = 'rebuilding'
        await bus.publish_outbound(message)
        await asyncio.wait_for(completed.wait(), 30)
        assert len(sent) == 1 and p.live_snapshot_count == 0
    finally:
        dispatcher.cancel()
        await dispatcher
        p.close()
