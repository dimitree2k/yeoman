from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from yeoman_gateway.knowledge._memory.shared_facts import FactReadContext, FactRetrievalResult
from yeoman_gateway.knowledge.authority import EvidenceAudience
from yeoman_gateway.knowledge.models import (
    KnowledgeContext,
    RecallQuery,
    SourceRef,
    StatementCandidate,
    TrustedCaptureContext,
    TrustedReadContext,
)
from yeoman_gateway.processing.participation_knowledge import ParticipationKnowledgeSelector

from tests.gateway.capture_harness import AUTHOR, GROUP, CaptureHarness

NOW = 1_800_000_000_000
CHAT = "group-1@g.us"
MEMBERS = frozenset({"alice", "bob"})


def _contexts(*, members: frozenset[str] | None = MEMBERS):
    return (
        TrustedReadContext(
            principal_id="alice",
            channel="whatsapp",
            chat_id=CHAT,
            recipient_principals=members,
            membership_revision="membership-v3" if members is not None else None,
            policy_revision=8,
            purpose="proactive",
            now_ms=NOW,
        ),
        FactReadContext(
            principal_id="alice",
            chat_scope_key=f"channel:whatsapp:chat:{CHAT}",
            current_members=members,
            epoch=4,
            now_ms=NOW,
        ),
    )


@dataclass
class _Knowledge:
    result: KnowledgeContext
    calls: list[dict[str, Any]]

    def recall(self, query, **kwargs):
        self.calls.append({"query": query, **kwargs})
        return self.result

    def revalidate(self, result, **kwargs):
        self.calls.append({"revalidate": result, **kwargs})
        return self.result


@dataclass
class _Memory:
    result: FactRetrievalResult
    calls: list[dict[str, Any]]

    def retrieve_for_context(self, **kwargs):
        self.calls.append(kwargs)
        return self.result

    def revalidate_for_context(self, result, **kwargs):
        self.calls.append({"revalidate": result, **kwargs})
        return self.result


def _selector(statement_text: str = "Stammtisch donnerstags.", fact_text: str = ""):
    ref = SourceRef("ev-1", 1, "whatsapp", CHAT, "alice", NOW - 7 * 86_400_000)
    knowledge = _Knowledge(
        KnowledgeContext(
            text=statement_text,
            statement_ids=("stmt-1",) if statement_text else (),
            source_refs=(ref,) if statement_text else (),
            context_revision="statement-r1",
        ),
        [],
    )
    memory = _Memory(
        FactRetrievalResult(
            text=fact_text,
            hits=(
                SimpleNamespace(
                    entry=SimpleNamespace(id="fact-1", content=fact_text.removeprefix("- "))
                ),
            )
            if fact_text
            else (),
            used_source_refs={"fact-1": (("ev-2", 1),)} if fact_text else {},
        ),
        [],
    )
    return ParticipationKnowledgeSelector(knowledge=knowledge, memory=memory), knowledge, memory


def test_selects_old_same_chat_knowledge_without_provider_call() -> None:
    selector, knowledge, memory = _selector(fact_text="Gegessen wird um sieben.")
    read_context, fact_context = _contexts()

    selected = selector.select(
        query="Wann ist der Stammtisch?\nWird davor gegessen?",
        read_context=read_context,
        fact_context=fact_context,
    )

    assert "Stammtisch donnerstags." in selected.text
    assert "Gegessen wird um sieben." in selected.text
    assert memory.calls[0]["lexical_only"] is True
    assert memory.calls[0]["read_context"].group_wide is True
    assert knowledge.calls[0]["group_wide"] is True
    assert len(knowledge.calls[0]["query"].text) <= 600
    facts_snapshot = selected.facts
    facts_snapshot.text = "mutated outside the envelope"
    assert selected.facts.text == "- Gegessen wird um sieben."


def test_selector_requires_identical_membership_in_both_reader_contexts() -> None:
    from dataclasses import replace

    selector, knowledge, memory = _selector()
    read_context, fact_context = _contexts()
    wider_fact_context = replace(
        fact_context, current_members=frozenset({"alice", "bob", "mallory"})
    )

    selected = selector.select(
        query="topic", read_context=read_context, fact_context=wider_fact_context
    )

    assert selected.text == ""
    assert not knowledge.calls
    assert not memory.calls


def test_query_budget_preserves_message_boundaries() -> None:
    selector, knowledge, _memory = _selector()
    read_context, fact_context = _contexts()
    oversized = "x" * 601

    selector.select(
        query=f"first message\n{oversized}\nlast message",
        read_context=read_context,
        fact_context=fact_context,
    )

    assert knowledge.calls[0]["query"].text == "first message"


def test_selection_revision_tracks_identity_revision() -> None:
    from dataclasses import replace

    selector, knowledge, _memory = _selector()
    read_context, fact_context = _contexts()
    before = selector.select(
        query="topic", read_context=read_context, fact_context=fact_context
    )
    knowledge.result = replace(knowledge.result, identity_revision=9)
    after = selector.select(
        query="topic", read_context=read_context, fact_context=fact_context
    )

    assert before.text == after.text
    assert before.revision != after.revision


