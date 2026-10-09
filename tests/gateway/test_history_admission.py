import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from history_turn_fixtures import Projector
from yeoman_gateway.channels.whatsapp import WhatsAppChannel
from yeoman_gateway.history.queries import HistoryQueries


async def test_admission_barrier_covers_other_layer1_destinations(tmp_path):
    from yeoman_gateway.history.context import current_history_snapshot, history_turn
    from yeoman_gateway.history.live import HistoryProjector
    from yeoman_shared.raw_archive.writer import RawArchive

    root = tmp_path / 'raw'
    root.mkdir()
    archive = RawArchive(root, spool=tmp_path / 'spool', status_path=tmp_path / 'status.json')
    from yeoman_gateway.history.project import project
    project([root], tmp_path / 'history.db', publish_lineage_root=root)
    p = HistoryProjector(root, tmp_path / 'history.db', archive)
    await p.start()
    await p._startup_task
    try:
        # A distinct destination committed without a notification must be included.
        from yeoman_shared.raw_archive.writer import RawEvent
        archive.append(RawEvent('whatsapp', 'membership_snapshot', 'in', {
            'chatJid': 'other@g.us', 'participants': [], 'snapshotAtMs': 1700000000000,
        }))
        from yeoman_shared.raw_archive.records import enumerate_committed
        committed = enumerate_committed(root)
        async with history_turn(p) as snapshot:
            assert snapshot.sources == committed
            assert current_history_snapshot() is snapshot
            assert all(boundary.end_offset > 0 for boundary in snapshot.sources)
        assert not p._reader._snapshots
    finally:
        await p.stop()


async def test_admission_and_final_reply_do_not_share_pre_enrichment_snapshot():
    from yeoman_gateway.history.context import current_history_snapshot, history_turn
    p = Projector()
    channel = object.__new__(WhatsAppChannel)
    channel._history_projector = p
    channel._history_selected = True
    channel._history_knowledge = None
    channel._is_duplicate = Mock(return_value=False)
    channel._processing_request = Mock(return_value=object())
    admission_ids = []
    async def admit(_request):
        admission_ids.append(id(current_history_snapshot()))
        return SimpleNamespace(denied=True, reason='synthetic', decision=None)
    channel._processing_gate = SimpleNamespace(admit_async=admit)
    channel._archive_inbound_event = Mock()
    event = SimpleNamespace(chat_jid='a@g.us', message_id='m')
    await channel._ingest_inbound_event(event)
    assert admission_ids and admission_ids[0] != id(None)
    assert p.live_snapshot_count == 0
    channel._processing_gate.admit_async = AsyncMock(return_value=None)
    async def enrich(value):
        assert p.live_snapshot_count == 0
        p.add('enriched', text='synthetic transcript')
        async with history_turn(p) as snapshot:
            assert id(snapshot) != admission_ids[0]
            assert HistoryQueries(snapshot).native_message(chat_id='a@g.us', native_id='enriched')['current_text'] == 'synthetic transcript'
        raise asyncio.CancelledError
    channel._enrich_media_event = enrich
    with pytest.raises(asyncio.CancelledError):
        await channel._ingest_inbound_event(event)
    assert p.live_snapshot_count == 0
    p.status = 'rebuilding'
    channel._enrich_media_event = AsyncMock()
    await channel._ingest_inbound_event(event)
    channel._enrich_media_event.assert_not_called()
    assert channel._archive_inbound_event.call_count == 2
    p.close()


def test_reply_anchor_cannot_leak_other_chat_or_deleted_payload():
    from yeoman_gateway.adapters.reply_archive_history import HistoryReplyArchiveAdapter
    p = Projector()
    p.add('same', chat='other@g.us', text='private text')
    snapshot = __import__('yeoman_gateway.history.reader', fromlist=['HistorySnapshot']).HistorySnapshot(1, (), p.db)
    adapter = HistoryReplyArchiveAdapter(HistoryQueries(snapshot))
    assert adapter.lookup_message('whatsapp', 'a@g.us', 'same') is None
    assert adapter.lookup_message_any_chat('whatsapp', 'same', preferred_chat_id='a@g.us') is None
    p.add('deleted', text='old quoted payload')
    p.db.execute("INSERT INTO message_events(event_id,channel,chat_id,target_message_id,kind,payload_json,provenance,time_certainty,actor_basis,source_refs) VALUES ('delete','whatsapp','a@g.us','deleted','delete','{}','native','unknown','unknown','[]')")
    assert adapter.lookup_message('whatsapp', 'a@g.us', 'deleted') is None
    from yeoman_gateway.core.models import InboundEvent
    from yeoman_gateway.pipeline.reply_context import ReplyContextMiddleware
    event = InboundEvent(channel='whatsapp', chat_id='a@g.us', sender_id='synthetic', content='reply', reply_to_message_id='deleted', reply_to_text='old quoted payload')
    resolved, _, _ = ReplyContextMiddleware(archive=adapter)._resolve_reply_context(event)
    assert resolved.reply_to_text is None
    assert 'old quoted payload' not in str(resolved.raw_metadata)
    snapshot.close()
