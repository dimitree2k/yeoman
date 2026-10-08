import asyncio

import pytest
from yeoman_gateway.history.live import HistoryPaused
from yeoman_gateway.knowledge._memory.read_gate import FactReadGate
from yeoman_gateway.knowledge._memory.shared_facts import FactReadContext
from yeoman_gateway.knowledge.models import (
    AttributeCandidate,
    AttributeValue,
    Identifier,
    KnowledgeError,
    PersonLinkCandidate,
    RecallQuery,
    StatementCandidate,
    TrustedAdminContext,
    TrustedCaptureContext,
    TrustedReadContext,
)

from tests.gateway.convhist.test_hist_queries import contact, event, identifier
from tests.gateway.test_history_knowledge_upgrade import digest
from tests.gateway.test_history_source_compatibility import AUTHOR, MEMBER, old_alias
from tests.gateway.test_history_source_compatibility import (
    case as source_case,  # noqa: F401 - pytest fixture
)


@pytest.fixture
def case(source_case):  # noqa: F811 - pytest fixture dependency
    return source_case


def capture(service, source, *, attributes=False):
    context = TrustedCaptureContext('synthetic', 1, 'native', (source,))
    candidate = StatementCandidate('Synthetic statement', (source,), people=(
        PersonLinkCandidate('a', 'subject', source, 'explicit'),), attributes=(
            AttributeCandidate('a', 'description', AttributeValue('engineer')),
        ) if attributes else ())
    result = service.capture(candidate, context=context)
    assert result.statement_ids
    return result.statement_ids[0], context


def read(principal=AUTHOR, *, now=150, chat_id="g@g.us", is_direct=False):
    return TrustedReadContext(principal, 'whatsapp', chat_id, frozenset({principal}), None, 1,
                              'reply', now, is_direct=is_direct)


def frozen(service):
    return {name: digest(service._store.connection, name) for name in (
        'contacts', 'contact_identifiers', 'contact_aliases', 'contact_fields',
        'knowledge_identifier_bindings', 'knowledge_identity_redirects', 'knowledge_identity_ops')}


def test_statement_capture_new_history_contact_needs_no_legacy_identity_row(case):
    service, q, sources, _ = case
    before = frozen(service)
    source = sources.issue('m')
    with service.history_scope(q, sources):
        sid, _ = capture(service, source, attributes=True)
        assert service.person_for_principal(AUTHOR) == 'a'
        assert service.display_name('a') == 'a'
        assert service.resolve_identifier(Identifier('whatsapp', 'phone_jid', '10001@s.whatsapp.net', namespace='whatsapp'), at_ms=100).person_id == 'a'
        assert service.recall(RecallQuery('Synthetic'), context=read()).statement_ids == (sid,)
    assert frozen(service) == before
    assert service._store.query_one("SELECT 1 FROM contacts WHERE id='a'") is None
    assert service._store.query("PRAGMA foreign_key_check") == []
    assert service._store.scalar("SELECT count(*) FROM knowledge_person_attributes WHERE person_id='a'") == 1
    with pytest.raises(HistoryPaused):
        service.person_for_principal(AUTHOR)


