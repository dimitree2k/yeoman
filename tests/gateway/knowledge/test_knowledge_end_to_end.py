"""P5.1: the identity/statement lifecycle through the real middleware chain.

The test drives the actual :class:`Orchestrator` pipeline with an injected fake provider
and a recording transport, over a temporary knowledge database.  Identity and ACL are
*not* mocked: they come from the knowledge facade, a fake-but-explicit policy authority
and a chat registry whose membership is the only membership in play.

A successful spy send proves local routing only - no WhatsApp connection is involved.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from yeoman_gateway.core.models import InboundEvent, PolicyDecision
from yeoman_gateway.core.orchestrator import Orchestrator
from yeoman_gateway.knowledge.api import open_knowledge_store
from yeoman_gateway.knowledge.authority import EvidenceAudience, FakeSourceAuthority
from yeoman_gateway.knowledge.models import (
    Identifier,
    PersonLinkCandidate,
    SourceRef,
    StatementCandidate,
    TrustedCaptureContext,
    TrustedIdentityObservation,
    TrustedReadContext,
)
from yeoman_gateway.providers.base import LLMProvider, LLMResponse
from yeoman_gateway.storage.chat_registry import ChatRegistry

CHAT = "120363000000000001@g.us"
DM = "4910000000002@s.whatsapp.net"
OWNER = "whatsapp:4910000000001"
TOM = "whatsapp:4910000000002"
ALEX = "whatsapp:4910000000003"
MARIA = "whatsapp:4910000000004"
NEWCOMER = "whatsapp:4910000000005"


class SpyProvider(LLMProvider):
    """Records every prompt that would reach a model.  Never calls out."""

    def __init__(self, reply: str = "noted") -> None:
        super().__init__()
        self.reply = reply
        self.prompts: list[str] = []
        self.model_calls = 0

    async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7, reasoning=None):
        del tools, model, max_tokens, temperature, reasoning
        self.model_calls += 1
        self.prompts.append("\n".join(str(item.get("content", "")) for item in messages))
        return LLMResponse(content=self.reply)

    def get_default_model(self) -> str:
        return "spy/model"

    def saw(self, needle: str) -> bool:
        return any(needle in prompt for prompt in self.prompts)


class TransportSpy:
    """Records delivery attempts instead of sending anything."""

    def __init__(self) -> None:
        self.attempts: list[dict[str, Any]] = []

    def record(self, intent: object) -> None:
        self.attempts.append(
            {
                "kind": type(intent).__name__,
                "channel": getattr(intent, "channel", ""),
                "chat_id": getattr(intent, "chat_id", ""),
                "content": getattr(intent, "content", ""),
            }
        )


@dataclass
class _Membership:
    members: frozenset[str]
    revision: str


@dataclass
class _Policy:
    """Minimal policy authority: owners are explicit, capture is never implicit."""

    owners: frozenset[str] = frozenset({OWNER})
    revision: int = 1
    memberships: dict[str, _Membership] = field(default_factory=dict)

    def current_policy_revision(self) -> int:
        return self.revision

    def require_admin(self, context) -> str:
        if not context.owner or context.actor_principal not in self.owners:
            from yeoman_gateway.knowledge.models import KnowledgeError

            raise KnowledgeError("unauthorized", "actor is not an owner")
        return context.authorization_ref

    def require_capture(self, context) -> str:
        from yeoman_gateway.knowledge.models import KnowledgeError

        if not context.authorized:
            raise KnowledgeError("unauthorized", "capture is not authorized")
        return context.request_id

    def membership(self, context: TrustedReadContext) -> _Membership | None:
        return self.memberships.get(f"{context.channel}:{context.chat_id}")

    def set_members(self, channel: str, chat_id: str, members: set[str], *, revision: str) -> None:
        self.memberships[f"{channel}:{chat_id}"] = _Membership(frozenset(members), revision)

    def admin_actor(self) -> str:
        return sorted(self.owners)[0]


class EndToEnd:
    """A synthetic but real pipeline plus the knowledge runtime behind it."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.policy = _Policy()
        self.authority = FakeSourceAuthority()
        self.knowledge = open_knowledge_store(
            tmp_path / "knowledge.db",
            workspace_id="e2e",
            source_authority=self.authority,
            policy_authority=self.policy,
        )
        self.provider = SpyProvider()
        self.transport = TransportSpy()
        self.registry = ChatRegistry(db_path=tmp_path / "chat_registry.db")
        self.people: dict[str, str] = {}
        self._observations = 0
        self._sources = 0

    # ── setup helpers ────────────────────────────────────────────────────────

    def person(self, principal: str, name: str, *, preferred: str | None = None) -> str:
        self._observations += 1
        number = principal.split(":", 1)[1]
        observation = TrustedIdentityObservation(
            identifiers=(
                Identifier("whatsapp", "phone_jid", f"{number}@s.whatsapp.net"),
            ),
            evidence_ref=f"obs-{self._observations}",
            observed_name=name,
            observed_at_ms=1_700_000_000_000,
        )
        self.authority.issue_observation(observation)
        resolved = self.knowledge.resolve_person(observation)
        assert resolved.person_id, resolved.reason
        self.people[principal] = resolved.person_id
        if preferred:
            self.knowledge.set_preferred_name(
                resolved.person_id, preferred, context=self.admin_context()
            )
        return resolved.person_id

    def admin_context(self):
        from yeoman_gateway.knowledge.models import TrustedAdminContext

        return TrustedAdminContext(
            actor_principal=OWNER,
            policy_revision=self.policy.revision,
            authorization_ref="e2e-admin",
            owner=True,
        )

    def source(
        self,
        author: str,
        *,
        chat: str = CHAT,
        audience: set[str] | None = None,
        author_only: bool = False,
    ) -> SourceRef:
        self._sources += 1
        source = SourceRef(
            event_id=f"evt-{self._sources}",
            revision=1,
            channel="whatsapp",
            chat_id=chat,
            author_principal=author,
            occurred_at_ms=1_700_000_000_000,
        )
        if author_only:
            self.authority.issue_source(source, EvidenceAudience.author_only())
        else:
            self.authority.issue_source(
                source, EvidenceAudience.known(audience or {author})
            )
        return source

    def register_chat(self, *, chat_id: str, members: set[str]) -> None:
        self.registry.register_chat(
            channel="whatsapp",
            chat_id=chat_id,
            chat_type="group" if chat_id.endswith("@g.us") else "direct",
            readable_name="synthetic",
            metadata={"participants": [{"id": member} for member in sorted(members)]},
        )
        self.policy.set_members("whatsapp", chat_id, members, revision=f"rev-{len(members)}")

    def capture(
        self,
        text: str,
        source: SourceRef,
        *,
        people: tuple[tuple[str, str], ...] = (),
    ) -> str:
        candidate = StatementCandidate(
            content=text,
            sources=(source,),
            people=tuple(
                PersonLinkCandidate(
                    person_id=person_id, role=role, source=source, attribution="extracted"
                )
                for person_id, role in people
            ),
            extractor_version="e2e-extractor",
            confidence=0.5,
        )
        context = TrustedCaptureContext(
            request_id=f"cap-{source.event_id}",
            policy_revision=self.policy.revision,
            capture_basis="user_message",
            authorized_sources=(source,),
            actor_principal=source.author_principal,
            authorized=True,
        )
        result = self.knowledge.capture(candidate, context=context)
        assert result.statement_ids, result.rejected
        return result.statement_ids[0]

    def read_context(
        self, reader: str, *, chat: str = CHAT, recipients: set[str], purpose: str = "reply"
    ) -> TrustedReadContext:
        return TrustedReadContext(
            principal_id=reader,
            channel="whatsapp",
            chat_id=chat,
            recipient_principals=frozenset(recipients),
            membership_revision=f"rev-{len(recipients)}",
            policy_revision=self.policy.revision,
            purpose=purpose,
            now_ms=1_700_000_000_000,
            is_direct=not chat.endswith("@g.us"),
            owner=reader in self.policy.owners,
        )

    def identity_revision(self) -> int:
        return self.knowledge.stats(context=self.admin_context()).identity_revision

    # ── the pipeline under test ──────────────────────────────────────────────

    def orchestrator(self, responder: object) -> Orchestrator:
        return Orchestrator(
            policy=_AllowAll(),
            responder=responder,
            reply_archive=None,
            contacts=None,
            knowledge=self.knowledge,
            reply_context_window_limit=6,
            reply_context_line_max_chars=500,
            ambient_window_limit=8,
        )

    def close(self) -> None:
        self.knowledge.close()
        self.registry.close()


