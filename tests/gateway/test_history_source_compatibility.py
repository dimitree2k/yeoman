import json
import sqlite3
from dataclasses import asdict, replace

import pytest
from yeoman_gateway.history.queries import HistoryQueries
from yeoman_gateway.history.reader import HistorySnapshot
from yeoman_gateway.knowledge.models import KnowledgeError, SourceRef
from yeoman_gateway.knowledge.runtime import RuntimeKnowledgeSources

from tests.gateway.convhist.test_hist_queries import contact, event, identifier, message
from tests.gateway.test_history_knowledge_upgrade import open_service, v2

AUTHOR = 'whatsapp:10001'
MEMBER = 'whatsapp:10002'


@pytest.fixture
def case(tmp_path):
    from yeoman_gateway.history.schema import create
    from yeoman_gateway.knowledge._history_sources import (
        HistoryKnowledgeSources,
        HistorySourceLedger,
    )
    from yeoman_gateway.knowledge._history_upgrade import upgrade_history_knowledge
    source = v2(tmp_path / 'v2.db')
    target = tmp_path / 'v3.db'
    upgrade_history_knowledge(source=source, target=target)
    service = open_service(target, history_mode=True)
    db = sqlite3.connect(':memory:')
    create(db)
    contact(db, 'a')
    contact(db, 'b')
    identifier(db, 'a', '10001@s.whatsapp.net', start=1)
    identifier(db, 'b', '10002@s.whatsapp.net', start=1)
    event(db, 'roster', 'member_snapshot', 10, {'complete': True, 'participants': [
        ['10001@s.whatsapp.net'], ['10002@s.whatsapp.net']]})
    message(db, 'm', ms=100, text='Synthetic A')
    snapshot = HistorySnapshot(1, (), db)
    q = HistoryQueries(snapshot)
    legacy = RuntimeKnowledgeSources()
    ledger = HistorySourceLedger(service._store)
    sources = HistoryKnowledgeSources(q, {}, ledger, legacy)
    yield service, q, sources, db
    snapshot.close()
    service.close()


def old_alias(case):
    from yeoman_gateway.knowledge._history_sources import build_history_source_aliases
    service, q, sources, db = case
    issued = SourceRef('legacy', 7, 'whatsapp', q.message('m')['chat_id'], AUTHOR, 100)
    row = {**asdict(issued), 'author_contact_id': 'a', 'content_fingerprint': q.content_fingerprint('m'), 'source_audience_json': None if q.audience('m').status == 'author_only' else json.dumps([AUTHOR, MEMBER]), 'status': 'active'}
    aliases, counts = build_history_source_aliases(queries=q, legacy_rows=[row], locators={issued.key: ('m',)})
    assert counts['mapped'] == 1
    sources.ledger.persist_aliases(aliases)
    sources.compatibility = aliases
    return issued, row


def test_old_source_mapping_cannot_widen_audience_or_retarget_revision(case):
    from yeoman_gateway.knowledge._history_sources import build_history_source_aliases
    _, q, sources, db = case
    issued, row = old_alias(case)
    assert sources.verify_source_ref(*issued.key) == issued
    assert not sources.verify_source(replace(issued, author_principal='a'))
    event(db, 'later', 'member_add', 200, {'participants': [['10003@s.whatsapp.net']]})
    assert sources.evidence_audience(issued, basis='').members == frozenset({AUTHOR, MEMBER})
    aliases, counts = build_history_source_aliases(queries=q, legacy_rows=[row], locators={issued.key: ('m', 'other')})
    assert not aliases and counts['ambiguous'] == 1
    event(db, 'edit', 'edit', 300, {'text': 'Synthetic B'}, target='m')
    assert sources.verify_source_ref(*issued.key) is None
    assert row['revision'] == 7 and row['event_id'] == 'legacy'


def test_purged_source_never_revives_from_alias(case):
    _, _, sources, db = case
    issued, _ = old_alias(case)
    sources.mark_source_revoked(issued)
    assert sources.verify_source_ref(*issued.key) is None
    assert sources.source_revoked(issued)
    assert sources.ledger.lookup(*issued.key).revoked
    # Reappearing conversion locator cannot remove an explicit Knowledge revocation.
    assert sources.verify_source_ref(*issued.key) is None
    db.execute("DELETE FROM messages WHERE message_id='m'")
    assert sources.verify_source_ref(*issued.key) is None


def test_unchanged_rebuild_keeps_revision(case):
    _, q, sources, _ = case
    first = sources.issue('m')
    q.snapshot.generation += 1
    assert sources.issue('m') == first
    assert sources.verify_source_ref(*first.key) == first