@pytest.mark.parametrize('old', [False, True])
@pytest.mark.parametrize('author_only', [False, True])
@pytest.mark.parametrize('redirect', [False, True])
def test_channel_principals_authorize_old_and_new_sources_without_redirect_expansion(case, old, author_only, redirect):
    service, q, sources, db = case
    if author_only:
        db.execute("UPDATE messages SET chat_id='10001@s.whatsapp.net' WHERE message_id='m'")
    source = old_alias(case)[0] if old else sources.issue('m')
    def reader(principal=AUTHOR, *, now=150):
        return read(principal, now=now, chat_id=source.chat_id, is_direct=author_only)
    before = frozen(service)
    with service.history_scope(q, sources):
        sid, ctx = capture(service, source)
        if redirect:
            for i in range(12, -1, -1):
                contact(db, f'chain{i}', redirect=f'chain{i+1}' if i < 12 else None)
            db.execute("UPDATE contacts SET merged_into='chain0' WHERE contact_id='a'")
        assert service.recall(RecallQuery('Synthetic'), context=reader()).statement_ids == (sid,)
        assert bool(service.recall(RecallQuery('Synthetic'), context=reader(MEMBER)).statement_ids) is (not author_only)
        assert not service.recall(RecallQuery('Synthetic'), context=reader('whatsapp:10003')).statement_ids
        if redirect:
            assert service.recall(RecallQuery('', person_ids=('chain12',)), context=reader()).statement_ids == (sid,)
        # Reused principal string does not transfer source ownership.
        db.execute("UPDATE identifier_history SET valid_until_ms=200 WHERE contact_id='a'")
        contact(db, 'new-holder')
        identifier(db, 'new-holder', '10001@s.whatsapp.net', start=200)
        assert not service.recall(RecallQuery('Synthetic'), context=reader(now=250)).statement_ids
        memory = service.memory_store()
        gate = FactReadGate(memory)
        fact_context = FactReadContext(AUTHOR, f'channel:whatsapp:chat:{source.chat_id}', frozenset({AUTHOR, MEMBER}),
                                       None, service._store.acl_epoch, 250)
        assert gate.recheck((sid,), fact_context) == frozenset()
        event(db, 'removed', 'member_remove', 260, {'participants': [['10002@s.whatsapp.net']]})
        assert not service.recall(RecallQuery('Synthetic'), context=reader(MEMBER, now=300)).statement_ids
        assert frozen(service) == before
        assert service._store.query_one('SELECT event_id FROM knowledge_statement_sources WHERE statement_id=?', (sid,))[0] == source.event_id


def test_history_identity_mutations_refused_but_statement_curation_continues(case):
    service, q, sources, _ = case
    before = frozen(service)
    source = sources.issue('m')
    with service.history_scope(q, sources):
        sid, context = capture(service, source)
        admin = TrustedAdminContext(AUTHOR, 1, 'synthetic-owner', True)
        for call in (
            lambda: service.set_preferred_name('a', 'Changed', context=admin),
            lambda: service.merge_people('a', 'b', context=admin, expected_revision=1),
            lambda: service.bind_identifier('a', Identifier('whatsapp', 'phone_jid', '10003@s.whatsapp.net'), evidence_ref='proof', mapping_verified=True, context=admin),
        ):
            with pytest.raises(KnowledgeError) as error:
                call()
            assert error.value.code == 'history_identity_read_only'
        receipt = service.correct_statement(sid, StatementCandidate('Curated replacement', (source,)),
                                            expected_source=source, context=context)
        assert receipt.changed_ids
    assert frozen(service) == before
    assert service._store.scalar('SELECT count(*) FROM knowledge_statement_audit') > 1


@pytest.mark.asyncio
async def test_identity_scope_isolated_between_concurrent_turns(case):
    import sqlite3

    from yeoman_gateway.history.queries import HistoryQueries
    from yeoman_gateway.history.reader import HistorySnapshot
    from yeoman_gateway.history.schema import create
    from yeoman_gateway.knowledge._history_sources import HistoryKnowledgeSources
    service, q, sources, _ = case
    other_db = sqlite3.connect(':memory:')
    create(other_db)
    contact(other_db, 'other')
    identifier(other_db, 'other', '10001@s.whatsapp.net', start=1)
    other_snapshot = HistorySnapshot(2, (), other_db)
    other_q = HistoryQueries(other_snapshot)
    other_sources = HistoryKnowledgeSources(other_q, {}, sources.ledger, sources.legacy_authority)
    async def turn(queries, adapter, expected):
        with service.history_scope(queries, adapter):
            await asyncio.sleep(0)
            assert service.person_for_principal(AUTHOR) == expected
            with service.history_scope(q, sources):
                assert service.person_for_principal(AUTHOR) == 'a'
            assert service.person_for_principal(AUTHOR) == expected
    try:
        await asyncio.gather(turn(q, sources, 'a'), turn(other_q, other_sources, 'other'))
        with pytest.raises(HistoryPaused):
            service.person_for_principal(AUTHOR)
    finally:
        other_snapshot.close()