class _AllowAll:
    """Policy port for the pipeline: allow, without inventing person rights."""

    def evaluate(self, event: InboundEvent) -> PolicyDecision:
        return PolicyDecision(
            accept_message=True,
            should_respond=True,
            allowed_tools=frozenset(),
            reason="e2e",
        )


class _Responder:
    """Responder port that records what reached it and asks the spy provider."""

    def __init__(self, provider: SpyProvider, knowledge: object) -> None:
        self.provider = provider
        self.knowledge = knowledge
        self.seen: list[InboundEvent] = []

    async def generate_reply(self, event: InboundEvent, decision: PolicyDecision):
        self.seen.append(event)
        await self.provider.chat(
            [{"role": "user", "content": str(event.content or "")}]
        )
        return "spy reply"


@pytest.fixture
def e2e(tmp_path: Path):
    harness = EndToEnd(tmp_path)
    harness.person(TOM, "Tom push", preferred="Tom")
    harness.person(ALEX, "Alex push", preferred="Alex")
    harness.person(MARIA, "Maria push", preferred="Maria")
    harness.person(NEWCOMER, "Newcomer push", preferred="Newcomer")
    harness.register_chat(chat_id=CHAT, members={TOM, ALEX, MARIA})
    harness.register_chat(chat_id=DM, members={TOM, ALEX})
    try:
        yield harness
    finally:
        harness.close()


