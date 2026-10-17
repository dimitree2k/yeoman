"""Owner scope is deterministic; request text only narrows the target set.

Change 2 turned owner scope into a property of the trusted context - a verified owner
reading a direct reply - instead of a property of the question. The two question-shaped
tests here asserted the old text gate: a German question form switched the owner lookup
on and, in the same step, dropped the relevance bar to "whatever is in scope". They are
rewritten rather than deleted, because the invariant they guard still holds in the new
shape: the same forms resolve the same identities, but no phrasing is needed to earn
owner scope and relevance still decides what reaches the prompt.
"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from yeoman_gateway.adapters.responder_llm import LLMResponder

OWNER_ENDPOINTS = (
    ("whatsapp:10001", "10001@s.whatsapp.net"),
    ("whatsapp:90001@lid", "10001@s.whatsapp.net"),
    ("whatsapp:10001", "90001@lid"),
    ("whatsapp:90001@lid", "90001@lid"),
)

#: Phrasings that used to decide authorization: a question mark or a German/English
#: interrogative at the start of the message. None of them may decide it any more.
MESSAGES = (
    "Was weißt du über Alex' Aktien?",
    "Alex hält Aktien",
    "Was weis ich über Alex' Aktien",   # typo
    "Tell me about Alex",               # not German
    "Alex",                             # no phrasing at all
)


@pytest.fixture
def responder(monkeypatch):
    responder = object.__new__(LLMResponder)
    responder.knowledge = SimpleNamespace(policy_revision=1, recall=Mock(return_value=SimpleNamespace(text="Alex holds shares")))
    queries = SimpleNamespace(
        owner_member_chat_ids=Mock(return_value=("stocks@g.us",)),
        is_owner_direct_chat=Mock(side_effect=lambda principal, chat, at_ms, **kwargs:
                                  kwargs.get("is_owner") and (principal, chat) in OWNER_ENDPOINTS),
        resolve_people_in_text=Mock(side_effect=lambda text, **kwargs: (("Alex", "alex-person"),) if "Alex" in text or "Alex" in text.lower() else ()),
        chats=lambda: [{"chat_id": "stocks@g.us", "subject": "Aktienrunde"}],
    )
    monkeypatch.setattr("yeoman_gateway.history.context.current_history_snapshot", lambda: object())
    monkeypatch.setattr("yeoman_gateway.history.queries.HistoryQueries", lambda snapshot: queries)
    return responder


def recall(responder, text, *, owner=True, principal="whatsapp:10001",
           chat="10001@s.whatsapp.net", purpose="reply"):
    return responder._knowledge_statement_context(query=text, channel="whatsapp", chat_id=chat,
        principal=principal, is_owner=owner, purpose=purpose)


def read_context(responder, *, owner=True, principal="whatsapp:10001",
                 chat="10001@s.whatsapp.net", purpose="reply"):
    return responder._knowledge_read_context(channel="whatsapp", chat_id=chat,
        principal=principal, is_owner=owner, purpose=purpose)


def test_selected_direct_context_has_only_canonical_recipient(responder):
    context = responder._knowledge_read_context(channel="whatsapp", chat_id="10001@s.whatsapp.net",
        principal="whatsapp:10001", is_owner=True)
    assert context.recipient_principals == frozenset({"whatsapp:10001"})
    nonowner = responder._knowledge_read_context(channel="whatsapp", chat_id="10001@s.whatsapp.net",
        principal="whatsapp:10001", is_owner=False)
    assert nonowner.recipient_principals == frozenset({"whatsapp:10001", "10001@s.whatsapp.net"})


@pytest.mark.parametrize("message", MESSAGES)
@pytest.mark.parametrize(("principal", "chat"), OWNER_ENDPOINTS)
def test_owner_scope_holds_for_every_message_form(responder, message, principal, chat):
    context = read_context(responder, principal=principal, chat=chat)
    assert responder._knowledge_owner_scope(context) is True


@pytest.mark.parametrize("question", MESSAGES[:1])
@pytest.mark.parametrize(("principal", "chat"), OWNER_ENDPOINTS)
def test_owner_question_still_recalls_by_identity_at_the_same_relevance_bar(responder, question, principal, chat):
    assert recall(responder, question, principal=principal, chat=chat) == "Alex holds shares"
    call = responder.knowledge.recall.call_args
    assert call.args[0].person_ids == ("alex-person",)
    assert call.kwargs["context"].owner_lookup_person_ids == ("alex-person",)
    # A named target widens the candidate set; it never lowers the relevance bar.
    assert call.kwargs["require_match"] is True


@pytest.mark.parametrize(("principal", "chat"), OWNER_ENDPOINTS)
def test_named_chat_is_a_target_not_an_authorization_switch(responder, principal, chat):
    recall(responder, "Was wurde in Aktienrunde besprochen?", principal=principal, chat=chat)
    call = responder.knowledge.recall.call_args
    assert call.kwargs["context"].owner_lookup_chat_ids == ("stocks@g.us",)
    assert call.kwargs["require_match"] is True


@pytest.mark.parametrize("text,owner,chat,purpose", [
    ("Alex hält Aktien", False, "10001@s.whatsapp.net", "reply"),
    ("Was weißt du über Alex?", False, "10001@s.whatsapp.net", "reply"),
    ("Was weißt du über Alex?", True, "stocks@g.us", "reply"),
    ("Was weißt du über Alex?", True, "10002@s.whatsapp.net", "reply"),
    ("Was weißt du über Alex?", True, "10001@s.whatsapp.net", "proactive"),
])
def test_other_contexts_never_get_owner_scope(responder, text, owner, chat, purpose):
    context = read_context(responder, owner=owner, chat=chat, purpose=purpose)
    assert responder._knowledge_owner_scope(context) is False
    recall(responder, text, owner=owner, chat=chat, purpose=purpose)
    call = responder.knowledge.recall.call_args
    assert not call.kwargs["context"].owner_lookup_person_ids
    assert not call.kwargs["context"].owner_lookup_chat_ids
    assert call.kwargs["require_match"] is True