@pytest.mark.asyncio
async def test_edit_return_to_original_allocates_new_revision_between_acquisitions_and_after_restart_rebuild(case, tmp_path):
    await _edit_round(case, tmp_path, coalesced=False)


@pytest.mark.asyncio
async def test_coalesced_edit_round_within_copy_window_keeps_revision(case, tmp_path):
    await _edit_round(case, tmp_path, coalesced=True)


async def _edit_round(case, tmp_path, *, coalesced):
    from yeoman_gateway.history.attestations import make
    from yeoman_gateway.history.live import HistoryProjector
    from yeoman_gateway.history.project import project
    from yeoman_gateway.knowledge._history_sources import (
        HistoryKnowledgeSources,
        HistorySourceLedger,
    )
    from yeoman_shared.raw_archive.records import append_line, dumps
    from yeoman_shared.raw_archive.writer import RawArchive
    service, _, fixture_sources, _ = case
    root = tmp_path / 'synthetic-raw'
    owner = root / 'owner/attestations.jsonl'
    owner.parent.mkdir(parents=True)
    phone = '10001@s.whatsapp.net'
    owner.write_text('\n'.join(json.dumps(record) for record in (
        make('contact', 1, 'synthetic', identifiers=[phone], name='Synthetic'),
        make('identifier', 1, 'synthetic', anchor=phone, identifier=phone, valid_from_ms=1),
    )) + '\n')
    ms = 1791000000000
    def raw(kind, text, at, native_event=None):
        payload = {'chatJid': phone, 'senderId': phone, 'timestamp': at, 'messageId': 'M', 'text': text}
        if native_event:
            payload['nativeEventId'] = native_event
        return {'account': 'synthetic', 'archive_version': 1, 'channel': 'whatsapp', 'chat_id': phone,
                'correlation_id': '', 'direction': 'in', 'kind': kind, 'media': None,
                'native': {'type': kind, 'payload': payload}, 'received_ms': at}
    path = root / 'whatsapp/2026-10.jsonl'
    path.parent.mkdir(parents=True)
    path.write_text(dumps(raw('message', 'Synthetic A', ms)) + '\n')
    db_path = tmp_path / 'history.db'
    project([root], db_path, publish_lineage_root=root)
    archive = RawArchive(root, spool=tmp_path / 'spool', status_path=tmp_path / 'status.json')
    projector = HistoryProjector(root, db_path, archive)
    await projector.start()
    if projector._startup_task is not None:
        await projector._startup_task
    snapshot = None
    reopened = None
    try:
        snapshot = await projector.read_turn()
        q = HistoryQueries(snapshot)
        message_id = q.native_message(chat_id=phone, native_id='M')['message_id']
        sources = HistoryKnowledgeSources(q, {}, fixture_sources.ledger, fixture_sources.legacy_authority)
        first = sources.issue(message_id)
        original_bytes = json.dumps(asdict(first), sort_keys=True)
        fingerprint = q.content_fingerprint(message_id)
        snapshot.close()
        assert not projector._reader._snapshots
        await projector.rebuild(reason='synthetic unchanged rebuild')
        snapshot = await projector.read_turn()
        q = HistoryQueries(snapshot)
        sources = HistoryKnowledgeSources(q, {}, fixture_sources.ledger, fixture_sources.legacy_authority)
        assert sources.issue(message_id) == first
        snapshot.close()
        for kind, text, at, eid in (('edit', 'Synthetic B', ms+1000, 'E1'), ('edit', 'Synthetic A', ms+2000, 'E2')):
            append_line(path, dumps(raw(kind, text, at, eid)))
        snapshot = await projector.read_turn()
        q = HistoryQueries(snapshot)
        sources = HistoryKnowledgeSources(q, {}, fixture_sources.ledger, fixture_sources.legacy_authority)
        assert q.message(message_id)['current_text'] == 'Synthetic A'
        assert q.content_fingerprint(message_id) != fingerprint
        second = sources.issue(message_id)
        assert second.revision > first.revision
        assert sources.verify_source_ref(*first.key) is None
        assert sources.ledger.lookup(*first.key).source == first
        assert sources.ledger.lookup(*first.key).revoked
        assert json.dumps(asdict(first), sort_keys=True) == original_bytes
        snapshot.close()
        knowledge_path = service.db_path
        service.close()
        reopened = open_service(knowledge_path, history_mode=True)
        await projector.rebuild(reason='synthetic restart rebuild')
        snapshot = await projector.read_turn()
        q = HistoryQueries(snapshot)
        sources = HistoryKnowledgeSources(q, {}, HistorySourceLedger(reopened._store), RuntimeKnowledgeSources())
        assert sources.issue(message_id) == second
        snapshot.close()
        offset = 0 if coalesced else 120_000
        for kind, text, at, eid in (('edit', 'Synthetic B', ms+3000+offset, 'E3'), ('edit', 'Synthetic A', ms+4000+offset, 'E4')):
            append_line(path, dumps(raw(kind, text, at, eid)))
        snapshot = await projector.read_turn()
        q = HistoryQueries(snapshot)
        sources = HistoryKnowledgeSources(q, {}, sources.ledger, sources.legacy_authority)
        assert q.message(message_id)['current_text'] == 'Synthetic A'
        third = sources.issue(message_id)
        assert third.revision == (2 if coalesced else 3)
        assert sources.ledger.lookup(*second.key).revoked is (not coalesced)
        assert sources.verify_source_ref(*second.key) == (second if coalesced else None)
        assert sources.verify_source_ref(*first.key) is None
    finally:
        if snapshot is not None:
            snapshot.close()
        if reopened is not None:
            reopened.close()
        await projector.stop()


