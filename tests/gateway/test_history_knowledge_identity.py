
import pytest
from yeoman_gateway.history.live import HistoryPaused
from yeoman_gateway.knowledge.models import (
    AttributeCandidate,
    AttributeValue,
    Identifier,
    PersonLinkCandidate,
    RecallQuery,
    StatementCandidate,
    TrustedCaptureContext,
    TrustedReadContext,
)

from tests.gateway.convhist.test_hist_queries import contact, event, identifier, message
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
        event(db, 'removed', 'member_remove', 260, {'participants': [['10002@s.whatsapp.net']]})
        assert not service.recall(RecallQuery('Synthetic'), context=reader(MEMBER, now=300)).statement_ids
        assert frozen(service) == before
        assert service._store.query_one('SELECT event_id FROM knowledge_statement_sources WHERE statement_id=?', (sid,))[0] == source.event_id


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


@pytest.mark.parametrize("principal", [MEMBER, "whatsapp:20002@lid"])
def test_owner_dm_lookup_reads_author_only_statement_only_with_history_audience_proof(case, principal):
    service, q, sources, db = case
    if principal.endswith("@lid"):
        identifier(db, 'b', '20002@lid', start=1)
    source = sources.issue('m')
    with service.history_scope(q, sources):
        sid, _ = capture(service, source)
        service._store.execute("UPDATE knowledge_statements SET visibility_scope='author_only' WHERE statement_id=?", (sid,))
        owner_dm = TrustedReadContext(
            principal, 'whatsapp', '10002@s.whatsapp.net', frozenset({principal}), None, 1,
            'reply', 150, is_direct=True, owner=True, owner_lookup_person_ids=('a',),
        )
        assert service.recall(RecallQuery('Synthetic'), context=owner_dm).statement_ids == (sid,)

        normal_dm = TrustedReadContext(
            principal, 'whatsapp', '10002@s.whatsapp.net', frozenset({principal}), None, 1,
            'reply', 150, is_direct=True,
        )
        assert not service.recall(RecallQuery('Synthetic'), context=normal_dm).statement_ids
        group = TrustedReadContext(
            MEMBER, 'whatsapp', source.chat_id, frozenset({MEMBER}), None, 1,
            'reply', 150, owner=True, owner_lookup_person_ids=('a',),
        )
        assert not service.recall(RecallQuery('Synthetic'), context=group).statement_ids

        event(db, 'owner-left', 'member_remove', 120, {'participants': [['10002@s.whatsapp.net']]})
        assert not service.recall(RecallQuery('Synthetic'), context=owner_dm).statement_ids

        proactive = TrustedReadContext(
            MEMBER, 'whatsapp', '10002@s.whatsapp.net', frozenset({MEMBER}), None, 1,
            'proactive', 150, is_direct=True, owner=True, owner_lookup_person_ids=('a',),
        )
        assert not service.recall(RecallQuery('Synthetic'), context=proactive).statement_ids


@pytest.mark.parametrize("principal", [MEMBER, "whatsapp:20002@lid"])
def test_owner_lookup_keeps_an_author_only_statement_from_the_owner_dm(case, principal):
    service, q, sources, db = case
    if principal.endswith("@lid"):
        identifier(db, 'b', '20002@lid', start=1)
    message(db, 'owner-dm', chat='10002@s.whatsapp.net', sender='b',
            identifier='10002@s.whatsapp.net', text='Synthetic local note')
    source = sources.issue('owner-dm')
    with service.history_scope(q, sources):
        sid, _ = capture(service, source)
        service._store.execute(
            "UPDATE knowledge_statements SET visibility_scope='author_only' WHERE statement_id=?",
            (sid,),
        )
        context = TrustedReadContext(
            principal, 'whatsapp', '10002@s.whatsapp.net', frozenset({principal}), None, 1,
            'reply', 150, is_direct=True, owner=True, owner_lookup_person_ids=('a',),
        )
        assert service.recall(RecallQuery('Synthetic', person_ids=('a',)), context=context).statement_ids == (sid,)


