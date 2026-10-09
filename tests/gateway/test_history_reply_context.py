# ruff: noqa: F811
import asyncio
from unittest.mock import AsyncMock

import pytest
from yeoman_gateway.core.intents import SendOutboundIntent
from yeoman_gateway.core.models import InboundEvent, OutboundEvent

from tests.gateway.convhist.consumer_fixtures import reply_case  # noqa: F401


async def test_final_reply_snapshot_includes_enrichment_and_is_shared_by_tools(reply_case):
    reply_case.db.execute("INSERT INTO contacts VALUES ('person','person',NULL,'synthetic','confirmed',NULL,'[]')")
    reply_case.db.execute("INSERT INTO identifier_history(contact_id,channel,kind,value,strength,evidence,source_refs) VALUES ('person','whatsapp','pn_jid','491000000001@s.whatsapp.net','strong','owner_attested','[]')")
    reply_case.db.commit()
    await reply_case.run_voice_reply()
    assert reply_case.resolved_contact_id == 'person'
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
