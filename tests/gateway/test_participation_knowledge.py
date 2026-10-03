from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
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
from yeoman_gateway.processing.participation_knowledge import (
    ParticipationKnowledgeInvalidatedError,
    ParticipationKnowledgeReader,
    ParticipationKnowledgeReaders,
    ParticipationKnowledgeSelector,
    reader_key,
)

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


def test_selector_requests_topical_statement_match() -> None:
    selector, knowledge, _memory = _selector()
    read_context, fact_context = _contexts()

    selector.select(query="topic", read_context=read_context, fact_context=fact_context)

    assert knowledge.calls[0]["require_match"] is True


def test_multiline_statement_entry_remains_whole_with_identity_and_source() -> None:
    multiline = "Zusammenkunft am Donnerstag.\nBeginn ist um 19 Uhr."
    selector = ParticipationKnowledgeSelector(
        knowledge=_Knowledge(
            KnowledgeContext(
                text=multiline,
                statement_ids=("stmt-multiline",),
                source_refs=(SourceRef("ev-multi", 1, "whatsapp", CHAT, "alice", NOW),),
                entry_texts=(multiline,),
            ),
            [],
        ),
        memory=_Memory(FactRetrievalResult(), []),
    )
    read_context, fact_context = _contexts()

    selected = selector.select(
        query="Wann beginnt die Zusammenkunft?",
        read_context=read_context,
        fact_context=fact_context,
    )

    assert selected.text == multiline
    assert selected.statements.statement_ids == ("stmt-multiline",)
    assert tuple(ref.event_id for ref in selected.statements.source_refs) == ("ev-multi",)


def _capture_test_statement(harness: CaptureHarness, text: str, key: str):
    event_id = harness.observe(text, message_id=key)
    source = harness.authority.verify_source_ref(event_id, 1)
    assert source is not None
    return harness.knowledge.capture(
        StatementCandidate(
            content=text,
            sources=(source,),
            extractor_version="participation-review-v1",
            confidence=0.9,
        ),
        context=TrustedCaptureContext(
            request_id=f"participation-{key}",
            policy_revision=1,
            capture_basis="historic_row",
            authorized_sources=(source,),
        ),
    )


def test_owned_selector_returns_empty_for_unrelated_and_multilingual_zero_hit(
    tmp_path,
) -> None:
    harness = CaptureHarness(tmp_path)
    try:
        _capture_test_statement(harness, "Stammtisch Donnerstag um acht.", "3EB0C01")
        members = frozenset({f"{AUTHOR}@s.whatsapp.net", "491511@s.whatsapp.net"})
        read_context = TrustedReadContext(
            principal_id=f"{AUTHOR}@s.whatsapp.net",
            channel="whatsapp",
            chat_id=GROUP,
            recipient_principals=members,
            membership_revision="captured-members-v1",
            policy_revision=1,
            purpose="proactive",
            now_ms=harness.now,
            is_direct=False,
        )
        fact_context = FactReadContext(
            principal_id=read_context.principal_id,
            chat_scope_key=f"channel:whatsapp:chat:{GROUP}",
            current_members=members,
            epoch=1,
            now_ms=harness.now,
        )
        selector = ParticipationKnowledgeSelector(
            knowledge=harness.knowledge,
            memory=_Memory(FactRetrievalResult(), []),
        )

        unrelated = selector.select(
            query="quasar nebula xyzzy",
            read_context=read_context,
            fact_context=fact_context,
        )
        multilingual = selector.select(
            query="東京の集合時間",
            read_context=read_context,
            fact_context=fact_context,
        )

        assert unrelated.text == ""
        assert multilingual.text == ""
    finally:
        harness.close()


