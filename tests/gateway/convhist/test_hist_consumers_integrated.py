# ruff: noqa: F811
"""Integrated acceptance gates, using synthetic history and real Knowledge writes."""
import json
import sqlite3

import pytest
from yeoman_gateway.knowledge._capture_worker import StatementDraft

from tests.gateway.convhist.consumer_fixtures import (  # noqa: F401
    FAMILIES,
    capture_case,
    consumer_case,
    continuity_case,
    parity_case,
    refusal_case,
    reply_case,
    statement_case,
)


async def test_final_reply_snapshot_includes_enrichment_and_is_shared_by_tools(reply_case):
    await reply_case.run_voice_reply()
    assert reply_case.prompt_transcript == 'synthetic transcript'
    assert len(set(reply_case.reply_snapshot_ids)) == 1
    assert reply_case.responder_acquisitions == 0
    assert reply_case.legacy_history_reads == 0
    assert reply_case.live_snapshot_count == 0


@pytest.mark.parametrize('old', [False, True], ids=['new', 'legacy-ref'])
@pytest.mark.parametrize('author_only', [False, True], ids=['group', 'author-only'])
@pytest.mark.parametrize('disposition', ['delete', 'purge'])
async def test_live_statement_redirect_preserves_curation_and_revocation(
    statement_case, old, author_only, disposition,
):
    c = statement_case
    await c.publish(old=old, author_only=author_only)
    source = c.source
    assert source.event_id == ('legacy-source' if old else c.message_id)
    assert source.author_principal == c.author
    assert c.speaker() == c.original_contact
    assert c.audience() == (frozenset() if author_only else frozenset({c.author, c.member}))
    await c.curate()
    curated = c.curated_rows()
    refs = c.source_rows()
    await c.redirect()
    assert c.curated_rows() == curated
    assert c.source_rows() == refs
    assert await c.recall(c.author) == (c.statement_id,)
    assert bool(await c.recall(c.member)) is (not author_only)
    for denied in (c.later, 'whatsapp:19999', c.terminal_contact):
        assert await c.recall(denied) == ()
    assert await c.recall(c.author, person=c.terminal_contact) == (c.statement_id,)
    await c.remove_member()
    assert await c.recall(c.member) == ()
    await c.reuse_identifier()
    assert await c.recall(c.author) == ()
    await c.dispose(disposition)
    assert await c.recall(c.author) == ()
    assert not await c.revalidate()
    assert c.curated_rows() == curated
    assert c.source_rows() == refs
    c.assert_no_leases()


@pytest.mark.parametrize('family', FAMILIES)
async def test_consumer_family_reopens_after_atomic_replace(consumer_case, family):
    c = consumer_case
    first = await c.run(family)
    c.assert_no_leases()
    inode = c.path.stat().st_ino
    await c.replace()
    assert c.path.stat().st_ino != inode
    second = await c.run(family)
    assert second.generation == first.generation + 1
    assert second.connection is not first.connection
    for connection in (first.connection, second.connection):
        with pytest.raises(sqlite3.ProgrammingError, match='closed|thread'):
            connection.execute('SELECT 1')
    assert first.text == 'Synthetic original'
    assert second.text == 'Synthetic edited'
    assert first.result != second.result
    assert 'Synthetic original' not in str(second.result)
    if family in ('participation', 'reply_tools', 'knowledge_worker', 'secondary_consciousness_persona', 'cli_overseer'):
        assert 'Synthetic edited' in str(second.result)
    if family == 'a2a_recipient':
        assert first.result['status'] == 'completed' and second.result['status'] == 'rejected'
        assert len(c.effects) == 1
    if family == 'knowledge_identity_cache':
        assert first.result[2] and not set(first.result[2]) & set(second.result[2])
    assert second.name == 'Synthetic new'
    assert second.members == frozenset({c.author, c.later})
    assert first.source_valid and not second.source_valid
    c.assert_no_leases()


async def test_reader_parity_same_synthetic_conversation(parity_case):
    await parity_case.compare()
    assert set(parity_case.checked) == {
        'recent', 'ambient', 'reply_before_after', 'dm', 'thread', 'literal_search',
        'speaker', 'media',
    }
    assert set(parity_case.differences) == {
        'latest_edit', 'delete_purge', 'typed_terminal_identity', 'evidence_audience',
        'derived_label', 'authorized_cross_chat', 'passive', 'new_boundary',
        'failed_assistant_and_tools',
    }
    parity_case.assert_no_leases()