def test_redirect_equivalence_does_not_change_content_revision(case):
    _, q, sources, db = case
    first = sources.issue('m')
    fingerprint = q.content_fingerprint('m')
    contact(db, 'curated')
    db.execute("UPDATE contacts SET merged_into='curated' WHERE contact_id='a'")
    assert q.terminal('a') == 'curated'
    assert q.content_fingerprint('m') == fingerprint
    assert sources.issue('m') == first
    assert sources.verify_source_ref(*first.key) == first
    assert first.author_principal == AUTHOR


def test_history_source_ledger_is_shared_and_never_writes_processing(case):
    from yeoman_gateway.knowledge._history_sources import HistoryKnowledgeSources
    service, q, sources, _ = case
    def forbidden(*args, **kwargs):
        pytest.fail('legacy authority write')
    sources.legacy_authority.register_source = forbidden
    sources.legacy_authority.mark_source_revoked = forbidden
    ref = sources.issue('m')
    for _ in ('reply', 'tool', 'cli'):
        adapter = HistoryKnowledgeSources(q, {}, sources.ledger, sources.legacy_authority)
        with service.history_scope(q, adapter):
            assert service.knowledge_sources.verify_source_ref(*ref.key) == ref
    sources.mark_source_revoked(ref)
    assert sources.verify_source_ref(*ref.key) is None
    with pytest.raises(KnowledgeError):
        sources.issue('m')
    with pytest.raises(RuntimeError):
        with service._store.transaction():
            sources.ledger.issue(message_id='rollback', content_fingerprint='new', channel='whatsapp',
                                 chat_id='g@g.us', author_principal=AUTHOR, occurred_at_ms=100,
                                 author_contact_id='a', audience=q.audience('m'))
            raise RuntimeError('rollback')
    assert sources.ledger.current('rollback') is None
    from yeoman_gateway.knowledge._history_sources import HistorySourceLedger
    path = service.db_path
    service.close()
    reopened = open_service(path, history_mode=True)
    try:
        adapter = HistoryKnowledgeSources(q, {}, HistorySourceLedger(reopened._store), sources.legacy_authority)
        assert adapter.verify_source_ref(*ref.key) is None
        assert adapter.ledger.lookup(*ref.key).source == ref
        assert adapter.ledger.lookup(*ref.key).revoked
    finally:
        reopened.close()


def test_aliases_require_manifest_fingerprint_and_persist_revocation_before_mapping(case):
    from yeoman_gateway.knowledge._history_sources import build_history_source_aliases
    _, q, sources, _ = case
    issued, row = old_alias(case)
    invalid = {**row, 'content_fingerprint': 'unproven'}
    assert not build_history_source_aliases(queries=q, legacy_rows=[invalid], locators={issued.key: ('m',)})[0]
    issued = replace(issued, event_id='previously-unmapped')
    row = {**row, 'event_id': issued.event_id}
    sources.compatibility = {}
    sources.mark_source_revoked(issued)
    sources.compatibility = build_history_source_aliases(queries=q, legacy_rows=[row], locators={issued.key: ('m',)})[0]
    sources.ledger.persist_aliases(sources.compatibility)
    assert sources.verify_source_ref(*issued.key) is None