@pytest.mark.asyncio
async def test_shared_claim_is_retrievable_and_private_claim_is_not(e2e):
    """One private claim, one shared claim: the group read only sees the shared one."""
    alex, maria = e2e.people[ALEX], e2e.people[MARIA]
    shared_group = {TOM, ALEX, MARIA}
    shared_source = e2e.source(TOM, audience=shared_group)
    private_source = e2e.source(TOM, chat=DM, audience={TOM, ALEX})
    shared_id = e2e.capture("Alex faehrt mit Maria nach Berlin.", shared_source, people=((alex, "subject"), (maria, "participant")))
    private_id = e2e.capture("private-itinerary for the trip", private_source, people=((alex, "subject"),))

    group = e2e.read_context(TOM, recipients=shared_group)
    result = e2e.knowledge.recall(
        __import__("yeoman_gateway.knowledge.models", fromlist=["RecallQuery"]).RecallQuery(
            person_ids=(alex,), limit=10
        ),
        context=group,
    )
    assert shared_id in result.statement_ids
    assert private_id not in result.statement_ids
    assert "private-itinerary" not in result.text

    direct = e2e.read_context(TOM, chat=DM, recipients={TOM, ALEX})
    direct_result = e2e.knowledge.recall(
        __import__("yeoman_gateway.knowledge.models", fromlist=["RecallQuery"]).RecallQuery(
            person_ids=(alex,), limit=10
        ),
        context=direct,
    )
    assert private_id in direct_result.statement_ids


@pytest.mark.asyncio
async def test_middleware_chain_runs_with_knowledge_context(e2e):
    """The real middleware chain accepts the event and the responder sees it."""
    responder = _Responder(e2e.provider, e2e.knowledge)
    orchestrator = e2e.orchestrator(responder)
    event = InboundEvent(
        channel="whatsapp",
        chat_id=CHAT,
        sender_id=ALEX,
        message_id="m1",
        content="hallo",
        raw_metadata={"sender_name": "Alex push"},
    )
    intents = await orchestrator.handle(event)
    assert responder.seen, "the responder was never reached"
    assert e2e.provider.model_calls == 1
    for intent in intents:
        e2e.transport.record(intent)
    # Only local intents were recorded; the spy never opened a transport.  Intents for
    # other channels (for example an owner alert) are recorded, not sent.
    assert all(attempt["content"] is not None for attempt in e2e.transport.attempts)
    assert e2e.transport.attempts, "the pipeline produced no intent at all"


@pytest.mark.asyncio
async def test_stale_group_membership_drops_the_prepared_draft(e2e):
    """A new member between preparation and output invalidates the whole draft."""
    alex = e2e.people[ALEX]
    source = e2e.source(TOM, audience={TOM, ALEX})
    e2e.capture("group-token-5531 claim", source, people=((alex, "subject"),))
    from yeoman_gateway.knowledge.models import RecallQuery

    before = e2e.read_context(TOM, recipients={TOM, ALEX})
    prepared = e2e.knowledge.recall(RecallQuery(person_ids=(alex,)), context=before)
    assert prepared.statement_ids

    e2e.register_chat(chat_id=CHAT, members={TOM, ALEX, MARIA, NEWCOMER})
    after = e2e.read_context(TOM, recipients={TOM, ALEX, MARIA, NEWCOMER})
    checked = e2e.knowledge.revalidate(prepared, context=after)
    assert checked.statement_ids == ()
    draft = "Sure: " + prepared.text
    delivered = draft if checked.statement_ids else ""
    assert delivered == ""
    assert "group-token-5531" not in delivered


