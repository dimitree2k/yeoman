from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from history_turn_fixtures import Projector
from test_participation_integration import COMMENT, _opportunity, _runtime
from yeoman_gateway.processing.continuation import ContinuationResolver
from yeoman_gateway.processing.models import CanonicalEvent, TextPayload


async def test_participation_pause_prevents_judge_and_effect(tmp_path):
    runtime, judge, builder, log = _runtime(tmp_path, decision=COMMENT)
    p = Projector()
    runtime._history_projector = p
    runtime._history_knowledge = None
    p.status = 'rebuilding'
    result = await runtime.evaluate_participation(_opportunity())
    assert result == {'status': 'skipped', 'reason': 'history_paused'}
    assert judge.calls == 0 and builder.calls == 0
    assert runtime._submission.calls == 0 and runtime._reactor.calls == []
    assert p.live_snapshot_count == 0
    p.close()
    log.close()


async def test_continuation_uses_current_history_not_journal_payload():
    from yeoman_gateway.history.context import history_turn
    p = Projector()
    p.add('human', text='edited human')
    p.add('bot', text='edited bot', direction='out')
    source = CanonicalEvent(event_id='ev-human', event_key='key', trace_id='trace',
                            channel='whatsapp', chat_id='a@g.us', principal='sender',
                            source_message_id='human', payload={'text': 'old journal'})
    effect = SimpleNamespace(effect_id='effect', payload=TextPayload(text='old draft', reply_to='human'), origin='participation')
    receipt = SimpleNamespace(provider_message_id='bot', confirmed_ms=1700000000001)
    store = SimpleNamespace(recent_reply_effects=lambda **kw: [effect], effect_transport_receipt=lambda _: receipt,
        events_by_source_message=lambda _: [source], get_event_source_authority=lambda *a: None,
        resolve_reference=lambda _: None, event_assignment=lambda _: None)
    resolver = ContinuationResolver(store=store, threads=SimpleNamespace(policy=SimpleNamespace(followup_window_ms=30000)), judge=SimpleNamespace(choose=AsyncMock(return_value='bot')))
    event = CanonicalEvent(event_id='new', event_key='new', trace_id='new', channel='whatsapp', chat_id='a@g.us', principal='sender', payload={'text': 'followup'})
    async with history_turn(p):
        anchors = resolver.candidates(event, aliases=(), now_ms=1700000000002)
        assert len(anchors) == 1
        assert anchors[0].effect_id == 'effect' and anchors[0].thread_id is None
        assert anchors[0].text == 'edited bot'
        assert anchors[0].source.payload['text'] == 'edited human'
    p.db.execute("INSERT INTO message_events(event_id,kind,channel,chat_id,target_message_id,actor_basis,time_certainty,payload_json,provenance,source_refs) VALUES ('del','delete','whatsapp','a@g.us','bot','unknown','unknown','{}','native','[]')")
    p.db.commit()
    async with history_turn(p):
        assert resolver.candidates(event, aliases=(), now_ms=1700000000002) == ()
    assert receipt.provider_message_id == 'bot' and effect.payload.text == 'old draft'
    from yeoman_gateway.core.models import InboundEvent
    from yeoman_gateway.history.live import HistoryPaused
    from yeoman_gateway.processing.responder import ThreadActorResponder
    responder = object.__new__(ThreadActorResponder)
    responder._store = SimpleNamespace(get_event=lambda _: source)
    request = InboundEvent(channel='whatsapp', chat_id='a@g.us', sender_id='sender', content='old journal', message_id='human')
    actor_snapshot = SimpleNamespace(source_refs=(SimpleNamespace(event_id='ev-human'),))
    async with history_turn(p):
        assert responder._request_event(request, actor_snapshot).content == 'edited human'
    p.db.execute("INSERT INTO message_events(event_id,kind,channel,chat_id,target_message_id,actor_basis,time_certainty,payload_json,provenance,source_refs) VALUES ('del-human','delete','whatsapp','a@g.us','human','unknown','unknown','{}','native','[]')")
    p.db.commit()
    async with history_turn(p):
        with pytest.raises(HistoryPaused):
            responder._request_event(request, actor_snapshot)
    p.close()


