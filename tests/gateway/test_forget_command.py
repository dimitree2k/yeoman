"""Tests for the /forget admin command handler."""
from __future__ import annotations

import hashlib
import uuid
from unittest.mock import MagicMock

from yeoman_gateway.core.admin_commands import AdminCommandContext, AdminCommandResult, AdminCommandRouter
from yeoman_gateway.memory.models import MemoryEntry, MemoryHit


def _ctx(raw_text: str, *, owner: bool = True) -> AdminCommandContext:
    return AdminCommandContext(
        channel="whatsapp",
        chat_id="owner@lid" if owner else "intruder@lid",
        sender_id="owner@lid" if owner else "intruder@lid",
        participant=None,
        is_group=False,
        raw_text=raw_text,
    )


def _make_hit(content: str, chat_id: str = "group@g.us") -> MemoryHit:
    entry = MemoryEntry(
        id=str(uuid.uuid4()),
        workspace_id="ws1",
        scope_type="chat",
        scope_key=f"channel:whatsapp:chat:{chat_id}",
        channel="whatsapp",
        chat_id=chat_id,
        sender_id="sender1",
        sector="episodic",
        kind="utterance",
        content=content,
        content_norm=content.lower(),
        content_hash=hashlib.sha256(content.lower().encode()).hexdigest(),
        salience=0.7,
        confidence=0.8,
        source="test",
    )
    return MemoryHit(entry=entry, final_score=0.9)


def _compute_hash(ids: list[str]) -> str:
    payload = "\n".join(sorted(ids))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:4]


def _make_handler_and_router():
    from yeoman_gateway.adapters.policy_engine import EnginePolicyAdapter, ForgetCommandHandler

    adapter = MagicMock(spec=EnginePolicyAdapter)
    adapter.forget_is_applicable.return_value = True

    handler = ForgetCommandHandler(adapter)
    router = AdminCommandRouter([handler])
    return adapter, handler, router


def test_forget_no_query_shows_usage() -> None:
    adapter, _, router = _make_handler_and_router()
    adapter.forget_handle.return_value = AdminCommandResult(
        status="handled", response="Usage: /forget <query>"
    )

    router.route(_ctx("/forget"))
    adapter.forget_handle.assert_called_once()


def test_forget_preview_shows_results() -> None:
    adapter, _, router = _make_handler_and_router()
    hits = [_make_hit("voice message test")]
    token = _compute_hash([h.entry.id for h in hits])

    adapter.forget_handle.return_value = AdminCommandResult(
        status="handled",
        response=f"Found 1 memory:\n1. (DM) \"voice message test\"\n\n/forget confirm {token}",
    )

    router.route(_ctx("/forget voice message"))
    adapter.forget_handle.assert_called_once()


def test_forget_confirm_computes_correct_hash() -> None:
    """Verify the hash computation used by /forget confirm."""
    ids = ["id-aaa", "id-bbb", "id-ccc"]
    token = _compute_hash(ids)
    expected = hashlib.sha256("\n".join(sorted(ids)).encode("utf-8")).hexdigest()[:4]
    assert token == expected