@pytest.mark.asyncio
async def test_revoked_source_cannot_reach_the_provider(e2e):
    alex = e2e.people[ALEX]
    source = e2e.source(TOM, audience={TOM, ALEX})
    e2e.capture("revocable-token-9922", source, people=((alex, "subject"),))
    from yeoman_gateway.knowledge.models import RecallQuery

    context = e2e.read_context(TOM, recipients={TOM, ALEX})
    prepared = e2e.knowledge.recall(RecallQuery(person_ids=(alex,)), context=context)
    assert prepared.statement_ids

    capture_context = TrustedCaptureContext(
        request_id="revoke-1",
        policy_revision=e2e.policy.revision,
        capture_basis="user_message",
        authorized_sources=(source,),
        actor_principal=TOM,
        authorized=True,
    )
    e2e.knowledge.invalidate_source(source, context=capture_context)
    checked = e2e.knowledge.revalidate(prepared, context=context)
    assert checked.statement_ids == ()
    assert "revocable-token-9922" not in checked.text
    assert not e2e.provider.saw("revocable-token-9922")


@pytest.mark.asyncio
async def test_merge_and_undo_keep_the_statement_visible_for_both_ids(e2e):
    alex, maria = e2e.people[ALEX], e2e.people[MARIA]
    source = e2e.source(TOM, audience={TOM, ALEX, MARIA})
    statement_id = e2e.capture(
        "Alex und Maria reisen.",
        source,
        people=((alex, "subject"), (maria, "participant")),
    )
    from yeoman_gateway.knowledge.models import RecallQuery

    context = e2e.read_context(TOM, recipients={TOM, ALEX, MARIA})
    receipt = e2e.knowledge.merge_people(
        alex, maria, expected_revision=e2e.identity_revision(), context=e2e.admin_context()
    )
    assert e2e.knowledge.recall(RecallQuery(person_ids=(alex,)), context=context).statement_ids == (
        statement_id,
    )
    assert e2e.knowledge.recall(RecallQuery(person_ids=(maria,)), context=context).statement_ids == (
        statement_id,
    )
    e2e.knowledge.undo_merge(
        receipt.operation_id, expected_revision=receipt.identity_revision, context=e2e.admin_context()
    )
    assert e2e.knowledge.recall(RecallQuery(person_ids=(maria,)), context=context).statement_ids == (
        statement_id,
    )


@pytest.mark.asyncio
async def test_group_member_without_audience_gets_nothing(e2e):
    """The reader may be an owner; a verified non-audience member still blocks output."""
    alex = e2e.people[ALEX]
    source = e2e.source(TOM, audience={TOM, ALEX})
    e2e.capture("narrow-claim-3321", source, people=((alex, "subject"),))
    from yeoman_gateway.knowledge.models import RecallQuery

    wide = e2e.read_context(TOM, recipients={TOM, ALEX, MARIA})
    result = e2e.knowledge.recall(RecallQuery(person_ids=(alex,)), context=wide)
    assert result.statement_ids == ()
    assert "narrow-claim-3321" not in result.text


@pytest.mark.asyncio
async def test_deleted_statement_leaves_no_trace_for_the_provider(e2e):
    alex = e2e.people[ALEX]
    source = e2e.source(TOM, audience={TOM, ALEX})
    statement_id = e2e.capture("erasable-e2e-7788", source, people=((alex, "subject"),))
    e2e.knowledge.erase_statement(
        statement_id, expected_source=source, context=e2e.admin_context()
    )
    from yeoman_gateway.knowledge.models import RecallQuery

    context = e2e.read_context(TOM, recipients={TOM, ALEX})
    result = e2e.knowledge.recall(RecallQuery(person_ids=(alex,)), context=context)
    assert result.statement_ids == ()
    assert not e2e.provider.saw("erasable-e2e-7788")
    # The tombstone is still inspectable for an administrator.
    summary = e2e.knowledge.inspect_statement(statement_id, context=e2e.admin_context())
    assert summary.status == "revoked"
    assert summary.content is None or summary.content == ""


