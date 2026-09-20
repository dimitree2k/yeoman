"""Phase 2 / Task 2: versioned asynchronous embeddings over the existing FTS path.

The index is rebuildable, never memory truth.  Text is committed and searchable by FTS
*before* any provider call; the provider runs only on bounded, source-linked sections;
a provider failure never hides a lexical result; and no vector is ever compared against
a vector of a different model, dimension or preprocessing version.

Offline and synthetic: temporary databases, a recording embedder, no network.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from yeoman_gateway.knowledge._memory.embeddings import (
    EMBEDDING_PREPROCESSING_VERSION,
    MAX_EMBEDDING_SECTION_CHARS,
    MemoryEmbeddingQueue,
    chunk_source_text,
)
from yeoman_gateway.knowledge._memory.store import MemoryStore
from yeoman_gateway.knowledge.authority import EvidenceAudience, FakeClock, FakeSourceAuthority
from yeoman_gateway.processing.models import CanonicalEvent

CHAT = "chat@g.us"
SCOPE = f"channel:whatsapp:chat:{CHAT}"
AUDIENCE = EvidenceAudience.known({"4915", "4916"}, snapshot_id="snap-1")


@dataclass
class SpyEmbedder:
    """Records every text that would cross the provider boundary."""

    model: str = "openai/text-embedding-3-small"
    dims: int = 8
    fail: bool = False
    calls: list[str] = field(default_factory=list)

    def embed(self, text: str) -> list[float] | None:
        self.calls.append(text)
        if self.fail:
            return None
        return [0.25] * self.dims


def _event(*, event_id: str = "event-1", text: str = "canonical message") -> CanonicalEvent:
    return CanonicalEvent(
        event_id=event_id,
        event_key=f"whatsapp:{CHAT}:{event_id}",
        trace_id=f"trace-{event_id}",
        kind="message",
        origin="whatsapp_bridge",
        principal="4915",
        channel="whatsapp",
        chat_id=CHAT,
        revision=1,
        occurred_ms=1_700_000_000_000,
        source_message_id=f"provider-{event_id}",
        payload={"text": text},
    )


@dataclass
class EmbeddingHarness:
    tmp_path: Path

    def __post_init__(self) -> None:
        self.clock = FakeClock()
        self.authority = FakeSourceAuthority()
        self.store = MemoryStore(
            self.tmp_path / "memory.db", source_authority=self.authority
        )
        self.embedder = SpyEmbedder()
        self.embedder.dims = 8
        self.queue = MemoryEmbeddingQueue(
            store=self.store,
            embedder=self.embedder,
            authority=self.authority,
            clock=self.clock.now_ms,
        )

    # ── sources ──────────────────────────────────────────────────────────────

    def issue_source(self, event_id: str = "event-1", *, revision: int = 1, revoked: bool = False):
        from yeoman_gateway.knowledge.models import SourceRef

        source = SourceRef(
            event_id=event_id,
            revision=revision,
            channel="whatsapp",
            chat_id=CHAT,
            author_principal="4915",
            occurred_at_ms=1_700_000_000_000,
        )
        self.authority.issue_source(source, AUDIENCE)
        if revoked:
            self.authority.revoke_source(source)
        return source

    # ── indexing ─────────────────────────────────────────────────────────────

    def index(self, *, event_id: str = "event-1", text: str = "canonical message", **kwargs):
        entries = self.store.index_canonical_event(
            _event(event_id=event_id, text=text), audience=AUDIENCE, **kwargs
        )
        return entries

    def enqueue(self, entry, **kwargs) -> str:
        return self.queue.enqueue_node(entry, now_ms=self.clock.now_ms(), **kwargs)

    def close(self) -> None:
        self.store.close()


@pytest.fixture
def h(tmp_path: Path):
    harness = EmbeddingHarness(tmp_path)
    try:
        yield harness
    finally:
        harness.close()


# ── lexical truth comes first ────────────────────────────────────────────────


def test_canonical_text_is_committed_and_fts_visible_before_any_provider_call(h) -> None:
    entries = h.index(text="the boat leaves on friday")
    assert entries
    h.issue_source()
    h.enqueue(entries[0])

    # Committed and searchable, but the provider has not been touched yet.
    assert h.embedder.calls == []
    assert [
        hit.entry.content
        for hit in h.store.search_lexical(
            workspace_id=entries[0].workspace_id, query="boat friday", scope_keys=[SCOPE]
        )
    ] == ["the boat leaves on friday"]
    assert h.queue.waiting == 1

    h.queue.run_due(now_ms=h.clock.now_ms())
    assert h.embedder.calls


def test_provider_failure_keeps_fts_results_and_retries_durably(tmp_path: Path) -> None:
    h = EmbeddingHarness(tmp_path)
    h.embedder.fail = True
    entries = h.index(text="a fact that must stay findable")
    h.issue_source()
    job_key = h.enqueue(entries[0])

    report = h.queue.run_due(now_ms=h.clock.now_ms())
    assert report.failed == 1

    # FTS is unaffected by the provider failure.
    assert [
        hit.entry.content
        for hit in h.store.search_lexical(
            workspace_id=entries[0].workspace_id, query="findable", scope_keys=[SCOPE]
        )
    ] == ["a fact that must stay findable"]
    assert h.store.search_embedding_index(
        workspace_id=entries[0].workspace_id, query_vector=[0.25] * 8, scope_keys=[SCOPE]
    ) == []

    job = h.store.get_embedding_job(job_key)
    assert job is not None
    assert job["state"] == "failed"
    assert int(job["attempts"]) == 1
    assert int(job["due_ms"]) > h.clock.now_ms()
    h.close()

    # The durable job survives a restart and still retries.
    reopened = MemoryStore(tmp_path / "memory.db")
    assert reopened.get_embedding_job(job_key)["state"] == "failed"
    reopened.close()


def test_retry_after_a_provider_recovery_publishes_the_index(h) -> None:
    h.embedder.fail = True
    entries = h.index(text="retry me")
    h.issue_source()
    job_key = h.enqueue(entries[0])
    h.queue.run_due(now_ms=h.clock.now_ms())

    h.embedder.fail = False
    h.clock.advance(3_600_000)
    report = h.queue.run_due(now_ms=h.clock.now_ms())

    assert report.published == 1
    assert h.store.get_embedding_job(job_key)["state"] == "done"
    assert len(
        h.store.search_embedding_index(
            workspace_id=entries[0].workspace_id, query_vector=[0.25] * 8, scope_keys=[SCOPE]
        )
    ) == 1


# ── version isolation ────────────────────────────────────────────────────────


def test_model_change_creates_a_new_identity_without_mixing_vectors(h) -> None:
    entries = h.index(text="versioned text")
    h.issue_source()
    first = h.enqueue(entries[0])
    h.queue.run_due(now_ms=h.clock.now_ms())
    assert h.store.get_embedding_job(first)["state"] == "done"

    h.embedder.model = "openai/text-embedding-3-large"
    h.embedder.dims = 16
    second = h.enqueue(entries[0])
    assert second != first

    h.queue.run_due(now_ms=h.clock.now_ms())
    rows = h.store.embedding_index_rows(source_event_id="event-1")
    active = [row for row in rows if row["status"] == "active"]
    retired = [row for row in rows if row["status"] == "retired"]
    assert len(active) == 1
    assert len(retired) == 1
    assert active[0]["model_id"] == "openai/text-embedding-3-large"
    assert int(active[0]["dimension"]) == 16
    assert retired[0]["model_id"] == "openai/text-embedding-3-small"
    assert int(retired[0]["retired_ms"]) > 0


def test_incompatible_dimensions_are_never_compared(h) -> None:
    entries = h.index(text="eight dimensional text")
    h.issue_source()
    h.enqueue(entries[0])
    h.queue.run_due(now_ms=h.clock.now_ms())

    assert len(
        h.store.search_embedding_index(
            workspace_id=entries[0].workspace_id, query_vector=[0.25] * 8, scope_keys=[SCOPE]
        )
    ) == 1
    assert (
        h.store.search_embedding_index(
            workspace_id=entries[0].workspace_id, query_vector=[0.25] * 4, scope_keys=[SCOPE]
        )
        == []
    )
    assert (
        h.store.search_embedding_index(
            workspace_id=entries[0].workspace_id,
            query_vector=[0.25] * 8,
            scope_keys=[SCOPE],
            model_id="some-other-model",
        )
        == []
    )


def test_preprocessing_version_change_creates_a_new_identity(h) -> None:
    entries = h.index(text="preprocessed text")
    h.issue_source()
    first = h.enqueue(entries[0])
    h.queue.run_due(now_ms=h.clock.now_ms())

    other = MemoryEmbeddingQueue(
        store=h.store,
        embedder=h.embedder,
        authority=h.authority,
        clock=h.clock.now_ms,
        preprocessing_version="memory-text-v2",
    )
    second = other.enqueue_node(entries[0], now_ms=h.clock.now_ms())
    assert second != first
    other.run_due(now_ms=h.clock.now_ms())
    active = [row for row in h.store.embedding_index_rows(source_event_id="event-1") if row["status"] == "active"]
    assert len(active) == 1
    assert active[0]["preprocessing_version"] == "memory-text-v2"


def test_audience_change_creates_a_new_source_revision_identity(h) -> None:
    """A changed audience must change the index identity on the production path too.

    Nothing here passes an explicit audience: the fingerprint is read from the node's own
    stored audience metadata, exactly as the queue reads it in production.
    """
    entries = h.index(text="audience scoped text")
    h.issue_source()
    first_job = h.enqueue(entries[0])
    h.queue.run_due(now_ms=h.clock.now_ms())
    first_hash = str(h.store.embedding_index_rows(source_event_id="event-1")[0]["source_revision_hash"])

    h.store.update_node_meta(
        entries[0].id,
        workspace_id=entries[0].workspace_id,
        meta_json=json.dumps(
            {
                "source_event_id": "event-1",
                "source_revision": 1,
                "audience_status": "known",
                "audience_members": ["4915", "4916", "4917"],
                "audience_snapshot_id": "snap-2",
            }
        ),
    )
    refreshed = h.store.get_node(entries[0].id, workspace_id=entries[0].workspace_id)
    assert refreshed is not None
    second_job = h.enqueue(refreshed)

    assert second_job != first_job
    h.queue.run_due(now_ms=h.clock.now_ms())
    hashes = {
        str(row["source_revision_hash"])
        for row in h.store.embedding_index_rows(source_event_id="event-1")
    }
    assert len(hashes) == 2
    assert first_hash in hashes


def test_preprocessing_version_constant_is_stable() -> None:
    assert EMBEDDING_PREPROCESSING_VERSION == "memory-text-v1"
    assert MAX_EMBEDDING_SECTION_CHARS == 2000
