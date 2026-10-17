"""Owner scope is deterministic and identifier-complete; nothing else widens.

The live owner chat carries 204 ``author_only`` statements with empty audiences, split
across five spellings of the same person: phone form (158), ``@lid`` form (21), LID
number without suffix (16), bare LID number (6) and bare phone (3). These tests build
that shape synthetically on the real v3 service, so the gate - not a mock - decides.
"""

import pytest
from yeoman_gateway.adapters.responder_llm import LLMResponder
from yeoman_gateway.knowledge.models import (
    RecallQuery,
    StatementCandidate,
    TrustedCaptureContext,
    TrustedReadContext,
)

from tests.gateway.convhist.test_hist_queries import identifier, message
from tests.gateway.test_history_source_compatibility import (
    case as source_case,  # noqa: F401 - pytest fixture
)

DM_CHAT = "20002@lid"  # the owner's chat identifier space, LID form
OWNER_LID = "whatsapp:20002@lid"
OWNER_PHONE_FORM = "whatsapp:10002"
OTHER = "whatsapp:10001"

#: Every spelling the statement store may carry for the *same* owner identifier.
OWNER_FORMS = (
    ("whatsapp:10002", "Synthetic phone form"),  # phone form (158 live rows)
    ("whatsapp:10002@s.whatsapp.net", "Synthetic phone jid"),
    ("10002", "Synthetic bare phone"),
    ("+10002", "Synthetic plus phone"),
    ("whatsapp:20002@lid", "Synthetic lid form"),  # @lid form (21)
    ("20002@lid", "Synthetic bare lid jid"),
    ("whatsapp:20002", "Synthetic lid digits"),  # LID number without suffix (16)
    ("20002", "Synthetic bare lid"),  # bare LID number (6)
)


@pytest.fixture
def owner_case(source_case):  # noqa: F811 - pytest fixture dependency
    service, q, sources, db = source_case
    # The owner's phone and LID are the same person, proven by one stored binding.
    identifier(db, "b", "20002@lid", start=1)
    return service, q, sources, db


def capture(service, source, content):
    context = TrustedCaptureContext("synthetic", 1, "native", (source,))
    result = service.capture(StatementCandidate(content, (source,)), context=context)
    assert result.statement_ids
    return result.statement_ids[0]


def stored_source(db, sources, message_id, *, chat=DM_CHAT, sender="b",
                  sender_identifier="10002@s.whatsapp.net", text="Synthetic"):
    message(db, message_id, chat=chat, sender=sender, identifier=sender_identifier,
            text=text)
    return sources.issue(message_id)


def own_statement(service, sources, db, message_id, content, author_form):
    statement_id = capture(service, stored_source(db, sources, message_id, text=content), content)
    service._store.execute(
        "UPDATE knowledge_statements SET author_principal=?, visibility_scope='author_only'"
        " WHERE statement_id=?",
        (author_form, statement_id),
    )
    return statement_id


def owner_read(principal=OWNER_LID, chat=DM_CHAT, **overrides):
    fields = dict(
        principal_id=principal, channel="whatsapp", chat_id=chat,
        recipient_principals=frozenset({principal}), membership_revision=None,
        policy_revision=1, purpose="reply", now_ms=150, is_direct=True, owner=True,
    )
    fields.update(overrides)
    return TrustedReadContext(**fields)


@pytest.fixture
def owner_forms(owner_case):
    service, q, sources, db = owner_case
    with service.history_scope(q, sources):
        rows = {
            form: own_statement(service, sources, db, f"owner-form-{index}",
                                content, form)
            for index, (form, content) in enumerate(OWNER_FORMS)
        }
        yield rows


def test_owner_dm_reads_every_stored_author_form_of_its_own_statements(owner_case, owner_forms):
    service, q, sources, _ = owner_case
    with service.history_scope(q, sources):
        result = service.recall(RecallQuery("Synthetic", limit=50), context=owner_read())
    assert set(result.statement_ids) == set(owner_forms.values())


def test_owner_read_context_reaches_every_form_without_request_text(owner_case, owner_forms):
    """The owner-direct read the CLI and tools issue reaches the same rows.

    ``owner_read_context`` runs as ``purpose="admin"``, so owner scope must not be a
    property of the reply path either: the same proven owner direct chat decides it.
    """
    service, q, sources, _ = owner_case
    service._policy.admin_principals = frozenset({"whatsapp:10002@s.whatsapp.net"})
    with service.history_scope(q, sources):
        context = service.owner_read_context(channel="whatsapp", chat_id=DM_CHAT)
        result = service.recall(RecallQuery("Synthetic", limit=50), context=context)
    assert context.principal_id == OWNER_LID
    assert context.purpose == "admin"
    assert set(result.statement_ids) == set(owner_forms.values())