def test_unknown_membership_skips_readers_and_requests_group_wide_access() -> None:
    selector, knowledge, memory = _selector("private fact")
    unknown_read, unknown_fact = _contexts(members=None)

    denied = selector.select(
        query="topic", read_context=unknown_read, fact_context=unknown_fact
    )

    assert denied.text == ""
    assert not knowledge.calls
    assert not memory.calls

    read_context, fact_context = _contexts()
    # The protected readers receive the group-wide contract; real reader tests below
    # verify author-only exclusion.
    selected = selector.select(
        query="topic", read_context=read_context, fact_context=fact_context
    )
    assert selected.text == "private fact"
    assert knowledge.calls[-1]["group_wide"] is True
    assert memory.calls[-1]["read_context"].group_wide is True


def test_statement_reader_excludes_author_only_for_group_wide_participation(
    tmp_path,
) -> None:
    harness = CaptureHarness(tmp_path)
    try:
        event_id = harness.observe("The private group detail is secret.", message_id="3EB0A01")
        source = harness.authority.verify_source_ref(event_id, 1)
        assert source is not None
        harness.authority.register_source(source, EvidenceAudience.author_only())
        harness.knowledge.capture(
            StatementCandidate(
                content="The private group detail is secret.",
                sources=(source,),
                extractor_version="test-v1",
                confidence=0.9,
            ),
            context=TrustedCaptureContext(
                request_id="author-only-test",
                policy_revision=1,
                capture_basis="historic_row",
                authorized_sources=(source,),
            ),
        )
        reader = TrustedReadContext(
            principal_id=f"{AUTHOR}@s.whatsapp.net",
            channel="whatsapp",
            chat_id=GROUP,
            recipient_principals=frozenset(
                {f"{AUTHOR}@s.whatsapp.net", "491511@s.whatsapp.net"}
            ),
            membership_revision="members-v1",
            policy_revision=1,
            purpose="proactive",
            now_ms=harness.now,
        )
        participation = harness.knowledge.recall(
            RecallQuery(text="private group detail", limit=3),
            context=reader,
            group_wide=True,
            max_chars=1200,
        )

        assert participation.text == ""
        assert participation.statement_ids == ()
        assert participation.source_refs == ()
    finally:
        harness.close()


def test_knowledge_reader_budget_keeps_whole_entries_and_source_refs(tmp_path) -> None:
    harness = CaptureHarness(tmp_path)
    try:
        contents = (
            "Stammtisch " + "A" * 680,
            "Stammtisch " + "B" * 680,
        )
        statement_ids = []
        event_ids = []
        for index, content in enumerate(contents):
            event_id = harness.observe(content, message_id=f"3EB0B{index:02d}")
            source = harness.authority.verify_source_ref(event_id, 1)
            assert source is not None
            result = harness.knowledge.capture(
                StatementCandidate(
                    content=content,
                    sources=(source,),
                    extractor_version="bounded-test-v1",
                    confidence=0.9,
                ),
                context=TrustedCaptureContext(
                    request_id=f"bounded-{index}",
                    policy_revision=1,
                    capture_basis="historic_row",
                    authorized_sources=(source,),
                ),
            )
            statement_ids.extend(result.statement_ids)
            event_ids.append(event_id)
        context = TrustedReadContext(
            principal_id=f"{AUTHOR}@s.whatsapp.net",
            channel="whatsapp",
            chat_id=GROUP,
            recipient_principals=frozenset(
                {f"{AUTHOR}@s.whatsapp.net", "491511@s.whatsapp.net"}
            ),
            membership_revision="members-v1",
            policy_revision=1,
            purpose="proactive",
            now_ms=harness.now,
        )

        result = harness.knowledge.recall(
            RecallQuery(text="Stammtisch", limit=3),
            context=context,
            group_wide=True,
            max_chars=700,
        )

        assert len(result.text) <= 700
        assert result.text in contents
        assert len(result.statement_ids) == 1
        assert len(result.source_refs) == 1
        assert result.source_refs[0].event_id in event_ids
    finally:
        harness.close()


def test_whole_entry_budget_keeps_metadata_aligned() -> None:
    selector, _knowledge, _memory = _selector("A" * 700, "C" * 700)
    read_context, fact_context = _contexts()

    selected = selector.select(
        query="topic", read_context=read_context, fact_context=fact_context
    )

    assert len(selected.text) <= 1200
    assert selected.text == "A" * 700
    assert selected.statements.statement_ids == ("stmt-1",)
    assert selected.statements.source_refs == (
        SourceRef("ev-1", 1, "whatsapp", CHAT, "alice", NOW - 7 * 86_400_000),
    )
    assert selected.facts.used_source_refs == {}


def test_shared_fact_partial_revocation_changes_selection() -> None:
    selector, _knowledge, memory = _selector(fact_text="Gegessen wird um sieben.")
    read_context, fact_context = _contexts()
    selected = selector.select(
        query="Essen", read_context=read_context, fact_context=fact_context
    )
    memory.result = FactRetrievalResult()

    current = selector.revalidate(
        selected, read_context=read_context, fact_context=fact_context
    )

    assert current.revision != selected.revision
    assert "Gegessen wird um sieben." not in current.text
    assert memory.calls[-1]["revalidate"] is not None
