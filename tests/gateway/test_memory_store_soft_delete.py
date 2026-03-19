"""Tests for MemoryStore.soft_delete()."""
from __future__ import annotations

import uuid
from pathlib import Path

from yeoman_gateway.memory.models import MemoryEntry
from yeoman_gateway.memory.store import MemoryStore


def _make_entry(workspace_id: str = "ws1", scope_key: str = "test:scope") -> MemoryEntry:
    content = f"test content {uuid.uuid4().hex[:8]}"
    return MemoryEntry(
        id=str(uuid.uuid4()),
        workspace_id=workspace_id,
        scope_type="chat",
        scope_key=scope_key,
        sector="episodic",
        kind="utterance",
        content=content,
        content_norm=content.lower(),
        content_hash=f"hash_{uuid.uuid4().hex[:8]}",
        salience=0.7,
        confidence=0.8,
        source="test",
    )


def test_soft_delete_marks_entries_and_returns_count(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "mem.db")
    e1, _ = store.upsert_node(_make_entry())
    e2, _ = store.upsert_node(_make_entry())
    e3, _ = store.upsert_node(_make_entry())

    deleted = store.soft_delete([e1.id, e2.id])
    assert deleted == 2

    # Deleted entries should not appear in search
    hits = store.search_lexical(
        workspace_id="ws1", query=e1.content, scope_keys=["test:scope"], limit=10
    )
    hit_ids = {h.entry.id for h in hits}
    assert e1.id not in hit_ids
    assert e2.id not in hit_ids

    # e3 should still be findable
    hits3 = store.search_lexical(
        workspace_id="ws1", query=e3.content, scope_keys=["test:scope"], limit=10
    )
    assert any(h.entry.id == e3.id for h in hits3)


def test_soft_delete_empty_list_returns_zero(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "mem.db")
    assert store.soft_delete([]) == 0


def test_soft_delete_nonexistent_ids_returns_zero(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "mem.db")
    assert store.soft_delete(["nonexistent-id-1", "nonexistent-id-2"]) == 0


def test_soft_delete_idempotent(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "mem.db")
    e1, _ = store.upsert_node(_make_entry())
    assert store.soft_delete([e1.id]) == 1
    assert store.soft_delete([e1.id]) == 0  # already deleted


def test_distinct_scope_keys(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "mem.db")
    e1 = _make_entry(scope_key="scope:a")
    e2 = _make_entry(scope_key="scope:b")
    e3 = _make_entry(scope_key="scope:a")  # duplicate scope
    store.upsert_node(e1)
    store.upsert_node(e2)
    store.upsert_node(e3)

    keys = store.distinct_scope_keys("ws1")
    assert set(keys) == {"scope:a", "scope:b"}
