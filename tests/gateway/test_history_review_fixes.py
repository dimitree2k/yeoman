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

from tests.gateway.history_test_helpers import ProjectedHistory


async def test_selected_contacts_resolve_terminal_string_once(tmp_path):
    from yeoman_gateway.core.models import InboundEvent
    from yeoman_gateway.core.pipeline import Pipeline
    from yeoman_gateway.pipeline.contacts import ContactsMiddleware

    p = ProjectedHistory(
        tmp_path / "history-review-fixes",
        contacts=[dict(identifiers=["491000000001@s.whatsapp.net"], name="synthetic", timestamp=1700000000000)],
    )
    downstream = AsyncMock()
    event = InboundEvent(channel='whatsapp', chat_id='a@g.us', sender_id='491000000001@s.whatsapp.net', content='synthetic', timestamp=datetime.fromtimestamp(1700000000, UTC))
    try:
        async with p.turn() as snapshot:
            from yeoman_gateway.history.queries import HistoryQueries

            expected = HistoryQueries(snapshot).resolve_identifier(event.sender_id, at_ms=1700000000000, time_basis='native')
            assert expected is not None
            await Pipeline([ContactsMiddleware(), downstream]).run(event)
        downstream.assert_awaited_once()
        assert downstream.call_args.args[0].event.raw_metadata['contact_id'] == expected
    finally:
        p.close()


async def test_spawned_send_reacquires_with_projected_roster_after_parent_closed(tmp_path):
    from yeoman_gateway.bus.events import OutboundMessage
    from yeoman_gateway.bus.queue import MessageBus
    from yeoman_gateway.channels.whatsapp import WhatsAppChannel
    from yeoman_shared.config.schema import WhatsAppConfig

    phone, lid = '491000000001@s.whatsapp.net', '100000000001@lid'
    projected = ProjectedHistory(
        tmp_path / 'spawned-send-history',
        groups=[dict(chat_id='a@g.us', participants=[{'phoneJid': phone, 'lid': lid}], timestamp=1700000000000)],
    )
    channel = WhatsAppChannel(WhatsAppConfig(bridge_token='synthetic-token'), MessageBus())
    channel._history_projector = projected
    channel._connected = True
    channel._stop_typing = AsyncMock()
    channel._raw_archive_outbound = AsyncMock()
    sent = []
    async def ws_send(encoded):
        assert current_history_snapshot() is None
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
        async with history_turn(projected) as parent:
            task = asyncio.create_task(child())
        assert parent._closed
        release.set()
        await asyncio.wait_for(task, 30)
        assert sent[0]['mentions'] == [phone]
        assert sent[0]['text'] == 'synthetic @491000000001'
        assert len(projected.snapshots) == 2
        assert all(snapshot._closed for snapshot in projected.snapshots)
    finally:
        projected.close()


async def test_spawned_send_fails_when_reader_pauses_before_child_acquires():
    from yeoman_gateway.bus.events import OutboundMessage
    from yeoman_gateway.bus.queue import MessageBus
    from yeoman_gateway.channels.whatsapp import WhatsAppChannel
    from yeoman_shared.config.schema import WhatsAppConfig

    p = Projector()
    channel = WhatsAppChannel(WhatsAppConfig(bridge_token='synthetic-token'), MessageBus())
    channel._history_projector = p
    channel._connected = True
    channel._stop_typing = AsyncMock()
    channel._raw_archive_outbound = AsyncMock()
    sent = []

    async def ws_send(encoded):
        sent.append(json.loads(encoded)['payload'])

    channel._ws = SimpleNamespace(send=ws_send)
    release = asyncio.Event()

    async def child():
        await asyncio.wait_for(release.wait(), 30)
        await channel.send(OutboundMessage(channel='whatsapp', chat_id='a@g.us', content='plain text'))

    try:
        async with history_turn(p) as parent:
            task = asyncio.create_task(child())
        assert parent._closed
        p.status = 'rebuilding'
        release.set()
        with pytest.raises(HistoryPaused):
            await asyncio.wait_for(task, 30)
        assert sent == [] and p.acquisitions == 1
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