def test_permission_cache_does_not_survive_history_generation(case):
    service, q, sources, _ = case
    source = sources.issue('m')
    with service.history_scope(q, sources):
        sid, _ = capture(service, source)
        result = service.recall(RecallQuery('Synthetic'), context=read())
        assert result.statement_ids == (sid,)
        q.snapshot.generation += 1
        assert not service.revalidate(result, context=read()).statement_ids
        service._policy.policy_revision = 2
        assert not service.recall(RecallQuery('Synthetic'), context=read()).statement_ids


def test_history_identity_read_surface_and_frozen_store_guard(case):
    from yeoman_gateway.knowledge._contacts.store import ContactsStore
    from yeoman_gateway.knowledge._history_identity import HistoryIdentityEngine
    service, q, sources, _ = case
    ident = Identifier('whatsapp', 'phone_jid', '10001@s.whatsapp.net', namespace='whatsapp')
    with service.history_scope(q, sources):
        engine = HistoryIdentityEngine(service._store, queries=q, authority=sources, policy=service._policy)
        assert engine.binding_at(ident, 100).person_id == 'a'
        assert engine.binding_for(ident).person_id == 'a'
        assert engine.get_person('a').person_id == 'a'
        assert engine.require_person('a').display_name == 'a'
        assert engine.resolve_endpoint('a', 'whatsapp', at_ms=100).identifier == ident
        assert engine.eligible_people_for_context(read()) == (('a', 'a'),)
        assert engine.search_mention_name_candidates('a', person_ids=('a',), context=read())[0].person_id == 'a'
        assert engine.search_mention_text_candidates('a', person_ids=('a',), context=read())[0].person_id == 'a'
        assert engine.aliases_of_many(('a',))['a'][0].name == 'a'
        # An observed name has no global address-release proof.
        assert engine.delivery_identifiers_for_alias('a', channel='whatsapp', scope_key='global') == ()
        with pytest.raises(KnowledgeError) as error:
            ContactsStore(owner=service._store)
        assert error.value.code == 'history_identity_read_only'
        with pytest.raises(KnowledgeError):
            with service._store.transaction():
                service._store.execute("UPDATE contacts SET display_name='changed'")


def test_history_alias_delivery_preserves_frozen_release_controls(case):
    from yeoman_gateway.knowledge._history_identity import HistoryIdentityEngine
    service, q, sources, db = case
    before = frozen(service)
    contact(db, 'curated')
    db.execute("UPDATE contacts SET display_name='Synthetic' WHERE contact_id='curated'")
    identifier(db, 'curated', '10009@s.whatsapp.net', start=1)
    with service.history_scope(q, sources):
        engine = HistoryIdentityEngine(service._store, queries=q, authority=sources, policy=service._policy)
        expected = Identifier('whatsapp', 'phone_jid', '10009@s.whatsapp.net', namespace='whatsapp')
        assert engine.delivery_identifiers_for_alias('Synthetic', channel='whatsapp', scope_key='global') == (expected,)
        assert service.identifier_for_name('Synthetic', channel='whatsapp') == expected
        assert not engine.delivery_identifiers_for_alias('a', channel='whatsapp', scope_key='global')
        # Current history, rather than a frozen cache match, determines ownership.
        db.execute("UPDATE identifier_history SET valid_until_ms=200 WHERE contact_id='curated'")
        contact(db, 'replacement')
        identifier(db, 'replacement', '10009@s.whatsapp.net', start=200)
        assert not engine.delivery_identifiers_for_alias('Synthetic', channel='whatsapp', scope_key='global')
    assert frozen(service) == before