def test_owned_multiline_statement_keeps_complete_text_and_metadata_through_revalidation(
    tmp_path,
) -> None:
    harness = CaptureHarness(tmp_path)
    multiline = "Zusammenkunft am Donnerstag.\nBeginn ist um 19 Uhr."
    singleline = "Beginn der zweiten Zusammenkunft ist um 20 Uhr."
    try:
        _capture_test_statement(harness, multiline, "3EB0C02")
        _capture_test_statement(harness, singleline, "3EB0C03")
        members = frozenset({f"{AUTHOR}@s.whatsapp.net", "491511@s.whatsapp.net"})
        read_context = TrustedReadContext(
            principal_id=f"{AUTHOR}@s.whatsapp.net",
            channel="whatsapp",
            chat_id=GROUP,
            recipient_principals=members,
            membership_revision="captured-members-v1",
            policy_revision=1,
            purpose="proactive",
            now_ms=harness.now,
            is_direct=False,
        )
        fact_context = FactReadContext(
            principal_id=read_context.principal_id,
            chat_scope_key=f"channel:whatsapp:chat:{GROUP}",
            current_members=members,
            epoch=1,
            now_ms=harness.now,
        )
        selector = ParticipationKnowledgeSelector(
            knowledge=harness.knowledge,
            memory=_Memory(FactRetrievalResult(), []),
        )

        selected = selector.select(
            query="Zusammenkunft Donnerstag Beginn",
            read_context=read_context,
            fact_context=fact_context,
        )
        current = selector.revalidate(
            selected,
            read_context=read_context,
            fact_context=fact_context,
        )

        assert multiline in selected.text and singleline in selected.text
        assert set(selected.statements.entry_texts) == {multiline, singleline}
        assert len(selected.statements.statement_ids) == 2
        assert len(selected.statements.source_refs) == 2
        assert multiline in current.text and singleline in current.text
        assert current.statements.statement_ids == selected.statements.statement_ids
        assert current.statements.source_refs == selected.statements.source_refs
    finally:
        harness.close()


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


# -- multi-author protected recall -------------------------------------------------


def _reader(principal: str):
    """One verified reader over the same chat and the same current membership."""
    return (
        TrustedReadContext(
            principal_id=principal,
            channel="whatsapp",
            chat_id=CHAT,
            recipient_principals=MEMBERS,
            membership_revision="membership-v3",
            policy_revision=8,
            purpose="proactive",
            now_ms=NOW,
        ),
        FactReadContext(
            principal_id=principal,
            chat_scope_key=f"channel:whatsapp:chat:{CHAT}",
            current_members=MEMBERS,
            epoch=4,
            now_ms=NOW,
        ),
    )


def _multi_selector(per_reader):
    """A store double whose knowledge and memory answers depend on the reader."""
    knowledge = _PerReader(per_reader, "knowledge")
    memory = _PerReader(per_reader, "memory")
    return (
        ParticipationKnowledgeSelector(knowledge=knowledge, memory=memory),
        knowledge,
        memory,
    )


class _PerReader:
    def __init__(self, per_reader, kind: str) -> None:
        self._per_reader = per_reader
        self._kind = kind
        self.principals: list[str] = []

    def _target(self, context):
        principal = str(context.principal_id)
        self.principals.append(principal)
        target = self._per_reader[principal]
        return target[0] if self._kind == "knowledge" else target[1]

    def recall(self, query, **kwargs):
        return self._target(kwargs["context"])

    def revalidate(self, result, **kwargs):
        return self._target(kwargs["context"])

    def retrieve_for_context(self, **kwargs):
        return self._target(kwargs["read_context"])

    def revalidate_for_context(self, result, **kwargs):
        return self._target(kwargs["read_context"])


def _per_reader(*, alice_statements=(), bob_statements=(), shared_fact: str = ""):
    def knowledge(text: str, statement_id: str) -> KnowledgeContext:
        return KnowledgeContext(
            text=text,
            statement_ids=(statement_id,) if text else (),
            source_refs=(
                SourceRef(statement_id, 1, "whatsapp", CHAT, "alice", NOW - 86_400_000),
            )
            if text
            else (),
            context_revision=f"{statement_id}-r1",
        )

    def memory(text: str) -> FactRetrievalResult:
        return FactRetrievalResult(
            text=f"- {text}" if text else "",
            hits=(
                SimpleNamespace(entry=SimpleNamespace(id="fact-shared", content=text)),
            )
            if text
            else (),
            used_source_refs={"fact-shared": (("ev-shared", 1),)} if text else {},
        )

    return {
        "alice": (knowledge(alice_statements, "stmt-alice"), memory(shared_fact)),
        "bob": (knowledge(bob_statements, "stmt-bob"), memory(shared_fact)),
    }