def test_owner_scope_does_not_open_another_authors_author_only_statement(owner_case):
    service, q, sources, db = owner_case
    with service.history_scope(q, sources):
        statement_id = capture(
            service,
            stored_source(db, sources, "other-author", sender="a",
                          sender_identifier="10001@s.whatsapp.net",
                          text="Synthetic other author"),
            "Synthetic other author",
        )
        service._store.execute(
            "UPDATE knowledge_statements SET visibility_scope='author_only'"
            " WHERE statement_id=?",
            (statement_id,),
        )
        result = service.recall(RecallQuery("Synthetic other author", limit=50),
                                context=owner_read())
        assert statement_id not in result.statement_ids
        # The author's own reach is unchanged, not revoked: see the non-owner read test.


def test_non_owner_read_is_unchanged_and_gains_nothing_from_owner_spellings(owner_case):
    service, q, sources, db = owner_case
    with service.history_scope(q, sources):
        own = capture(service, stored_source(db, sources, "member-own", sender="a",
                                            sender_identifier="10001@s.whatsapp.net",
                                            chat="10001@s.whatsapp.net",
                                            text="Synthetic member own"),
                      "Synthetic member own")
        service._store.execute("UPDATE knowledge_statements SET visibility_scope='author_only'"
                               " WHERE statement_id=?", (own,))
        # A spelling of the same identifier is *not* the same string for a non-owner.
        variant = capture(service, stored_source(db, sources, "member-variant", sender="a",
                                                sender_identifier="10001@s.whatsapp.net",
                                                chat="10001@s.whatsapp.net",
                                                text="Synthetic member variant"),
                          "Synthetic member variant")
        service._store.execute(
            "UPDATE knowledge_statements SET author_principal='10001',"
            " visibility_scope='author_only' WHERE statement_id=?", (variant,))
        # Naming every owner spelling in the request text changes nothing either.
        result = service.recall(
            RecallQuery(f"Synthetic {OWNER_LID} 20002@lid 10002 whatsapp:10002", limit=50),
            context=owner_read(principal=OTHER, chat="10001@s.whatsapp.net", owner=False,
                               is_direct=True),
        )
        assert result.statement_ids == (own,)


@pytest.fixture
def responder(owner_case, monkeypatch):
    service, q, sources, _ = owner_case
    responder = object.__new__(LLMResponder)
    responder.knowledge = service
    monkeypatch.setattr(
        "yeoman_gateway.history.context.current_history_snapshot", lambda: q.snapshot
    )
    return responder


@pytest.mark.parametrize("text", [
    "Shanghai ist echt teuer",                  # no question mark
    "Was weis ich über Shanghai",               # typo
    "Tell me about Shanghai",                   # not German
    "shanghai",                                 # one word, no phrasing at all
])
def test_owner_dm_keeps_owner_scope_without_any_question_form(owner_case, responder, text):
    service, q, sources, db = owner_case
    with service.history_scope(q, sources):
        own_statement(service, sources, db, "shanghai", "Shanghai ist eine Stadt",
                      "whatsapp:20002")
        assert responder._knowledge_statement_context(
            query=text, channel="whatsapp", chat_id=DM_CHAT, principal=OWNER_LID,
            is_owner=True,
        ) == "Shanghai ist eine Stadt (b)"


def test_owner_dm_matching_nothing_injects_nothing(owner_case, responder):
    service, q, sources, db = owner_case
    with service.history_scope(q, sources):
        own_statement(service, sources, db, "shanghai", "Shanghai ist eine Stadt",
                      "whatsapp:20002")
        assert responder._knowledge_statement_context(
            query="Wie wird das Wetter?", channel="whatsapp", chat_id=DM_CHAT,
            principal=OWNER_LID, is_owner=True,
        ) == ""


def test_non_owner_context_does_not_reach_owner_material_naming_its_forms(owner_case, responder):
    service, q, sources, db = owner_case
    with service.history_scope(q, sources):
        own_statement(service, sources, db, "shanghai", "Shanghai ist eine Stadt",
                      "whatsapp:20002")
        # A group read by another member: no owner scope, whatever the text names.
        assert responder._knowledge_statement_context(
            query=f"Shanghai {OWNER_LID} 20002@lid 10002 whatsapp:10002",
            channel="whatsapp", chat_id="g@g.us", principal=OTHER, is_owner=False,
        ) == ""
        # The owner's own DM read from a non-owner is not a mechanism either.
        assert responder._knowledge_statement_context(
            query="Shanghai", channel="whatsapp", chat_id=DM_CHAT, principal=OTHER,
            is_owner=False,
        ) == ""