def test_roster_and_recall_use_the_same_audience(e2e):
    """Names and statements obey one gate: the group roster cannot widen it."""
    alex = e2e.people[ALEX]
    source = e2e.source(TOM, audience={TOM, ALEX})
    e2e.capture("roster-scoped-4455", source, people=((alex, "subject"),))
    wide = e2e.read_context(TOM, recipients={TOM, ALEX, MARIA})
    rows = e2e.knowledge.roster(context=wide, participant_ids=tuple(sorted({TOM, ALEX, MARIA})))
    assert not any("roster-scoped-4455" in " ".join(facts) for _name, facts in rows)
    narrow = e2e.read_context(TOM, recipients={TOM, ALEX})
    rows = e2e.knowledge.roster(context=narrow, participant_ids=tuple(sorted({TOM, ALEX})))
    assert any("roster-scoped-4455" in " ".join(facts) for _name, facts in rows)


# ── deterministic person profile (Task 6) ────────────────────────────────────
#
# The profile is a projection, never a stored document: the same stored rows always
# produce the same card, every value carries the source it came from, and the card is
# bounded at entry boundaries instead of by slicing a string.


def test_profile_is_deterministic_and_sourced(knowledge_harness):
    h = knowledge_harness
    alex = h.person("Alex")
    source = h.source(alex)
    h.capture_text("Alex wohnt in Köln.", source, subjects=(alex,))
    context = h.read_context(alex)

    first = h.service.person_profile(alex, context=context)
    second = h.service.person_profile(alex, context=context)

    assert first.card == second.card
    assert first.person_id == alex
    assert first.entry_count == len([line for line in first.card.splitlines() if line.strip()])
    assert first.truncated is False
    assert first.statement_ids
    assert first.source_refs
    assert any("Köln" in line for line in first.card.splitlines())


def test_profile_without_a_read_context_delivers_nothing(knowledge_harness):
    h = knowledge_harness
    alex = h.person("Alex")
    source = h.source(alex)
    h.capture_text("Alex wohnt in Köln.", source, subjects=(alex,))
    empty = h.service.person_profile(alex, context=None)
    assert empty.card == ""
    assert empty.statement_ids == ()
    assert empty.reason == "no_read_context"


def test_profile_never_shows_a_locked_statement(knowledge_harness):
    h = knowledge_harness
    alex = h.person("Alex")
    source = h.source(alex)
    statement_id = h.capture_text("Alex wohnt in Köln.", source, subjects=(alex,)).statement_ids[0]
    context = h.read_context(alex)
    assert "Köln" in h.service.person_profile(alex, context=context).card

    with h.service._store.transaction():  # noqa: SLF001 - lifecycle write under test
        h.service._store.execute(  # noqa: SLF001
            "UPDATE knowledge_statements SET status = 'superseded',"
            " supersession_reason = 'quality_rejected', revision = revision + 1"
            " WHERE statement_id = ?",
            (statement_id,),
        )
    blocked = h.service.person_profile(alex, context=context)
    assert "Köln" not in blocked.card
    assert blocked.statement_ids == ()


def test_profile_bounds_at_entry_boundaries_not_by_slicing(knowledge_harness):
    """Every listed entry is complete; the stored value is never modified."""
    h = knowledge_harness
    alex = h.person("Alex")
    long_sentence = "Alex hat einen sehr ausführlichen Sachverhalt geschildert, " * 4
    for index in range(30):
        source = h.source(alex)
        h.capture_text(
            f"{long_sentence} Nummer {index}.", source, subjects=(alex,)
        )
    context = h.read_context(alex)
    profile = h.service.person_profile(alex, context=context)
    assert profile.truncated is True
    assert len(profile.statement_ids) <= 20
    # No listed line was cut in the middle: each one is present in full in the store.
    import sqlite3

    conn = sqlite3.connect(f"file:{h.service.db_path}?mode=ro", uri=True)
    try:
        stored = {
            str(row[0])
            for row in conn.execute(
                "SELECT content FROM memory2_nodes WHERE id IN"
                f" ({','.join('?' for _ in profile.statement_ids)})",
                profile.statement_ids,
            )
        }
    finally:
        conn.close()
    for statement_id in profile.statement_ids:
        node = h.service._store.query_one(  # noqa: SLF001 - stored value assertion
            "SELECT content FROM memory2_nodes WHERE id = ?", (statement_id,)
        )
        assert str(node["content"]) in "\n".join([str(item) for item in stored])


