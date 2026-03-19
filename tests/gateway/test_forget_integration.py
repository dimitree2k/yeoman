"""Integration test: full /forget preview → confirm round trip."""
from __future__ import annotations

import re
import uuid
from pathlib import Path
from unittest.mock import patch

from yeoman_gateway.adapters.policy_engine import EnginePolicyAdapter
from yeoman_gateway.core.admin_commands import AdminCommandContext
from yeoman_gateway.memory.models import MemoryEntry
from yeoman_gateway.memory.service import MemoryService
from yeoman_shared.config.schema import Config


def _ctx(raw_text: str) -> AdminCommandContext:
    return AdminCommandContext(
        channel="whatsapp",
        chat_id="owner@lid",
        sender_id="491757070305",
        participant=None,
        is_group=False,
        raw_text=raw_text,
    )


def _make_memory_service(tmp_path: Path) -> MemoryService:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    config = Config()
    config.memory.enabled = True
    config.memory.capture.enabled = False
    config.memory.db_path = str(tmp_path / "memory.db")
    with patch("yeoman_gateway.memory.service._load_owner_ids", return_value={}):
        return MemoryService(workspace=workspace, config=config.memory)


def _insert(svc: MemoryService, content: str, chat_id: str = "group@g.us") -> str:
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


def _make_adapter(tmp_path: Path, svc: MemoryService) -> EnginePolicyAdapter:
    adapter = EnginePolicyAdapter(
        engine=None,
        known_tools=set(),
        session_manager=None,
        workspace=tmp_path,
    )
    adapter.set_memory_service(svc)
    # Stub owner check to always return True
    adapter._owner_policy_for_context = lambda ctx: True
    return adapter


def test_full_forget_round_trip(tmp_path: Path) -> None:
    svc = _make_memory_service(tmp_path)
    id1 = _insert(svc, "antworte immer als Voice Message")
    id2 = _insert(svc, "Voice bitte")
    _insert(svc, "something about cooking recipes")

    adapter = _make_adapter(tmp_path, svc)

    # Step 1: Preview
    result = adapter.forget_handle(_ctx("/forget voice"), ["voice"])
    assert result.status == "handled"
    assert "Found" in (result.response or "")
    assert "voice" in (result.response or "").lower()
    assert "/forget confirm" in (result.response or "")

    # Extract hash from response
    match = re.search(r"/forget confirm (\w+)", result.response or "")
    assert match, f"No confirm hash in response: {result.response}"
    token = match.group(1)

    # Step 2: Confirm all
    result2 = adapter.forget_handle(_ctx(f"/forget confirm {token}"), ["confirm", token])
    assert result2.status == "handled"
    assert "Forgot" in (result2.response or "")

    # Verify entries are soft-deleted
    remaining = svc.forget(query="Voice Message")
    remaining_ids = {h.entry.id for h in remaining}
    assert id1 not in remaining_ids
    assert id2 not in remaining_ids


def test_selective_forget_by_index(tmp_path: Path) -> None:
    svc = _make_memory_service(tmp_path)
    _insert(svc, "Voice Message eins")
    _insert(svc, "Voice Message zwei")
    _insert(svc, "Voice Message drei")

    adapter = _make_adapter(tmp_path, svc)

    # Preview
    result = adapter.forget_handle(_ctx("/forget voice"), ["voice"])
    assert result.status == "handled"

    match = re.search(r"/forget confirm (\w+)", result.response or "")
    assert match
    token = match.group(1)

    # Confirm only index 2
    result2 = adapter.forget_handle(
        _ctx(f"/forget confirm {token} 2"), ["confirm", token, "2"]
    )
    assert result2.status == "handled"
    assert "Forgot 1" in (result2.response or "")