async def test_delayed_approval_reopens_scope_and_revalidates_source_revisions():
    from yeoman_gateway.history.context import (
        current_history_snapshot,
        history_turn,
        validate_history_evidence,
    )
    from yeoman_gateway.processing.effects import EffectGateway
    from yeoman_gateway.processing.participation_runtime import _knowledge_evidence_for_admission
    p = Projector()
    p.add('source', text='synthetic source')
    admission = SimpleNamespace(channel='whatsapp', chat_id='a@g.us', source_event_ids=('source',), knowledge_evidence=None)
    async with history_turn(p):
        evidence = _knowledge_evidence_for_admission(admission, {})
        assert validate_history_evidence(evidence)
    stored = SimpleNamespace(target=SimpleNamespace(channel='whatsapp'))
    gateway = EffectGateway(SimpleNamespace(get_effect=lambda _: stored))
    gateway.set_history_scope(p, None)
    scopes = []
    async def execute(_):
        scopes.append(id(current_history_snapshot()))
        return validate_history_evidence(evidence)
    gateway._execute_ready_scoped = execute
    assert await gateway.execute_ready('synthetic-effect')
    p.db.execute("UPDATE messages SET text='edited source' WHERE message_id='source'")
    p.db.commit()
    assert not await gateway.execute_ready('synthetic-effect')
    p.generation += 1
    assert not await gateway.execute_ready('synthetic-effect')
    assert p.live_snapshot_count == 0 and len(scopes) == 3
    p.close()


@pytest.mark.parametrize('with_knowledge', [False, True])
async def test_native_approval_proof_survives_real_staging(tmp_path, with_knowledge):
    from test_participation_knowledge_approval import (
        CHAT,
        _build_tools,
        _stage,
        _statement_selection,
        _stored_snapshot,
    )
    from yeoman_gateway.history.context import history_turn
    from yeoman_gateway.processing.participation_knowledge import selection_to_mapping
    from yeoman_gateway.processing.participation_runtime import _knowledge_evidence_for_admission
    p = Projector()
    p.add('m1', chat=CHAT)
    admission = SimpleNamespace(channel='whatsapp', chat_id=CHAT, source_event_ids=('m1',), knowledge_evidence=None)
    context = {'_knowledge_evidence': selection_to_mapping(_statement_selection())} if with_knowledge else {}
    tools, log, archive, effects = _build_tools(tmp_path)
    try:
        async with history_turn(p):
            evidence = _knowledge_evidence_for_admission(admission, context)
            result = await _stage(tools, knowledge_evidence=evidence)
            assert result['status'] == 'awaiting_approval', result
            stored = await _stored_snapshot(log)
            assert stored['participation_admission']['knowledge_evidence'] == evidence
            assert 'history' not in str(effects.calls)
    finally:
        archive.close()
        log.close()
        p.close()


async def test_non_dict_history_evidence_with_stale_proof_is_refused():
    from collections import UserDict

    from yeoman_gateway.app.bootstrap import _KnowledgeDecisionValidator
    from yeoman_gateway.history.context import history_turn, validate_history_evidence
    p = Projector()
    evidence = UserDict({'history': {'generation': 0, 'revisions': {'source': 'stale'}}})
    validator = _KnowledgeDecisionValidator(selector=None, chat_registry=None, knowledge=None)
    try:
        async with history_turn(p):
            assert not validate_history_evidence(evidence)
            assert validator(SimpleNamespace(knowledge_evidence=evidence)) == (False, 'history_sources_changed')
    finally:
        p.close()


def test_pre_dispatch_unreadable_history_proof_refuses_without_escaping():
    from test_participation_knowledge_approval import _UnreadableEvidence
    from yeoman_gateway.app.bootstrap import build_effect_router
    from yeoman_gateway.bus.queue import MessageBus
    from yeoman_shared.config.schema import Config
    config = Config()
    config.processing.enabled = True
    admission = SimpleNamespace(knowledge_evidence=_UnreadableEvidence())
    store = SimpleNamespace(get_participation_admission=lambda _: admission)
    adapter = SimpleNamespace(policy_engine=lambda: None, known_tools=set())
    router = build_effect_router(config, adapter, store, MessageBus())
    check = router._gateway._executor._participation_pre_dispatch
    assert check(SimpleNamespace(admission_id='synthetic-admission')) == (False, 'knowledge_approval_revalidation_unavailable')
