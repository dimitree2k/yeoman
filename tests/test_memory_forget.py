"""Tests for MemoryService.forget() and forget_confirm()."""
from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import patch

from yeoman.memory.service import MemoryService


def _minimal_memory_config():
    """Return a minimal MemoryConfig for testing."""
    from yeoman.config.schema import Config
    config = Config()
    config.memory.enabled = True
    config.memory.capture.enabled = False  # Don't start capture threads in test
    return config.memory


def _make_service(tmp_path: Path) -> MemoryService:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    config = _minimal_memory_config()
    config.db_path = str(tmp_path / "memory.db")
    with patch("yeoman.memory.service._load_owner_ids", return_value={}):
        svc = MemoryService(workspace=workspace, config=config)
    return svc


def _insert_entry(svc: MemoryService, content: str, chat_id: str = "test-chat") -> str:
    from yeoman.memory.models import MemoryEntry
    entry = MemoryEntry(
        id=str(uuid.uuid4()),
        workspace_id=svc.workspace_id,
        scope_type="chat",
        scope_key=svc.chat_scope_key("whatsapp", chat_id),
        channel="whatsapp",
        chat_id=chat_id,
        sender_id="sender1",
        sector="episodic",
        kind="utterance",
        content=content,
        content_norm=content.lower(),
        content_hash=svc._hash_content(content),
        salience=0.7,
        confidence=0.8,
        source="test",
    )
    saved, _ = svc.store.upsert_node(entry)
    return saved.id


def test_forget_returns_matching_hits(tmp_path: Path) -> None:
    svc = _make_service(tmp_path)
    _insert_entry(svc, "antworte als Voice Message")
    _insert_entry(svc, "something unrelated about cooking")

    hits = svc.forget(query="Voice Message")
    assert len(hits) >= 1
    assert any("voice" in h.entry.content.lower() for h in hits)


def test_forget_confirm_soft_deletes(tmp_path: Path) -> None:
    svc = _make_service(tmp_path)
    entry_id = _insert_entry(svc, "delete me please")

    count = svc.forget_confirm([entry_id])
    assert count == 1

    # Entry should no longer appear in search
    hits = svc.forget(query="delete me please")
    assert not any(h.entry.id == entry_id for h in hits)


def test_forget_confirm_empty_list(tmp_path: Path) -> None:
    svc = _make_service(tmp_path)
    assert svc.forget_confirm([]) == 0
