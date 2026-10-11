"""Owner questions widen recall only through an explicit, resolved target."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from yeoman_gateway.adapters.responder_llm import LLMResponder


@pytest.fixture
def responder(monkeypatch):
    responder = object.__new__(LLMResponder)
    responder.knowledge = SimpleNamespace(policy_revision=1, recall=Mock(return_value=SimpleNamespace(text="Alex holds shares")))
    responder.chat_registry = None
    responder._history_selected = True
    queries = SimpleNamespace(
        owner_member_chat_ids=Mock(return_value=("stocks@g.us",)),
        resolve_people_in_text=Mock(side_effect=lambda text, **kwargs: (("Alex", "alex-person"),) if "Alex" in text else ()),
        chats=lambda: [{"chat_id": "stocks@g.us", "subject": "Aktienrunde"}],
    )
    monkeypatch.setattr("yeoman_gateway.history.context.current_history_snapshot", lambda: object())
    monkeypatch.setattr("yeoman_gateway.history.queries.HistoryQueries", lambda snapshot: queries)
    return responder


def recall(responder, text, *, owner=True, chat="10001@s.whatsapp.net", purpose="reply"):
    return responder._knowledge_statement_context(query=text, channel="whatsapp", chat_id=chat,
        principal="whatsapp:10001", is_owner=owner, purpose=purpose)


def test_selected_direct_context_has_only_canonical_recipient(responder):
    context = responder._knowledge_read_context(channel="whatsapp", chat_id="10001@s.whatsapp.net",
        principal="whatsapp:10001", is_owner=True)
    assert context.recipient_principals == frozenset({"whatsapp:10001"})
    nonowner = responder._knowledge_read_context(channel="whatsapp", chat_id="10001@s.whatsapp.net",
        principal="whatsapp:10001", is_owner=False)
    assert nonowner.recipient_principals == frozenset({"whatsapp:10001", "10001@s.whatsapp.net"})


@pytest.mark.parametrize("question", ["Was weißt du über Alex' Aktien?", "Was hält Alex", "Erzähl mir von Alex"])
def test_explicit_german_person_question_recalls_by_identity(responder, question):
    assert recall(responder, question) == "Alex holds shares"
    call = responder.knowledge.recall.call_args
    assert call.args[0].person_ids == ("alex-person",)
    assert call.kwargs["context"].owner_lookup_person_ids == ("alex-person",)
    assert call.kwargs["require_match"] is False


def test_explicit_chat_question_targets_known_chat(responder):
    recall(responder, "Was wurde in Aktienrunde besprochen?")
    assert responder.knowledge.recall.call_args.kwargs["context"].owner_lookup_chat_ids == ("stocks@g.us",)


@pytest.mark.parametrize("text,owner,chat,purpose", [
    ("Alex hält Aktien", True, "10001@s.whatsapp.net", "reply"),
    ("Was weißt du über Alex?", False, "10001@s.whatsapp.net", "reply"),
    ("Was weißt du über Alex?", True, "stocks@g.us", "reply"),
    ("Was weißt du über Alex?", True, "10002@s.whatsapp.net", "reply"),
    ("Was weißt du über Alex?", True, "10001@s.whatsapp.net", "proactive"),
    ("Wie wird das Wetter?", True, "10001@s.whatsapp.net", "reply"),
])
def test_other_contexts_do_not_enable_owner_lookup(responder, text, owner, chat, purpose):
    recall(responder, text, owner=owner, chat=chat, purpose=purpose)
    call = responder.knowledge.recall.call_args
    context = call.kwargs["context"]
    assert not context.owner_lookup_person_ids
    assert not context.owner_lookup_chat_ids
    assert call.kwargs["require_match"] is True
