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
from yeoman_gateway.knowledge.models import (
    PersonLinkCandidate,
    RecallQuery,
    SourceRef,
    StatementCandidate,
    TrustedReadContext,
)

from tests.gateway.knowledge.conftest import ProjectedKnowledgeHarness

WORKSPACE_ID = "test-workspace"
TOM = "whatsapp:4910000000002"
CHAT = "120363000000001@g.us"
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


class HybridHarness(ProjectedKnowledgeHarness):
    def __init__(self, tmp_path: Path) -> None:
        super().__init__(tmp_path)
        self.embedder = SpyEmbedder()

    def source(self, *, author: str = TOM, audience: set[str] | None = None) -> SourceRef:
        person = self.person_for(author, author)
        return super().source(person, chat=CHAT, audience=audience)

    def capture(self, text: str, *, author: str = TOM) -> tuple[str, SourceRef]:
        source = self.source(author=author)
        person = self.person_for(author, author)
        result = self.service.capture(
            StatementCandidate(
                content=text, sources=(source,),
                people=(PersonLinkCandidate(person_id=person, role="speaker", source=source,
                                            attribution="transport"),),
                extractor_version="hybrid-extractor-1", confidence=0.6,
            ), context=self.capture_context(source),
        )
        assert result.statement_ids, result.rejected
        return result.statement_ids[0], source

    def publish_vector(self, statement_id: str, *, dims: int | None = None) -> None:
        """Seed an already-published vector row for the retrieval integration test.

        WhatsApp embedding publication is retired at this call site. This fixture seeds
        the additive retrieval index directly while retaining its real schema and source
        identity, so the guarded vector search and final Knowledge gate remain under test.
        """
        from yeoman_gateway.knowledge._memory.store import (
            _EMBEDDING_INDEX_SCHEMA,
            MemoryStore,
        )

        store = self._raw_service._store
        for statement in _EMBEDDING_INDEX_SCHEMA:
            store.execute(statement)
        store.commit_if_idle()
        dimension = self.embedder.dims if dims is None else dims
        text = store.scalar("SELECT content FROM memory2_nodes WHERE id = ?", (statement_id,))
        source = store.query_one(
            "SELECT event_id, revision FROM knowledge_statement_sources WHERE statement_id = ?",
            (statement_id,),
        )
        content_hash = hashlib.sha256(str(text).encode("utf-8")).hexdigest()[:32]
        store.execute(
            """
            INSERT INTO memory2_embedding_index (
                document_id, content_hash, source_revision_hash, model_id, dimension,
                preprocessing_version, workspace_id, scope_key, channel, chat_id,
                source_event_id, source_revision, node_id, section_index, section_start,
                section_end, page_number, conversation_ids_json, vector, status,
                created_ms, retired_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, '[]', ?,
                      'active', ?, NULL)
            """,
            (
                f"emb:{statement_id}:s0",
                content_hash,
                "rev-hash-1",
                self.embedder.model,
                dimension,
                "memory-text-v1",
                "test-workspace",
                SCOPE,
                "whatsapp",
                CHAT,
                str(source["event_id"]),
                int(source["revision"]),
                statement_id,
                0,
                0,
                len(str(text)),
                MemoryStore._serialize_vector([0.5] * dimension),
                self.clock.now_ms(),
            ),
        )
        store.commit_if_idle()

    def vector_candidates(self, query, *, context, embedder):
        with self.history_scope():
            return self._raw_service._retrieval._vector_candidates(query, context=context, embedder=embedder)

    def read_context(self, reader: str = TOM, *, chat: str = CHAT) -> TrustedReadContext:
        return super().read_context(reader, chat=chat, recipients={reader})


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

    assert h.vector_candidates(  # noqa: SLF001 - vector path check
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
    assert h.vector_candidates(  # noqa: SLF001
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

    matched = h.vector_candidates(  # noqa: SLF001 - vector path check
        query, context=h.read_context(), embedder=h.embedder
    )
    assert matched == (statement_id,)

    # A row of another width must not be scored against this query vector at all.
    other, _source2 = h.capture("second statement with its own vector")
    h.publish_vector(other, dims=16)
    still = h.vector_candidates(  # noqa: SLF001
        query, context=h.read_context(), embedder=h.embedder
    )
    assert still == (statement_id,)
    assert other not in still