def test_multi_author_selection_keeps_only_the_common_records() -> None:
    """An entry only one author may read is dropped, never unioned."""
    per_reader = _per_reader(
        alice_statements="Alice only hears this.",
        bob_statements="Bob only hears this.",
        shared_fact="Both hear this.",
    )
    selector, knowledge, memory = _multi_selector(per_reader)
    readers = ParticipationKnowledgeReaders(readers=(
        ParticipationKnowledgeReader(*_reader("alice")),
        ParticipationKnowledgeReader(*_reader("bob")),
    ))

    selected = selector.select_for_readers(query="topic", readers=readers)

    assert "Alice only hears this." not in selected.text
    assert "Bob only hears this." not in selected.text
    assert "Both hear this." in selected.text
    assert sorted(knowledge.principals) == ["alice", "bob"]
    assert selected.reader_keys == tuple(
        sorted(reader_key(*_reader(principal)) for principal in ("alice", "bob"))
    )


def test_multi_author_selection_is_independent_of_author_order() -> None:
    per_reader = _per_reader(shared_fact="Both hear this.")
    selector, _knowledge, _memory = _multi_selector(per_reader)
    alice = ParticipationKnowledgeReader(*_reader("alice"))
    bob = ParticipationKnowledgeReader(*_reader("bob"))

    forward = selector.select_for_readers(
        query="topic", readers=ParticipationKnowledgeReaders(readers=(alice, bob))
    )
    reverse = selector.select_for_readers(
        query="topic", readers=ParticipationKnowledgeReaders(readers=(bob, alice))
    )

    assert forward.text == reverse.text == "- Both hear this."
    assert forward.records == reverse.records
    assert forward.revision == reverse.revision


def test_multi_author_selection_reads_a_repeated_author_once() -> None:
    per_reader = _per_reader(shared_fact="Both hear this.")
    selector, knowledge, _memory = _multi_selector(per_reader)
    alice = ParticipationKnowledgeReader(*_reader("alice"))

    selected = selector.select_for_readers(
        query="topic", readers=ParticipationKnowledgeReaders(readers=(alice, alice))
    )

    assert selected.text == "- Both hear this."
    assert knowledge.principals == ["alice"]


def test_revalidation_for_readers_rejects_a_changed_record() -> None:
    per_reader = _per_reader(shared_fact="Both hear this.")
    selector, _knowledge, _memory = _multi_selector(per_reader)
    readers = ParticipationKnowledgeReaders(readers=(
        ParticipationKnowledgeReader(*_reader("alice")),
        ParticipationKnowledgeReader(*_reader("bob")),
    ))
    selected = selector.select_for_readers(query="topic", readers=readers)

    per_reader["bob"] = (
        per_reader["bob"][0],
        FactRetrievalResult(
            text="- Both hear this, restated.",
            hits=(
                SimpleNamespace(
                    entry=SimpleNamespace(id="fact-shared", content="Both hear this, restated.")
                ),
            ),
            used_source_refs={"fact-shared": (("ev-shared", 1),)},
        ),
    )
    changed = selector.select_for_readers(query="topic", readers=readers)

    assert changed.records != selected.records
    with pytest.raises(ParticipationKnowledgeInvalidatedError):
        selector.revalidate_for_readers(selected, readers=readers)


def test_revalidation_for_readers_rejects_a_departed_author() -> None:
    per_reader = _per_reader(shared_fact="Both hear this.")
    selector, _knowledge, _memory = _multi_selector(per_reader)
    readers = ParticipationKnowledgeReaders(readers=(
        ParticipationKnowledgeReader(*_reader("alice")),
        ParticipationKnowledgeReader(*_reader("bob")),
    ))
    selected = selector.select_for_readers(query="topic", readers=readers)

    alice_only = ParticipationKnowledgeReaders(
        readers=(ParticipationKnowledgeReader(*_reader("alice")),)
    )
    with pytest.raises(ParticipationKnowledgeInvalidatedError):
        selector.revalidate_for_readers(selected, readers=alice_only)


def test_revalidation_for_readers_accepts_an_unchanged_selection() -> None:
    per_reader = _per_reader(shared_fact="Both hear this.")
    selector, _knowledge, _memory = _multi_selector(per_reader)
    readers = ParticipationKnowledgeReaders(readers=(
        ParticipationKnowledgeReader(*_reader("alice")),
        ParticipationKnowledgeReader(*_reader("bob")),
    ))
    selected = selector.select_for_readers(query="topic", readers=readers)

    current = selector.revalidate_for_readers(selected, readers=readers)

    assert current.records == selected.records
    assert current.text == selected.text