async def test_integrated_capture_continuity_has_exact_promotable_inputs_and_statements(continuity_case):
    c = continuity_case
    expected = await c.continuity()
    assert set(c.seen) == expected
    assert len(c.seen) == 5
    assert c.completed_sources() == expected
    assert c.outcomes() == {**dict.fromkeys(expected, 'published'), c.historical: 'processed'}
    assert c.statements() == {(mid, f'Expected statement {mid}') for mid in expected}
    assert len(c.knowledge._store.query('SELECT * FROM knowledge_statements')) == 5
    await c.starvation()
    c.assert_no_leases()


async def test_integrated_publication_crash_then_changed_retry_is_atomic(continuity_case, monkeypatch):
    c = continuity_case
    await c.prepare_empty()
    mid = await c.add('crash-source')
    tables = ('knowledge_statements', 'knowledge_statement_sources',
              'knowledge_statement_people', 'knowledge_statement_audit', 'memory2_nodes')
    baseline = {t: c.knowledge._store.scalar(f'SELECT count(*) FROM {t}') for t in tables}
    original = c.knowledge.capture
    calls = []
    def crash(*args, **kwargs):
        result = original(*args, **kwargs)
        calls.append(result.statement_ids)
        raise RuntimeError('synthetic crash after first capture')
    monkeypatch.setattr(c.knowledge, 'capture', crash)
    c.extractor = lambda items: [StatementDraft('Discarded first one'), StatementDraft('Discarded first two')]
    with pytest.raises(RuntimeError, match='after first capture'):
        await c.work()
    assert len(calls) == 1 and calls[0]
    c.reopen()
    assert {t: c.knowledge._store.scalar(f'SELECT count(*) FROM {t}') for t in tables} == baseline
    assert c.completed_sources() == set()
    assert c.knowledge._store.scalar("SELECT count(*) FROM knowledge_jobs WHERE state='done'") == 0
    assert c.outcomes()[mid] != 'published'
    c.extractor = lambda items: [StatementDraft('Complete retry one'), StatementDraft('Complete retry two')]
    await c.work(offset=1000000)
    assert c.statements() == {(mid, 'Complete retry one'), (mid, 'Complete retry two')}
    assert c.knowledge._store.scalar("SELECT count(*) FROM knowledge_jobs WHERE state='done'") == 1
    assert c.completed_sources() == {mid}
    assert c.outcomes()[mid] == 'published'
    c.assert_no_leases()


@pytest.mark.parametrize('status', ['failed', 'backlog', 'rebuilding'])
async def test_all_selected_paths_pause_without_legacy_fallback(consumer_case, status):
    c = consumer_case
    await c.queue_pending()
    before = [dict(row) for row in c.knowledge._store.query('SELECT * FROM knowledge_jobs')]
    c.pause(status)
    for family in (*FAMILIES, 'judge', 'reply'):
        result = await c.run_paused(family)
        assert result in ('history_paused', 'rejected')
        c.assert_no_leases()
    assert [dict(row) for row in c.knowledge._store.query('SELECT * FROM knowledge_jobs')] == before
    assert c.effects == []
    await c.accept_observation()
    assert c.observations == 1
    assert c.archive.status().spooled == (1 if status == 'backlog' else 0)
    assert 'Synthetic' not in json.dumps(c.effects)


async def test_integrated_refused_sources_are_separate_from_promotable_success(refusal_case):
    c = refusal_case
    await c.prepare_empty()
    unknown = await c.add('unknown-refusal', known=False)
    c.append('message', 'empty-refusal', text='')
    await c.settle()
    with c.snapshot() as snapshot:
        from yeoman_gateway.history.queries import HistoryQueries
        empty = HistoryQueries(snapshot).native_message(chat_id='synthetic@g.us', native_id='empty-refusal')['message_id']
    await c.work()
    assert c.seen == [] and c.statements() == set() and c.completed_sources() == set()
    assert c.outcomes() == {unknown: 'pending', empty: 'empty_text'}
    c.assert_no_leases()
