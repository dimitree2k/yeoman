"""Owner questions widen recall only through an explicit, resolved target."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from yeoman_gateway.adapters.responder_llm import LLMResponder

OWNER_ENDPOINTS = (
    ("whatsapp:4915000000099", "4915000000099@s.whatsapp.net"),
    ("whatsapp:900000000099@lid", "4915000000099@s.whatsapp.net"),
    ("whatsapp:4915000000099", "900000000099@lid"),
    ("whatsapp:900000000099@lid", "900000000099@lid"),
)


@pytest.fixture
def responder(monkeypatch,tmp_path):
    from tests.gateway.people.resolver_fixtures import DirectoryCase
    case = DirectoryCase(tmp_path)
    case.add_id(case.owner,'wa_lid','900000000099')
    person = case.add_person('Alex',ids=(('phone','4915000000011'),))
    case.hdb.execute("UPDATE contacts SET contact_id='alex-person' WHERE contact_id=?",(str(person),))
    case.hdb.execute("UPDATE identifier_history SET contact_id='alex-person' WHERE contact_id=?",(str(person),))
    case.add_id(person,'knowledge_person','alex-person')
    case.set_roster('stocks@g.us',[case.owner,case.assistant,person],label='Aktienrunde')
    case.set_roster('private@g.us',[case.counterpart,case.assistant],label='Private Group')
    snapshot = case.snapshot
    responder = object.__new__(LLMResponder)
    responder.people = case.directory
    responder._test_case, responder._test_person, responder._test_snapshot = case,person,snapshot
    responder.knowledge = SimpleNamespace(policy_revision=1, recall=Mock(return_value=SimpleNamespace(text="Alex holds shares")))
    monkeypatch.setattr("yeoman_gateway.history.context.current_history_snapshot", lambda: snapshot)
    try:
        yield responder
    finally:
        case.close()


def recall(responder, text, *, owner=True, principal="whatsapp:4915000000099",
           chat="4915000000099@s.whatsapp.net", purpose="reply"):
    return responder._knowledge_statement_context(query=text, channel="whatsapp", chat_id=chat,
        principal=principal, is_owner=owner, purpose=purpose)


def test_selected_direct_context_has_only_canonical_recipient(responder):
    context = responder._knowledge_read_context(channel="whatsapp", chat_id="4915000000099@s.whatsapp.net",
        principal="whatsapp:4915000000099", is_owner=True)
    assert context.recipient_principals == frozenset({"whatsapp:4915000000099"})
    nonowner = responder._knowledge_read_context(channel="whatsapp", chat_id="4915000000099@s.whatsapp.net",
        principal="whatsapp:4915000000099", is_owner=False)
    assert nonowner.recipient_principals == frozenset({"whatsapp:4915000000099", "4915000000099@s.whatsapp.net"})


@pytest.mark.parametrize("question", ["Was weißt du über Alex' Aktien?", "Was hält Alex", "Erzähl mir von Alex"])
@pytest.mark.parametrize(("principal", "chat"), OWNER_ENDPOINTS)
def test_explicit_german_person_question_recalls_by_identity(responder, question, principal, chat):
    assert recall(responder, question, principal=principal, chat=chat) == "Alex holds shares"
    call = responder.knowledge.recall.call_args
    assert call.args[0].person_ids == ("alex-person",)
    assert call.kwargs["context"].owner_lookup_person_ids == ("alex-person",)
    assert call.kwargs["require_match"] is False


@pytest.mark.parametrize(("principal", "chat"), OWNER_ENDPOINTS)
def test_explicit_chat_question_targets_known_chat(responder, principal, chat):
    recall(responder, "Was wurde in Aktienrunde besprochen?", principal=principal, chat=chat)
    assert responder.knowledge.recall.call_args.kwargs["context"].owner_lookup_chat_ids == ("stocks@g.us",)


@pytest.mark.parametrize("text,owner,chat,purpose", [
    ("Alex hält Aktien", True, "4915000000099@s.whatsapp.net", "reply"),
    ("Was weißt du über Alex?", False, "4915000000099@s.whatsapp.net", "reply"),
    ("Was weißt du über Alex?", True, "stocks@g.us", "reply"),
    ("Was weißt du über Alex?", True, "4915000000098@s.whatsapp.net", "reply"),
    ("Was weißt du über Alex?", True, "4915000000099@s.whatsapp.net", "proactive"),
    ("Wie wird das Wetter?", True, "4915000000099@s.whatsapp.net", "reply"),
])
def test_other_contexts_do_not_enable_owner_lookup(responder, text, owner, chat, purpose):
    recall(responder, text, owner=owner, chat=chat, purpose=purpose)
    call = responder.knowledge.recall.call_args
    context = call.kwargs["context"]
    assert not context.owner_lookup_person_ids
    assert not context.owner_lookup_chat_ids
    assert call.kwargs["require_match"] is True


def test_d2_maps_directory_id_to_legacy_subject(responder,monkeypatch):
    from yeoman_gateway.history.queries import HistoryQueries
    c = responder._test_case
    c.add_name(responder._test_person,'Owner Nickname',source='owner')
    def poisoned(*args,**kwargs):
        raise AssertionError('retired name resolver')
    monkeypatch.setattr(HistoryQueries,'resolve_people_in_text',poisoned)
    recall(responder,'Was weißt du über Owner Nickname?')
    call = responder.knowledge.recall.call_args
    assert call.args[0].person_ids == ('alex-person',)
    assert call.kwargs['context'].owner_lookup_person_ids == ('alex-person',)
    assert call.kwargs['require_match'] is False


def test_d2_ambiguous_span_never_widens(responder):
    responder._test_case.add_person('Alex')
    recall(responder,'Was weißt du über Alex?')
    call = responder.knowledge.recall.call_args
    assert call.args[0].person_ids == ()
    assert call.kwargs['context'].owner_lookup_person_ids == ()
    assert call.kwargs['require_match'] is True


def test_d2_all_legacy_subjects_for_one_directory_identity(responder):
    responder._test_case.add_id(responder._test_person,'knowledge_person','legacy-second')
    recall(responder,'Was weißt du über Alex?')
    assert responder.knowledge.recall.call_args.args[0].person_ids == ('alex-person','legacy-second')


@pytest.mark.parametrize('kind',['unmapped','hidden','missing_directory'])
def test_d2_unavailable_identity_does_not_fall_back(responder,kind):
    from yeoman_gateway.people.resolver import PeopleResolver
    c = responder._test_case
    if kind == 'unmapped':
        c.add_person('No Mapping')
        name = 'No Mapping'
    elif kind == 'hidden':
        c.add_name(responder._test_person,'Retired Nickname',source='owner',hidden=True)
        name = 'Retired Nickname'
    else:
        responder.people = PeopleResolver(c.path.parent/'absent.db')
        name = 'Alex'
    recall(responder,'Was weißt du über '+name+'?')
    call = responder.knowledge.recall.call_args
    assert call.args[0].person_ids == ()
    assert call.kwargs['require_match'] is True


def test_d2_retired_snapshot_propagates_history_fence(responder):
    from yeoman_gateway.history.live import HistoryPaused
    responder._test_snapshot.close()
    with pytest.raises(HistoryPaused):
        recall(responder,'Was weißt du über Alex?')
    responder.knowledge.recall.assert_not_called()


def test_nonmember_named_chat_is_still_refused(responder):
    assert recall(responder,'Was wurde in Private Group besprochen?') == ''
    responder.knowledge.recall.assert_not_called()


def test_no_question_never_scans_directory(responder,monkeypatch):
    def poisoned(*args,**kwargs):
        raise AssertionError('not a question')
    monkeypatch.setattr('yeoman_gateway.people.resolver.resolve_people_in_text',poisoned)
    recall(responder,'Alex hält Aktien')
    assert responder.knowledge.recall.call_args.kwargs['require_match'] is True
