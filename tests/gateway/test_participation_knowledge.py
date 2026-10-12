from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
from yeoman_gateway.knowledge._memory.shared_facts import FactReadContext, FactRetrievalResult
from yeoman_gateway.knowledge.models import (
    KnowledgeContext,
    SourceRef,
    TrustedReadContext,
)
from yeoman_gateway.processing.participation_knowledge import (
    ParticipationKnowledgeInvalidatedError,
    ParticipationKnowledgeReader,
    ParticipationKnowledgeReaders,
    ParticipationKnowledgeSelector,
    reader_key,
)

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
            source_refs_by_statement=(("stmt-1", (ref,)),) if statement_text else (),
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
    source = SourceRef("ev-multi", 1, "whatsapp", CHAT, "alice", NOW)
    selector = ParticipationKnowledgeSelector(
        knowledge=_Knowledge(
            KnowledgeContext(
                text=multiline,
                statement_ids=("stmt-multiline",),
                source_refs=(source,),
                entry_texts=(multiline,),
                source_refs_by_statement=(("stmt-multiline", (source,)),),
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


def _shared_reader_entry(
    statement_text: str, fact_text: str = ""
) -> tuple[KnowledgeContext, FactRetrievalResult]:
    """One reader's answer with stable record identity, so two readers can agree.

    Source refs carry the archived source event id, as the real store returns them;
    the record's own identity stays the statement id it was rendered from.
    """
    source = SourceRef("ev-shared", 1, "whatsapp", CHAT, "alice", NOW - 86_400_000)
    return (
        KnowledgeContext(
            text=statement_text,
            statement_ids=("stmt-shared",) if statement_text else (),
            source_refs=(source,) if statement_text else (),
            context_revision="stmt-shared-r1",
            source_refs_by_statement=(("stmt-shared", (source,)),) if statement_text else (),
        ),
        FactRetrievalResult(
            text=f"- {fact_text}" if fact_text else "",
            hits=(
                SimpleNamespace(entry=SimpleNamespace(id="fact-shared", content=fact_text)),
            )
            if fact_text
            else (),
            used_source_refs={"fact-shared": (("ev-fact", 1),)} if fact_text else {},
        ),
    )


def test_multi_author_intersection_drops_an_entry_one_reader_lost() -> None:
    """A record only one author still sees cannot survive the intersection."""
    shared = _shared_reader_entry(
        "Both authors still see this.", "Both authors share this fact."
    )
    per_reader = {"alice": shared, "bob": shared}
    selector, _knowledge, _memory = _multi_selector(per_reader)
    readers = ParticipationKnowledgeReaders(readers=(
        ParticipationKnowledgeReader(*_reader("alice")),
        ParticipationKnowledgeReader(*_reader("bob")),
    ))
    selected = selector.select_for_readers(query="topic", readers=readers)
    assert "Both authors still see this." in selected.text
    assert "Both authors share this fact." in selected.text
    assert [record.record_id for record in selected.records] == [
        "fact-shared",
        "stmt-shared",
    ]

    # Bob's answer loses the statement while Alice's still carries it.
    per_reader["bob"] = _shared_reader_entry("", "Both authors share this fact.")
    changed = selector.select_for_readers(query="topic", readers=readers)

    assert changed.text == "- Both authors share this fact."
    assert [record.record_id for record in changed.records] == ["fact-shared"]
    with pytest.raises(ParticipationKnowledgeInvalidatedError):
        selector.revalidate_for_readers(selected, readers=readers)


class _RecordingKnowledge:
    """The real statement store, recording the bounded reads it is asked for."""

    def __init__(self, store: Any) -> None:
        self._store = store
        self.calls: list[dict[str, Any]] = []

    def recall(self, query, **kwargs):
        self.calls.append({"query": query, **kwargs})
        return self._store.recall(query, **kwargs)

    def revalidate(self, result, **kwargs):
        return self._store.revalidate(result, **kwargs)


def test_multi_author_selection_refuses_a_reader_that_fails_the_purpose_check() -> None:
    """One author without the proactive read policy closes the whole multi-author read.

    No store read happens for the permitted author either: the remaining authors never
    cover for a denied one. The bundle entry point folds the policy-failing reader into
    the no-readers path, so it reports ``"empty"`` where the single-reader contract says
    ``"denied"``; the protection is the same either way.
    """
    from dataclasses import replace

    per_reader = _per_reader(shared_fact="Both hear this.")
    selector, knowledge, memory = _multi_selector(per_reader)
    reply_reader = ParticipationKnowledgeReader(
        replace(_reader("bob")[0], purpose="reply"), _reader("bob")[1]
    )
    readers = ParticipationKnowledgeReaders(readers=(
        ParticipationKnowledgeReader(*_reader("alice")),
        reply_reader,
    ))

    selected = selector.select_for_readers(query="topic", readers=readers)
    single = selector.select(
        query="topic",
        read_context=reply_reader.read_context,
        fact_context=reply_reader.fact_context,
    )

    assert selected.text == ""
    # A refused reader bundle is a refusal, not a no-hit, and it must agree with what
    # the single-reader entry point reports for the same reader.
    assert selected.reason == "denied"
    assert not knowledge.principals and not memory.principals
    assert single.text == ""
    assert single.reason == "denied"