@pytest.mark.asyncio
@pytest.mark.parametrize('principal', [
    'whatsapp:10002', 'whatsapp:10002@s.whatsapp.net', 'whatsapp:20002@lid',
    '10002', '20002', '10002@s.whatsapp.net', '20002@lid',
])
async def test_responder_handoff_uses_frozen_lid_principal_when_sender_canonicalization_is_empty(case, monkeypatch, principal):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from yeoman_gateway.adapters import responder_llm
    from yeoman_gateway.adapters.responder_llm import LLMResponder
    from yeoman_gateway.policy.identity import canonical_user_id
    from yeoman_gateway.processing.dispatch import CURRENT_PRINCIPAL, CURRENT_TURN
    from yeoman_gateway.processing.models import StoredTurn, TurnBinding

    service, q, sources, db = case
    identifier(db, 'b', '20002@lid', start=1)
    assert canonical_user_id("whatsapp", "20002@lid", {}) == ""
    responder = object.__new__(LLMResponder)
    session = SimpleNamespace(operational_store=None, messages=[])
    session.add_message = Mock()
    responder.sessions = SimpleNamespace(
        get_or_create=Mock(return_value=session), save=Mock(),
    )
    responder._model_for_profile = lambda profile: "test-model"
    responder._session_user_metadata = lambda *args, **kwargs: {}
    handed_off = {}

    class HandoffReachedError(Exception):
        pass

    def set_context(**kwargs):
        handed_off.update(kwargs)
        raise HandoffReachedError

    responder._set_tool_context = set_context
    monkeypatch.setattr(responder_llm.lf, "start_trace", lambda **kwargs: object())
    binding = TurnBinding(StoredTurn("turn-1", "thread-1", principal))
    turn_token = CURRENT_TURN.set(binding)
    principal_token = CURRENT_PRINCIPAL.set("")
    try:
        with service.history_scope(q, sources):
            with pytest.raises(HandoffReachedError):
                await responder._generate_locked(
                    session_key="whatsapp:20002@lid", channel="whatsapp", chat_id="20002@lid",
                    content="Was weißt du?", sender_id="20002@lid", media=(), metadata={},
                    allowed_tools=set(), persona_text=None, is_owner=True, history_snapshot=q.snapshot,
                )
    finally:
        CURRENT_PRINCIPAL.reset(principal_token)
        CURRENT_TURN.reset(turn_token)
    assert handed_off["canonical_user_id"] == principal

    from yeoman_gateway.agent.tools.recall_conversation import RecallConversationTool
    from yeoman_gateway.processing.tool_context import (
        ToolInvocationContext,
        reset_tool_context,
        set_tool_context,
    )

    db.execute("UPDATE contacts SET display_name='Alex' WHERE contact_id='a'")
    responder.knowledge = service
    responder.chat_registry = None
    monkeypatch.setattr("yeoman_gateway.history.context.current_history_snapshot", lambda: q.snapshot)
    with service.history_scope(q, sources):
        sid, _ = capture(service, sources.issue('m'))
        service._store.execute("UPDATE knowledge_statements SET visibility_scope='author_only' WHERE statement_id=?", (sid,))
        token = set_tool_context(ToolInvocationContext(
            channel=handed_off['channel'], chat_id=handed_off['chat_id'],
            canonical_user_id=handed_off['canonical_user_id'], is_owner=handed_off['is_owner'],
            history_snapshot=q.snapshot,
        ))
        try:
            text = await RecallConversationTool(None).execute(person='Alex', query='Synthetic')
            assert 'Synthetic A' in text
        finally:
            reset_tool_context(token)
        recall = Mock(wraps=service.recall)
        monkeypatch.setattr(service, 'recall', recall)
        assert responder._knowledge_statement_context(
            query="Was weißt du über Alex' Aktien?", channel=handed_off['channel'],
            chat_id=handed_off['chat_id'], principal=handed_off['canonical_user_id'],
            is_owner=handed_off['is_owner'],
        ) == 'Synthetic statement (Alex)'
        assert recall.call_args.args[0].person_ids == ('a',)


def test_owner_local_dm_does_not_read_author_only_statement_by_another_contact(case):
    service, q, sources, db = case
    identifier(db, 'b', '20002@lid', start=1)
    # This phone DM belonged to the other person at source time, then changed hands.
    db.execute("UPDATE identifier_history SET valid_until_ms=120 WHERE value='10001@s.whatsapp.net'")
    identifier(db, 'b', '10001@s.whatsapp.net', start=120)
    message(db, 'other-author-dm', chat='10001@s.whatsapp.net', sender='a',
            identifier='10001@s.whatsapp.net', text='Other person private note')
    source = sources.issue('other-author-dm')
    with service.history_scope(q, sources):
        sid, _ = capture(service, source)
        service._store.execute(
            "UPDATE knowledge_statements SET visibility_scope='author_only' WHERE statement_id=?",
            (sid,),
        )
        owner_dm = TrustedReadContext(
            'whatsapp:20002@lid', 'whatsapp', '10001@s.whatsapp.net',
            frozenset({'whatsapp:20002@lid'}), None, 1, 'reply', 150,
            is_direct=True, owner=True, owner_lookup_person_ids=('a',),
        )
        assert not service.recall(RecallQuery('Other person private note'), context=owner_dm).statement_ids


