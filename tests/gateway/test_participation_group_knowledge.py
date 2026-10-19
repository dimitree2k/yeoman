"""Group Knowledge for Participation: readers proven from history, statements only."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from yeoman_gateway.history.context import history_turn
from yeoman_gateway.history.live import HistoryPaused
from yeoman_gateway.knowledge.models import KnowledgeContext, SourceRef
from yeoman_gateway.processing.participation_knowledge import (
    ParticipationKnowledgeReaders,
    ParticipationKnowledgeSelector,
    history_participation_readers,
)

from tests.gateway.history_test_helpers import ProjectedHistory

GROUP = "120363000000000001@g.us"
A_LID = "8420000000001@lid"
A_PN = "4915550000001@s.whatsapp.net"
B_PN = "4915550000002@s.whatsapp.net"
OUTSIDER = "4915550000009@s.whatsapp.net"
KNOWLEDGE = SimpleNamespace(policy_revision=3)


@pytest.fixture
def history(tmp_path):
    h = ProjectedHistory(
        tmp_path,
        groups=[dict(chat_id=GROUP, timestamp=1_700_000_000_000,
                     participants=[{"phoneJid": A_PN, "lid": A_LID}, {"phoneJid": B_PN}])],
        contacts=[dict(identifiers=[A_PN, A_LID], name="Synthetic A", timestamp=1_699_000_000_000)],
    )
    yield h
    h.close()


def _opportunity(*sources, chat=GROUP):
    return SimpleNamespace(channel="whatsapp", chat_id=chat, source_event_ids=tuple(sources))


def _context(*rows):
    return {"messages": [{"message_id": mid, "sender_id": sender} for mid, sender in rows]}


async def _readers(history, opportunity, context):
    async with history_turn(history):
        return history_participation_readers(opportunity, context, knowledge=KNOWLEDGE)


@pytest.mark.asyncio
async def test_author_on_the_roster_reads_for_the_whole_current_roster(history) -> None:
    readers = await _readers(history, _opportunity("m1"), _context(("m1", B_PN)))
    assert isinstance(readers, ParticipationKnowledgeReaders) and readers.single is not None
    context = readers.single.read_context
    assert context.principal_id in context.recipient_principals
    assert len(context.recipient_principals) == 2
    assert (context.purpose, context.is_direct, context.owner) == ("proactive", False, False)
    assert context.policy_revision == 3
    assert readers.single.fact_context.current_members == context.recipient_principals


@pytest.mark.asyncio
async def test_lid_sender_maps_to_the_roster_principal(history) -> None:
    by_lid = await _readers(history, _opportunity("m1"), _context(("m1", A_LID)))
    by_phone = await _readers(history, _opportunity("m1"), _context(("m1", A_PN)))
    assert isinstance(by_lid, ParticipationKnowledgeReaders)
    assert by_lid.principals == by_phone.principals


@pytest.mark.asyncio
async def test_two_authors_get_one_reader_each(history) -> None:
    readers = await _readers(
        history, _opportunity("m1", "m2"), _context(("m1", A_PN), ("m2", B_PN))
    )
    assert isinstance(readers, ParticipationKnowledgeReaders)
    assert len(readers.readers) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("opportunity", "context", "reason"),
    [
        (_opportunity("m1"), _context(("m1", OUTSIDER)), "denied"),
        (_opportunity("m1", "m2"), _context(("m1", B_PN), ("m2", OUTSIDER)), "denied"),
        (_opportunity("m1"), _context(("other", B_PN)), "source_unavailable"),
        (_opportunity(), _context(("m1", B_PN)), "source_unavailable"),
        (_opportunity("m1", chat="491555@s.whatsapp.net"), _context(("m1", B_PN)),
         "unsupported_chat_type"),
        (_opportunity("m1", chat="120363000000000099@g.us"), _context(("m1", B_PN)),
         "unknown_membership"),
    ],
)
async def test_refusals_render_no_knowledge(history, opportunity, context, reason) -> None:
    assert await _readers(history, opportunity, context) == reason


def test_readers_need_the_history_turn() -> None:
    with pytest.raises(HistoryPaused):
        history_participation_readers(
            _opportunity("m1"), _context(("m1", B_PN)), knowledge=KNOWLEDGE
        )


class _Statements:
    def __init__(self, text: str) -> None:
        ref = SourceRef("ev-1", 1, "whatsapp", GROUP, "alice", 1_700_000_000_000)
        self.result = KnowledgeContext(
            text=text, statement_ids=("stmt-1",), source_refs=(ref,),
            context_revision="r1", source_refs_by_statement=(("stmt-1", (ref,)),),
        )
        self.calls: list[dict] = []

    def recall(self, query, **kwargs):
        self.calls.append(kwargs)
        return self.result

    def revalidate(self, result, **kwargs):
        return self.result


@pytest.mark.asyncio
async def test_selector_without_legacy_memory_renders_statements_and_revalidates(history) -> None:
    knowledge = _Statements("Synthetic group meets on Thursdays.")
    selector = ParticipationKnowledgeSelector(knowledge=knowledge)
    readers = await _readers(history, _opportunity("m1"), _context(("m1", B_PN)))
    selection = selector.select_for_readers(query="when do we meet", readers=readers)
    assert selection.reason == "selected"
    assert selection.text == "Synthetic group meets on Thursdays."
    assert knowledge.calls[0]["group_wide"] is True
    assert knowledge.calls[0]["context"].recipient_principals == (
        readers.single.read_context.recipient_principals
    )
    assert selector.revalidate_for_readers(selection, readers=readers).text == selection.text


def test_wiring_is_off_unless_knowledge_is_enabled() -> None:
    from yeoman_gateway.app.bootstrap import build_participation_knowledge

    store = _Statements("x")
    assert build_participation_knowledge(
        SimpleNamespace(knowledge_enabled=False), knowledge=store
    ) == (None, None, None)
    assert build_participation_knowledge(
        SimpleNamespace(knowledge_enabled=True), knowledge=None
    ) == (None, None, None)
    selector, supplier, revalidate = build_participation_knowledge(
        SimpleNamespace(knowledge_enabled=True), knowledge=store
    )
    assert isinstance(selector, ParticipationKnowledgeSelector)
    assert callable(supplier) and callable(revalidate)


@pytest.mark.asyncio
@pytest.mark.parametrize("supplied", ["readers", "denied", None])
async def test_builder_renders_selected_knowledge_only_for_verified_readers(
    tmp_path, supplied
) -> None:
    from yeoman_gateway.processing.participation import (
        ParticipationOpportunity,
        _JudgeContext,
    )
    from yeoman_gateway.processing.participation_context import ParticipationContextBuilder
    from yeoman_gateway.processing.participation_knowledge import readers_from_single

    from tests.gateway.test_participation_context import CHANNEL, CHAT, NOW_MS, _inputs, _record
    from tests.gateway.test_participation_knowledge import _contexts

    history = ProjectedHistory(tmp_path / "history")
    _record(history, message_id="m-now", text="when do we meet again", minutes_ago=1, chat_id=CHAT)
    knowledge = _Statements("Synthetic group meets on Thursdays.")
    seen = []

    def supplier(opportunity, context):
        seen.append(opportunity.opportunity_id)
        return readers_from_single(*_contexts()) if supplied == "readers" else supplied

    builder = history.wire(ParticipationContextBuilder(
        policy=None, source_authorizer=lambda row: True,
        knowledge_selector=ParticipationKnowledgeSelector(knowledge=knowledge) if supplied else None,
        knowledge_context_supplier=supplier if supplied else None,
    ))
    opportunity = ParticipationOpportunity(
        opportunity_id="opp-1", channel=CHANNEL, chat_id=CHAT, trigger="inbound",
        source_event_ids=("m-now",), observed_revision=1, activation_epoch=1,
        created_at_ms=NOW_MS,
    )
    try:
        async with history.turn():
            context = await builder.build(
                opportunity, inputs=_inputs(current_source_ids=("m-now",)), now_ms=NOW_MS
            )
    finally:
        history.close()
    rendered = _JudgeContext.from_mapping(context).render()
    if supplied == "readers":
        assert context["knowledge_selection_status"] == "selected"
        assert context["selected_knowledge_text"] == "Synthetic group meets on Thursdays."
        assert "Synthetic group meets on Thursdays." in rendered
    else:
        assert context["knowledge_selection_status"] == (supplied or "disabled")
        assert "selected_knowledge_text" not in context
        assert "Synthetic group meets" not in rendered
    assert bool(seen) == bool(supplied)