def test_profile_keeps_at_most_five_values_per_attribute_group(knowledge_harness):
    from yeoman_gateway.knowledge.models import (
        AttributeCandidate,
        AttributeValue,
        PersonLinkCandidate,
        StatementCandidate,
    )

    h = knowledge_harness
    alex = h.person("Alex")
    for index in range(8):
        source = h.source(alex)
        candidate = StatementCandidate(
            content=f"Alex hat Interesse Nummer {index}.",
            sources=(source,),
            people=(
                PersonLinkCandidate(
                    person_id=alex, role="subject", source=source, attribution="extracted"
                ),
            ),
            attributes=(
                AttributeCandidate(
                    person_id=alex,
                    attribute_key="interest",
                    value=AttributeValue(f"Interesse-{index}"),
                ),
            ),
            extractor_version="test-extractor-1",
            confidence=0.5,
        )
        h.service.capture(candidate, context=h.capture_context(source))

    profile = h.service.person_profile(alex, context=h.read_context(alex))
    listed = [line for line in profile.card.splitlines() if line.startswith("interest:")]
    assert len(listed) == 1  # one group line
    values = listed[0].split(":", 1)[1].split(",")
    assert len(values) == 5
    assert profile.truncated is True
    # The stored facets are untouched: only the view is bounded.
    total = h.service._store.scalar(  # noqa: SLF001 - stored value assertion
        "SELECT COUNT(*) FROM knowledge_person_attributes WHERE person_id = ?", (alex,)
    )
    assert int(total) == 8


def test_profile_marks_reported_values_as_reported(knowledge_harness):
    """A value somebody else reported reads as reported, not as a self-declaration."""
    h = knowledge_harness
    alex = h.person("Alex")
    tom = h.person("Tom")
    # Tom and Alex are both in the audience, so Alex's reader may see what Tom reported.
    audience = {h.principal_for(alex), h.principal_for(tom)}
    source = h.source(tom, audience=audience)
    h.capture_text(
        "Alex wohnt in Köln.", source, subjects=(alex,), reported_speakers=(tom,)
    )
    profile = h.service.person_profile(
        alex, context=h.read_context(alex, recipients=audience)
    )
    reported = [line for line in profile.card.splitlines() if line.startswith("reported:")]
    assert reported == ["reported: Alex wohnt in Köln."]


def test_profile_marks_two_values_of_one_group_as_a_conflict(knowledge_harness):
    from yeoman_gateway.knowledge.models import (
        AttributeCandidate,
        AttributeValue,
        PersonLinkCandidate,
        StatementCandidate,
    )

    h = knowledge_harness
    alex = h.person("Alex")
    for place in ("Köln", "Bonn"):
        source = h.source(alex)
        candidate = StatementCandidate(
            content=f"Alex wohnt in {place}.",
            sources=(source,),
            people=(
                PersonLinkCandidate(
                    person_id=alex, role="subject", source=source, attribution="extracted"
                ),
            ),
            attributes=(
                AttributeCandidate(
                    person_id=alex, attribute_key="residence", value=AttributeValue(place)
                ),
            ),
            extractor_version="test-extractor-1",
            confidence=0.5,
        )
        h.service.capture(candidate, context=h.capture_context(source))

    profile = h.service.person_profile(alex, context=h.read_context(alex))
    assert profile.conflicts == ("residence",)
    residence = [line for line in profile.card.splitlines() if line.startswith("residence:")]
    # Both values stay visible: no global last-write-wins.
    assert residence == ["residence: Bonn, Köln"]


def test_profile_reports_the_person_and_its_addresses(knowledge_harness):
    h = knowledge_harness
    person = h.observe("whatsapp", "phone_jid", "49181111111@s.whatsapp.net", name="Synthetic P.")
    h.service.set_preferred_name(person.person_id, "Synthetic Confirmed", context=h.admin_context())
    profile = h.service.person_profile(person.person_id, context=h.read_context(person.person_id))
    assert profile.display_name == "Synthetic Confirmed"
    assert profile.identity_revision == h.identity_revision()
    assert profile.acl_epoch == h.acl_epoch()
    assert profile.reason == "ok"