@pytest.mark.parametrize('legacy', [False, True])
@pytest.mark.parametrize('removed', [AUTHOR, MEMBER])
def test_later_approx_removal_narrows_but_keeps_statement(case, legacy, removed):
    from yeoman_gateway.knowledge.models import RecallQuery

    from tests.gateway.test_history_knowledge_identity import capture, read
    service, q, sources, db = case
    remaining = MEMBER if removed == AUTHOR else AUTHOR
    ref = old_alias(case)[0] if legacy else sources.issue('m')
    with service.history_scope(q, sources):
        sid, _ = capture(service, ref)
    event(db, 'removed', 'member_remove', 200, {'participants': [[removed.split(':')[1] + '@s.whatsapp.net']]}, certainty='capture_time_approx')
    assert sources.verify_source_ref(*ref.key) == ref
    assert sources.evidence_audience(ref, basis='').members == frozenset({remaining})
    assert sources.permits_principal(ref, remaining, now_ms=250)
    assert not sources.permits_principal(ref, removed, now_ms=250)
    with service.history_scope(q, sources):
        assert sid in service.recall(RecallQuery('Synthetic'), context=read(remaining, now=250)).statement_ids
        assert sid not in service.recall(RecallQuery('Synthetic'), context=read(removed, now=250)).statement_ids
    contact(db, 'c')
    identifier(db, 'c', '10003@s.whatsapp.net', start=1)
    event(db, 'widening', 'member_add', 50, {'participants': [['10003@s.whatsapp.net']]})
    assert sources.verify_source_ref(*ref.key) == ref
    assert sources.evidence_audience(ref, basis='').members == frozenset({remaining})
    assert not sources.permits_principal(ref, 'whatsapp:10003', now_ms=250)


def test_source_verification_and_audience_are_memoized_until_revocation(case, monkeypatch):
    import yeoman_gateway.knowledge._history_sources as module
    _, q, original, _ = case
    ref = original.issue('m')
    q = HistoryQueries(q.snapshot)
    sources = module.HistoryKnowledgeSources(q, {}, original.ledger, original.legacy_authority)
    calls = {'proof': 0, 'members': 0}
    proof, members = module._proof, q.members
    def counted_proof(*args, **kwargs):
        calls['proof'] += 1
        return proof(*args, **kwargs)
    def counted_members(*args, **kwargs):
        calls['members'] += 1
        return members(*args, **kwargs)
    monkeypatch.setattr(module, '_proof', counted_proof)
    monkeypatch.setattr(q, 'members', counted_members)
    assert sources.permits_principal(ref, AUTHOR, now_ms=150)
    assert sources.evidence_audience(ref, basis='').members == frozenset({AUTHOR, MEMBER})
    assert sources.author_contact(ref) == 'a'
    assert q.audience('m').members == frozenset({AUTHOR, MEMBER})
    assert calls == {'proof': 1, 'members': 1}
    sources.ledger.revoke(*ref.key, reason='synthetic direct revoke')
    assert not sources.permits_principal(ref, AUTHOR, now_ms=150)
    assert sources.evidence_audience(ref, basis='') is None
    assert sources.author_contact(ref) is None


def test_recomputed_audience_superset_cannot_widen_source(case):
    _, q, sources, db = case
    ref = sources.issue('m')
    contact(db, 'c')
    identifier(db, 'c', '10003@s.whatsapp.net', start=1)
    event(db, 'extra', 'member_add', 50, {'participants': [['10003@s.whatsapp.net']]})
    assert q.audience('m').members == frozenset({AUTHOR, MEMBER, 'whatsapp:10003'})
    assert sources.verify_source_ref(*ref.key) == ref
    assert sources.evidence_audience(ref, basis='').members == frozenset({AUTHOR, MEMBER})
    assert not sources.permits_principal(ref, 'whatsapp:10003', now_ms=150)
    assert sources.permits_principal(ref, MEMBER, now_ms=150)


def test_non_whatsapp_verification_does_not_cache_legacy_authority(case):
    from yeoman_gateway.knowledge.authority import EvidenceAudience
    _, _, sources, _ = case
    ref = SourceRef('telegram-source', 1, 'telegram', '123', 'telegram:123', 100)
    assert sources.verify_source_ref(*ref.key) is None
    sources.legacy_authority.register_source(ref, EvidenceAudience.author_only())
    assert sources.verify_source_ref(*ref.key) == ref
    sources.legacy_authority.mark_source_revoked(ref)
    assert sources.verify_source_ref(*ref.key) == sources.legacy_authority.verify_source_ref(*ref.key)
    assert sources.source_revoked(ref)
    assert not sources.permits_principal(ref, 'telegram:123', now_ms=150)
