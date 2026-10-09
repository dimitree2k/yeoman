"""Synthetic regressions for Task 13 review findings."""
import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from history_turn_fixtures import Projector
from yeoman_gateway.history.context import (
    current_history_snapshot,
    history_effect_metadata,
    history_turn,
)
from yeoman_gateway.history.live import HistoryPaused


async def test_selected_contacts_resolve_terminal_string_once():
    from yeoman_gateway.core.models import InboundEvent
    from yeoman_gateway.core.pipeline import Pipeline
    from yeoman_gateway.pipeline.contacts import ContactsMiddleware

    p = Projector()
    p.db.execute("INSERT INTO contacts VALUES ('person','person',NULL,'synthetic','confirmed',NULL,'[]')")
    p.db.execute("INSERT INTO identifier_history(contact_id,channel,kind,value,strength,evidence,source_refs) VALUES ('person','whatsapp','pn_jid','491000000001@s.whatsapp.net','strong','owner_attested','[]')")
    p.db.commit()
    downstream = AsyncMock()
    event = InboundEvent(channel='whatsapp', chat_id='a@g.us', sender_id='491000000001@s.whatsapp.net', content='synthetic', timestamp=datetime.fromtimestamp(1700000000, UTC))
    try:
        async with history_turn(p):
            await Pipeline([ContactsMiddleware(history_selected=True), downstream]).run(event)
        downstream.assert_awaited_once()
        assert downstream.call_args.args[0].event.raw_metadata['contact_id'] == 'person'
    finally:
        p.close()


@pytest.mark.parametrize('paused', [False, True])
async def test_spawned_send_reacquires_after_parent_closed(paused):
    from yeoman_gateway.bus.events import OutboundMessage
    from yeoman_gateway.bus.queue import MessageBus
    from yeoman_gateway.channels.whatsapp import WhatsAppChannel
    from yeoman_shared.config.schema import WhatsAppConfig

    p = Projector()
    phone, lid = '491000000001@s.whatsapp.net', '100000000001@lid'
    p.db.execute("INSERT INTO contacts VALUES ('person','person',NULL,'synthetic','confirmed',NULL,'[]')")
    for kind, value in (('pn_jid', phone), ('lid', lid)):
        p.db.execute("INSERT INTO identifier_history(contact_id,channel,kind,value,strength,evidence,source_refs) VALUES ('person','whatsapp',?,?,'strong','owner_attested','[]')", (kind, value))
    p.db.execute("INSERT INTO message_events(event_id,channel,chat_id,kind,occurred_ms,time_certainty,payload_json,provenance,actor_basis,source_refs) VALUES ('roster','whatsapp','a@g.us','member_snapshot',1700000000000,'native',?,'native','unknown','[]')", (json.dumps({'complete': True, 'participants': [[phone, lid]]}),))
    p.db.commit()
    channel = WhatsAppChannel(WhatsAppConfig(bridge_token='synthetic-token'), MessageBus())
    channel._history_projector = p
    channel._history_mentions_selected = True
    channel._connected = True
    channel._stop_typing = AsyncMock()
    channel._raw_archive_outbound = AsyncMock()
    sent = []
    async def ws_send(encoded):
        assert current_history_snapshot() is None and p.live_snapshot_count == 0
        frame = json.loads(encoded)
        sent.append(frame['payload'])
        channel._pending[frame['requestId']].set_result({'providerMessageId': 'synthetic-sent'})
    channel._ws = SimpleNamespace(send=ws_send)
    release = asyncio.Event()
    async def child():
        await asyncio.wait_for(release.wait(), 30)
        assert current_history_snapshot() is None
        assert history_effect_metadata() == {}
        await channel.send(OutboundMessage(channel='whatsapp', chat_id='a@g.us', content='synthetic @100000000001@lid'))
    try:
        async with history_turn(p) as parent:
            task = asyncio.create_task(child())
        assert parent._closed
        if paused:
            p.status = 'rebuilding'
        release.set()
        if paused:
            with pytest.raises(HistoryPaused):
                await asyncio.wait_for(task, 30)
            assert sent == [] and p.acquisitions == 1
        else:
            await asyncio.wait_for(task, 30)
            assert sent[0]['mentions'] == [phone]
            assert sent[0]['text'] == 'synthetic @491000000001'
            assert p.acquisitions == 2
        assert p.live_snapshot_count == 0
    finally:
        p.close()


async def test_open_stale_scope_still_refuses_effect():
    p = Projector()
    try:
        async with history_turn(p) as snapshot:
            p.generation += 1
            assert current_history_snapshot() is snapshot
            with pytest.raises(HistoryPaused, match='generation_invalidated'):
                history_effect_metadata()
    finally:
        p.close()


@pytest.mark.parametrize('kind', ['burst', 'lull'])
@pytest.mark.parametrize('case', ['disabled', 'ineligible', 'policy_ineligible', 'eligible'])
async def test_observer_scopes_only_eligible_inbound(tmp_path, kind, case):
    from yeoman_gateway.bus.events import InboundObservedEvent
    from yeoman_gateway.consciousness.burst import BurstObserver
    from yeoman_gateway.consciousness.lull import LullObserver
    from yeoman_shared.config.schema import Config

    config = Config()
    config.history.live_projection_enabled = True
    config.history.readers.secondary = True
    config.consciousness.enabled = case != 'disabled'
    config.consciousness.burst_enabled = True
    config.consciousness.lull_enabled = True
    cls = BurstObserver if kind == 'burst' else LullObserver
    observer = cls(config=config, state_path=tmp_path / f'{kind}.json', **{f'on_{kind}': AsyncMock()})
    p = Projector()
    observer._history_projector = p
    observer._is_eligible = AsyncMock(return_value=case != 'policy_ineligible')
    def direct(event):
        assert current_history_snapshot() is not None
        return False
    observer._is_direct_bot_interaction = direct
    event = InboundObservedEvent(channel='whatsapp', chat_id='' if case == 'ineligible' else 'a@g.us', sender_id='synthetic', content='synthetic', timestamp=1700000000)
    try:
        await observer.handle(event)
        assert p.acquisitions == (1 if case == 'eligible' else 0)
        assert observer._is_eligible.await_count == (1 if case in {'eligible', 'policy_ineligible'} else 0)
        assert p.live_snapshot_count == 0
    finally:
        p.close()


@pytest.mark.parametrize('kind', ['burst', 'lull'])
async def test_dormant_observer_keeps_eligibility_order(tmp_path, kind):
    from yeoman_gateway.bus.events import InboundObservedEvent
    from yeoman_gateway.consciousness.burst import BurstObserver
    from yeoman_gateway.consciousness.lull import LullObserver
    from yeoman_shared.config.schema import Config

    config = Config()
    config.consciousness.enabled = True
    config.consciousness.burst_enabled = True
    config.consciousness.lull_enabled = True
    calls = []
    def eligible(channel, chat_id):
        calls.append('eligible')
        return False
    cls = BurstObserver if kind == 'burst' else LullObserver
    observer = cls(config=config, state_path=tmp_path / f'{kind}.json', is_eligible=eligible,
                   **{f'on_{kind}': AsyncMock()})
    def direct(event):
        assert current_history_snapshot() is None
        calls.append('direct')
        return False
    observer._is_direct_bot_interaction = direct
    await observer.handle(InboundObservedEvent(channel='whatsapp', chat_id='a@g.us', sender_id='synthetic', content='synthetic', timestamp=1700000000))
    assert calls == (['direct', 'eligible'] if kind == 'burst' else ['direct'])
