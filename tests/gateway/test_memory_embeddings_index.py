"""Existing embedding index reads preserve model, dimension, scope and source gates."""
import pytest
from yeoman_gateway.knowledge._embedding_index import (
    EMBEDDING_PREPROCESSING_VERSION,
    EmbeddingIndex,
)

from tests.gateway.test_memory_hybrid_retrieval import SCOPE, HybridHarness


@pytest.fixture
def h(tmp_path):
    harness = HybridHarness(tmp_path)
    try:
        yield harness
    finally:
        harness.close()

@pytest.mark.parametrize("override", [
    {"model_id": "other-model"}, {"preprocessing_version": "other-version"},
    {"query_vector": [0.5] * 3}, {"scope_keys": ["other-chat"]},
])
def test_index_never_compares_incompatible_or_out_of_scope_vectors(h, override):
    sid, _ = h.capture("the harbour ferry departs at dawn")
    h.publish_vector(sid)
    index = EmbeddingIndex(owner=h._raw_service._store)
    kwargs = dict(workspace_id="test-workspace", query_vector=[0.5] * h.embedder.dims,
                  scope_keys=[SCOPE], model_id=h.embedder.model,
                  preprocessing_version=EMBEDDING_PREPROCESSING_VERSION)
    assert [row["node_id"] for row in index.search_embedding_index(**kwargs)] == [sid]
    assert index.search_embedding_index(**(kwargs | override)) == []

def test_source_is_rechecked_immediately_before_index_output(h):
    sid, source = h.capture("the harbour ferry departs at dawn")
    h.publish_vector(sid)
    authority = SimpleAuthority(source)
    index = EmbeddingIndex(owner=h._raw_service._store, source_authority=authority)
    kwargs = dict(workspace_id="test-workspace", query_vector=[0.5] * h.embedder.dims, scope_keys=[SCOPE])
    assert [row["node_id"] for row in index.search_embedding_index(**kwargs)] == [sid]
    authority.revoked = True
    assert index.search_embedding_index(**kwargs) == []
    authority.revoked = False
    authority.missing = True
    assert index.search_embedding_index(**kwargs) == []

class SimpleAuthority:
    def __init__(self, source):
        self.source = source
        self.revoked = False
        self.missing = False

    def verify_source_ref(self, event_id, revision):
        assert (event_id, revision) == (self.source.event_id, self.source.revision)
        return None if self.missing else self.source

    def source_revoked(self, source):
        assert source == self.source
        return self.revoked