@pytest.mark.parametrize(("principal", "chat"), [
    (MEMBER, "10002@s.whatsapp.net"), ("whatsapp:20002@lid", "10002@s.whatsapp.net"),
    (MEMBER, "20002@lid"), ("whatsapp:20002@lid", "20002@lid"),
])
def test_owner_german_person_question_retrieves_real_v3_statement(case, monkeypatch, principal, chat):
    from yeoman_gateway.adapters.responder_llm import LLMResponder

    service, q, sources, db = case
    if principal.endswith("@lid") or chat.endswith("@lid"):
        identifier(db, 'b', '20002@lid', start=1)
    source = sources.issue('m')
    db.execute("UPDATE contacts SET display_name='Alex' WHERE contact_id='a'")
    with service.history_scope(q, sources):
        sid, _ = capture(service, source)
        service._store.execute(
            "UPDATE knowledge_statements SET visibility_scope='author_only' WHERE statement_id=?",
            (sid,),
        )
        responder = object.__new__(LLMResponder)
        responder.knowledge = service
        responder.chat_registry = None
        monkeypatch.setattr(
            "yeoman_gateway.history.context.current_history_snapshot", lambda: q.snapshot
        )
        text = responder._knowledge_statement_context(
            query="Was weißt du über Alex' Aktien?", channel='whatsapp',
            chat_id=chat, principal=principal, is_owner=True,
        )
        assert text == 'Synthetic statement (Alex)'
        assert not responder._knowledge_statement_context(
            query="Wie wird das Wetter?", channel='whatsapp',
            chat_id='10002@s.whatsapp.net', principal=MEMBER, is_owner=True,
        )
        assert not responder._knowledge_statement_context(
            query="Was weißt du über Alex' Aktien?", channel='whatsapp',
            chat_id='10001@s.whatsapp.net', principal=AUTHOR, is_owner=False,
        )
        assert not responder._knowledge_statement_context(
            query="Was weißt du über Alex' Aktien?", channel='whatsapp',
            chat_id=source.chat_id, principal=MEMBER, is_owner=True,
        )


@pytest.mark.parametrize(("principal", "chat"), [
    (MEMBER, "10002@s.whatsapp.net"), ("whatsapp:20002@lid", "10002@s.whatsapp.net"),
    (MEMBER, "20002@lid"), ("whatsapp:20002@lid", "20002@lid"),
])
def test_owner_german_natural_prefix_preserves_display_name_in_real_v3_result(case, monkeypatch, principal, chat):
    from yeoman_gateway.adapters.responder_llm import LLMResponder

    service, q, sources, db = case
    if principal.endswith("@lid") or chat.endswith("@lid"):
        identifier(db, 'b', '20002@lid', start=1)
    source = sources.issue('m')
    db.execute("UPDATE contacts SET display_name='AlexGrrrrr😈' WHERE contact_id='a'")
    with service.history_scope(q, sources):
        capture(service, source)
        responder = object.__new__(LLMResponder)
        responder.knowledge = service
        responder.chat_registry = None
        monkeypatch.setattr(
            "yeoman_gateway.history.context.current_history_snapshot", lambda: q.snapshot
        )
        assert responder._knowledge_statement_context(
            query="Was weißt du über Alex' Aktien?", channel='whatsapp',
            chat_id=chat, principal=principal, is_owner=True,
        ) == 'Synthetic statement (AlexGrrrrr😈)'


