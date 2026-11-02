"""Group reply recall: the flag off/on gate and the history membership proof.

Synthetic projected history and temporary Knowledge stores only.  No provider call,
no live runtime path and no message text is read from anywhere but the fixture.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from yeoman_gateway.adapters.responder_llm import LLMResponder
from yeoman_gateway.history.queries import HistoryQueries
from yeoman_gateway.knowledge.models import RecallQuery

from tests.gateway.history_test_helpers import ProjectedHistory

CHAT = "120363000000000099@g.us"
OTHER_CHAT = "120363000000000098@g.us"
T0 = 1_790_000_000_000
MINUTE = 60_000
PRINCIPAL = "whatsapp:491700000001"
OTHER_PRINCIPAL = "whatsapp:491700000002"


class FakeKnowledge:
    """A read-gate-shaped stand-in: no membership means no content."""

    policy_revision = 1

    def __init__(self, text: str = "RECALL BLOCK") -> None:
        self.text = text
        self.calls: list[tuple[RecallQuery, Any, dict[str, Any]]] = []

    def recall(self, query: RecallQuery, *, context: Any, **kwargs: Any) -> Any:
        self.calls.append((query, context, dict(kwargs)))
        if context.recipient_principals is None:
            return SimpleNamespace(text="", statement_ids=())
        return SimpleNamespace(
            text=self.text,
            statement_ids=("s1",) if self.text else (),
        )


def _history(tmp_path: Path, *, known_membership: bool = True) -> ProjectedHistory:
    history = ProjectedHistory(tmp_path / "history")
    if known_membership:
        history.add_group(
            chat_id=CHAT,
            participants=["491700000001@s.whatsapp.net", "491700000002@s.whatsapp.net"],
            timestamp=(T0 - MINUTE) / 1000,
        )
    history.add_message(
        chat_id=CHAT,
        message_id="m1",
        text="synthetic line about a shared plan",
        timestamp=(T0 - 1000) / 1000,
        sender_id="491700000001@s.whatsapp.net",
        participant="491700000001@s.whatsapp.net",
        sender_name="anna",
    )
    history.snapshot().close()
    return history


def _responder(knowledge: FakeKnowledge, *, enabled: bool) -> LLMResponder:
    responder = object.__new__(LLMResponder)
    responder.knowledge = knowledge
    responder.people = None
    responder._group_reply_recall = enabled
    return responder


@pytest.mark.asyncio
async def test_group_recall_off_unchanged(tmp_path: Path) -> None:
    history = _history(tmp_path)
    knowledge = FakeKnowledge()
    responder = _responder(knowledge, enabled=False)
    try:
        async with history.turn() as snapshot:
            queries = HistoryQueries(snapshot)
            context = responder._knowledge_read_context(
                channel="whatsapp",
                chat_id=CHAT,
                principal=PRINCIPAL,
                is_owner=False,
            )
            recalled = responder._knowledge_statement_context(
                query="shared plan",
                channel="whatsapp",
                chat_id=CHAT,
                principal=PRINCIPAL,
                is_owner=False,
            )
    finally:
        history.close()

    # The flag-off prompt shape is exactly today's: no members, no recall content.
    assert context is not None and context.recipient_principals is None
    assert recalled == ""
    assert all(item[1].recipient_principals is None for item in knowledge.calls)
    assert queries is not None


@pytest.mark.asyncio
async def test_group_recall_on_uses_history_members(tmp_path: Path) -> None:
    history = _history(tmp_path)
    knowledge = FakeKnowledge()
    responder = _responder(knowledge, enabled=True)
    try:
        async with history.turn():
            recalled = responder._knowledge_statement_context(
                query="shared plan",
                channel="whatsapp",
                chat_id=CHAT,
                principal=PRINCIPAL,
                is_owner=False,
            )
    finally:
        history.close()

    assert recalled == "RECALL BLOCK"
    assert len(knowledge.calls) == 1
    query, context, kwargs = knowledge.calls[0]
    assert query.text == "shared plan" and query.limit == 5
    assert kwargs["group_wide"] is True
    assert context.recipient_principals == frozenset({PRINCIPAL, OTHER_PRINCIPAL})
    assert context.principal_id == PRINCIPAL
    assert context.is_direct is False


@pytest.mark.asyncio
async def test_group_recall_unknown_membership_stays_refused(tmp_path: Path) -> None:
    history = _history(tmp_path, known_membership=False)
    knowledge = FakeKnowledge()
    responder = _responder(knowledge, enabled=True)
    try:
        async with history.turn():
            recalled = responder._knowledge_statement_context(
                query="shared plan",
                channel="whatsapp",
                chat_id=CHAT,
                principal=PRINCIPAL,
                is_owner=False,
            )
    finally:
        history.close()

    assert recalled == ""
    assert all(item[1].recipient_principals is None for item in knowledge.calls)


def test_group_recall_never_admits_author_only_or_other_chat(tmp_path: Path) -> None:
    from tests.gateway.knowledge.conftest import ProjectedKnowledgeHarness

    harness = ProjectedKnowledgeHarness(tmp_path / "knowledge")
    try:
        tom, alex = harness.two_people()
        members = {harness.principal_for(tom), harness.principal_for(alex)}
        shared = harness.source(tom, audience=members)
        harness.capture_text("shared fact for this group", shared)
        private_source = harness.source(tom, audience=members)
        private = harness.capture_text("private fact that is author only", private_source)
        private_id = private.statement_ids[0]
        # The projected fixture has no author-only audience; make the stored statement
        # author-only directly so the group-wide read gate is what is under test.
        harness._raw_service._store.execute(  # noqa: SLF001 - fixture-only scope change
            "UPDATE knowledge_statements SET visibility_scope = 'author_only',"
            " group_rule = 'author_only' WHERE statement_id = ?",
            (private_id,),
        )
        harness._raw_service._store.commit_if_idle()  # noqa: SLF001
        other = harness.source(tom, chat="group-b", audience=members)
        harness.capture_text("fact from another chat", other)

        context = harness.read_context(tom, chat="group-a", recipients=members)
        result = harness.service.recall(
            RecallQuery(text="fact", limit=10),
            context=context,
            group_wide=True,
            require_match=True,
        )
    finally:
        harness.close()

    assert "shared fact for this group" in result.text
    assert "private fact that is author only" not in result.text
    assert "fact from another chat" not in result.text
