"""Phase 2 / Task 2: hybrid exact/FTS/vector retrieval over the Phase-1 read gate.

Vector candidates are only ever an *addition* to the lexical result.  The lexical path is
produced first and does not depend on a provider, so an embedding outage costs recall
quality and never a result.  Everything merged is gated again immediately before it is
rendered, which is what keeps a revoked source out of the output.

Offline and synthetic: temporary database, recording embedder, no network.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

import pytest
from yeoman_gateway.knowledge.api import KnowledgeService, open_knowledge_store
from yeoman_gateway.knowledge.authority import (
    EvidenceAudience,
    FakeClock,
    FakePolicyAuthority,
    FakeSourceAuthority,
)
from yeoman_gateway.knowledge.models import (
    PersonLinkCandidate,
    RecallQuery,
    SourceRef,
    StatementCandidate,
    TrustedCaptureContext,
    TrustedReadContext,
)

WORKSPACE_ID = "hybrid-workspace"
OWNER = "whatsapp:4910000000001"
TOM = "whatsapp:4910000000002"
ALEX = "whatsapp:4910000000003"
CHAT = "group-a"
SCOPE = f"channel:whatsapp:chat:{CHAT}"


@dataclass
class SpyEmbedder:
    model: str = "openai/text-embedding-3-small"
    dims: int = 8
    fail: bool = False
    calls: list[str] = field(default_factory=list)

    def embed(self, text: str) -> list[float] | None:
        self.calls.append(text)
        if self.fail:
            return None
        return [0.5] * self.dims


@dataclass
class HybridHarness:
    tmp_path: Path

    def __post_init__(self) -> None:
        self.clock = FakeClock()
        self.authority = FakeSourceAuthority()
        self.policy = FakePolicyAuthority(admins={OWNER}, capture_actors={OWNER})
        self.service: KnowledgeService = open_knowledge_store(
            self.tmp_path / "knowledge.db",
            workspace_id=WORKSPACE_ID,
            source_authority=self.authority,
            policy_authority=self.policy,
            clock=self.clock,
        )
        self.embedder = SpyEmbedder()
        self._counter = 0

    def close(self) -> None:
        self.service.close()

    def source(self, *, author: str = TOM, audience: set[str] | None = None) -> SourceRef:
        self._counter += 1
        source = SourceRef(
            event_id=f"event-{self._counter}",
            revision=1,
            channel="whatsapp",
            chat_id=CHAT,
            author_principal=author,
            occurred_at_ms=self.clock.now_ms(),
        )
        self.authority.issue_source(
            source,
            EvidenceAudience.known(
                set(audience) if audience is not None else {author},
                snapshot_id=f"snap-{source.event_id}",
            ),
        )
        return source

    def capture_context(self, *sources: SourceRef) -> TrustedCaptureContext:
        request_id = f"cap-{'/'.join(item.event_id for item in sources)}"
        self.policy.issue_capture(request_id)
        return TrustedCaptureContext(
            request_id=request_id,
            policy_revision=self.policy.revision,
            capture_basis="user_message",
            authorized_sources=tuple(sources),
            actor_principal=OWNER,
            authorized=True,
        )

    def capture(self, text: str, *, author: str = TOM) -> tuple[str, SourceRef]:
        source = self.source(author=author)
        result = self.service.capture(
            StatementCandidate(
                content=text,
                sources=(source,),
                people=(
                    PersonLinkCandidate(
                        person_id=self.person_for(author),
                        role="speaker",
                        source=source,
                        attribution="transport",
                    ),
                ),
                extractor_version="hybrid-extractor-1",
                confidence=0.6,
            ),
            context=self.capture_context(source),
        )
        assert result.statement_ids, result.rejected
        return result.statement_ids[0], source

    def person_for(self, principal: str) -> str:
        """Resolve a synthetic person through a proven observation."""
        from yeoman_gateway.knowledge.models import Identifier, TrustedIdentityObservation

        number = principal.split(":", 1)[-1]
        observation = TrustedIdentityObservation(
            identifiers=(
                Identifier(channel="whatsapp", kind="phone_jid", value=f"{number}@s.whatsapp.net"),
            ),
            evidence_ref=f"obs-{number}",
            observed_at_ms=self.clock.now_ms(),
        )
        self.authority.issue_observation(observation)
        resolved = self.service.resolve_person(observation)
        assert resolved.person_id, resolved.reason
        return resolved.person_id

    def publish_vector(self, statement_id: str, *, dims: int | None = None) -> None:
        """Publish one embedding section for a statement node, as the worker would."""
        from yeoman_gateway.knowledge._memory.store import MemoryStore

        memory = MemoryStore(owner=self.service._store)  # noqa: SLF001 - same file, one owner
        children = dims if dims is not None else self.embedder.dims
        text = self.service._store.scalar(  # noqa: SLF001 - test fixture only
            "SELECT content FROM memory2_nodes WHERE id = ?", (statement_id,)
        )
        memory.publish_embedding_section(
            document_id=f"emb:{statement_id}:s0",
            content_hash=hashlib.sha256(str(text).encode("utf-8")).hexdigest()[:32],
            source_revision_hash="rev-hash-1",
            model_id=self.embedder.model,
            dimension=children,
            preprocessing_version="memory-text-v1",
            workspace_id=WORKSPACE_ID,
            scope_key=SCOPE,
            channel="whatsapp",
            chat_id=CHAT,
            source_event_id="event-1",
            source_revision=1,
            node_id=statement_id,
            section_index=0,
            section_start=0,
            section_end=len(str(text)),
            vector=[0.5] * children,
            now_ms=self.clock.now_ms(),
        )

    def read_context(self, reader: str = TOM, *, chat: str = CHAT) -> TrustedReadContext:
        members = {reader}
        self.policy.set_members(
            TrustedReadContext(
                principal_id=reader,
                channel="whatsapp",
                chat_id=chat,
                recipient_principals=frozenset(members),
                membership_revision=f"mem-{chat}",
                policy_revision=self.policy.revision,
                purpose="reply",
                now_ms=self.clock.now_ms(),
            ),
            members,
            revision=f"mem-{chat}",
        )
        return TrustedReadContext(
            principal_id=reader,
            channel="whatsapp",
            chat_id=chat,
            recipient_principals=frozenset(members),
            membership_revision=f"mem-{chat}",
            policy_revision=self.policy.revision,
            purpose="reply",
            now_ms=self.clock.now_ms(),
        )


@pytest.fixture
def h(tmp_path: Path) -> Iterator[HybridHarness]:
    harness = HybridHarness(tmp_path)
    try:
        yield harness
    finally:
        harness.close()


def test_exact_fts_and_vector_candidates_merge_by_statement_identity(h: HybridHarness) -> None:
    statement_id, _source = h.capture("the harbour ferry departs at dawn")
    h.publish_vector(statement_id)
    before = len(h.embedder.calls)

    result = h.service.recall_hybrid(
        RecallQuery(text="harbour ferry", limit=10),
        context=h.read_context(),
        embedder=h.embedder,
    )

    # Found by both paths, rendered once.
    assert result.statement_ids == (statement_id,)
    assert result.text.count("harbour ferry") == 1
    assert len(h.embedder.calls) == before + 1


def test_provider_failure_never_hides_fts_results(h: HybridHarness) -> None:
    statement_id, _source = h.capture("a statement only the lexical path can find")
    h.embedder.fail = True

    result = h.service.recall_hybrid(
        RecallQuery(text="lexical path", limit=10),
        context=h.read_context(),
        embedder=h.embedder,
    )

    assert result.statement_ids == (statement_id,)
    assert "lexical path" in result.text


def test_missing_provider_still_returns_lexical_results(h: HybridHarness) -> None:
    statement_id, _source = h.capture("retrievable without any provider at all")
    result = h.service.recall_hybrid(
        RecallQuery(text="without any provider", limit=10),
        context=h.read_context(),
        embedder=None,
    )
    assert result.statement_ids == (statement_id,)


def test_vector_candidates_never_widen_the_read_gate(h: HybridHarness) -> None:
    """A foreign vector row must not become a hit for a reader outside its chat.

    The query shares no token with the statement, so only the vector path could ever
    produce the id; both the vector candidate query and the public recall gate are
    therefore genuinely load-bearing here.
    """
    statement_id, _source = h.capture("alpha bravo charlie")
    h.publish_vector(statement_id)
    query = RecallQuery(text="delta echo foxtrot", limit=10)

    assert h.service._retrieval._vector_candidates(  # noqa: SLF001 - vector path check
        query,
        context=h.read_context(),
        embedder=h.embedder,
    ) == (statement_id,)

    assert h.service.recall_hybrid(
        query,
        context=h.read_context(),
        embedder=h.embedder,
    ).statement_ids == (statement_id,)

    other = h.read_context(chat="group-b")
    assert h.service._retrieval._vector_candidates(  # noqa: SLF001
        query, context=other, embedder=h.embedder
    ) == ()
    result = h.service.recall_hybrid(
        query, context=other, embedder=h.embedder
    )
    assert result.statement_ids == ()
    assert result.text == ""


def test_merged_candidates_are_gated_again_before_rendering(h: HybridHarness) -> None:
    statement_id, source = h.capture("will be withdrawn before it is rendered")
    h.publish_vector(statement_id)

    first = h.service.recall_hybrid(
        RecallQuery(text="withdrawn", limit=10),
        context=h.read_context(),
        embedder=h.embedder,
    )
    assert first.statement_ids == (statement_id,)

    h.service.invalidate_source(source, context=h.capture_context(source))

    after = h.service.recall_hybrid(
        RecallQuery(text="withdrawn", limit=10),
        context=h.read_context(),
        embedder=h.embedder,
    )
    assert after.statement_ids == ()
    assert after.text == ""


def test_vector_candidates_are_scored_and_dimension_bound(h: HybridHarness) -> None:
    """The vector lookup itself is load-bearing: it is inspected directly.

    The lexical candidate path is a gate-filtered fetch that ranks afterwards, so a hit
    appearing in a full recall proves nothing about the vector path.  These assertions
    therefore read the vector candidates on their own.
    """
    statement_id, _source = h.capture("alpha bravo charlie")
    h.publish_vector(statement_id)
    query = RecallQuery(text="delta echo foxtrot", limit=10)

    matched = h.service._retrieval._vector_candidates(  # noqa: SLF001 - vector path check
        query, context=h.read_context(), embedder=h.embedder
    )
    assert matched == (statement_id,)

    # A row of another width must not be scored against this query vector at all.
    other, _source2 = h.capture("second statement with its own vector")
    h.publish_vector(other, dims=16)
    still = h.service._retrieval._vector_candidates(  # noqa: SLF001
        query, context=h.read_context(), embedder=h.embedder
    )
    assert still == (statement_id,)
    assert other not in still