@pytest.mark.parametrize(("principal", "chat"), [
    (MEMBER, "10002@s.whatsapp.net"), ("whatsapp:20002@lid", "10002@s.whatsapp.net"),
    (MEMBER, "20002@lid"), ("whatsapp:20002@lid", "20002@lid"),
])
def test_owner_ambiguous_natural_prefix_has_no_knowledge_person_targets_or_results(case, monkeypatch, principal, chat):
    from yeoman_gateway.adapters.responder_llm import LLMResponder

    service, q, sources, db = case
    if principal.endswith("@lid") or chat.endswith("@lid"):
        identifier(db, 'b', '20002@lid', start=1)
    source = sources.issue('m')
    db.execute("UPDATE contacts SET display_name='Alexander Müller' WHERE contact_id='a'")
    db.execute("UPDATE contacts SET display_name='Alexa Weber' WHERE contact_id='b'")
    with service.history_scope(q, sources):
        capture(service, source)
        capture_context = TrustedCaptureContext('synthetic', 1, 'native', (source,))
        candidate = StatementCandidate('Other synthetic statement', (source,), people=(
            PersonLinkCandidate('b', 'subject', source, 'explicit'),
        ))
        assert service.capture(candidate, context=capture_context).statement_ids

        recalled = []
        recall = service.recall

        def record_recall(query, **kwargs):
            recalled.append(query)
            return recall(query, **kwargs)

        monkeypatch.setattr(service, 'recall', record_recall)
        responder = object.__new__(LLMResponder)
        responder.knowledge = service
        responder.chat_registry = None
        monkeypatch.setattr(
            "yeoman_gateway.history.context.current_history_snapshot", lambda: q.snapshot
        )
        assert not responder._knowledge_statement_context(
            query='Was weißt du über Alex?', channel='whatsapp',
            chat_id=chat, principal=principal, is_owner=True,
        )
        assert len(recalled) == 1
        assert recalled[0].person_ids == ()


@pytest.mark.parametrize(("principal", "chat"), [
    (MEMBER, "10002@s.whatsapp.net"), ("whatsapp:20002@lid", "10002@s.whatsapp.net"),
    (MEMBER, "20002@lid"), ("whatsapp:20002@lid", "20002@lid"),
])
def test_owner_lookup_denies_late_joiner_even_when_currently_in_group(case, monkeypatch, principal, chat):
    from yeoman_gateway.adapters.responder_llm import LLMResponder

    service, q, sources, db = case
    if principal.endswith("@lid") or chat.endswith("@lid"):
        identifier(db, 'b', '20002@lid', start=1)
    db.execute("UPDATE message_events SET payload_json=? WHERE event_id='roster'",
               ('{"complete":true,"participants":[["10001@s.whatsapp.net"]]}',))
    event(db, 'owner-joined-later', 'member_add', 120,
          {'participants': [['10002@s.whatsapp.net']]})
    db.execute("UPDATE contacts SET display_name='Alex' WHERE contact_id='a'")
    source = sources.issue('m')
    with service.history_scope(q, sources):
        sid, _ = capture(service, source)
        service._store.execute(
            "UPDATE knowledge_statements SET visibility_scope='author_only' WHERE statement_id=?",
            (sid,),
        )
        responder = object.__new__(LLMResponder)
        responder.knowledge = service
        responder.chat_registry = None
        monkeypatch.setattr(
            "yeoman_gateway.history.context.current_history_snapshot", lambda: q.snapshot
        )
        assert not responder._knowledge_statement_context(
            query="Was weißt du über Alex' Aktien?", channel='whatsapp',
            chat_id=chat, principal=principal, is_owner=True,
        )


def test_owner_cross_chat_lookup_denies_reassigned_original_audience_identifier(case):
    service, q, sources, db = case
    identifier(db, 'b', '20002@lid', start=1)
    source = sources.issue('m')
    with service.history_scope(q, sources):
        sid, _ = capture(service, source)
        service._store.execute(
            "UPDATE knowledge_statements SET visibility_scope='author_only' WHERE statement_id=?",
            (sid,),
        )
        owner_dm = TrustedReadContext(
            'whatsapp:20002@lid', 'whatsapp', '20002@lid',
            frozenset({'whatsapp:20002@lid'}), None, 1, 'reply', 150,
            is_direct=True, owner=True, owner_lookup_person_ids=('a',),
        )
        assert service.recall(RecallQuery('Synthetic'), context=owner_dm).statement_ids == (sid,)
        db.execute("UPDATE identifier_history SET valid_until_ms=160 WHERE contact_id='b' AND value='10002@s.whatsapp.net'")
        contact(db, 'reassigned')
        identifier(db, 'reassigned', '10002@s.whatsapp.net', start=160)
        from dataclasses import replace
        assert not service.recall(
            RecallQuery('Synthetic'), context=replace(owner_dm, now_ms=170)
        ).statement_ids
