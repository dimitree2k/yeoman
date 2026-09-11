"""Regressions for the findings of the 2026-09-11 final review (F06, F07, F11)."""

from __future__ import annotations

from pathlib import Path

from yeoman_gateway.memory.read_gate import build_read_context, registry_members
from yeoman_gateway.processing.reconcile import _effect_meta
from yeoman_gateway.processing.store import ProcessingStore

CHAT_A = "chat-a"
CHAT_B = "chat-b"
T0 = 1_700_000_000_000


def _store(tmp_path: Path) -> ProcessingStore:
    return ProcessingStore(tmp_path / "processing.db")


def _queued(store: ProcessingStore, effect_id: str, chat_id: str, *, now: int = T0) -> None:
    store.enqueue_effect(
        effect_id=effect_id,
        operation_key=f"k:{effect_id}",
        payload={"text": "hi"},
        target={"channel": "whatsapp", "chat_id": chat_id},
        turn_id="",
        turn_revision=1,
        now_ms=now,
    )


def test_f06_waiting_count_is_per_chat_and_ignores_blocked(tmp_path: Path) -> None:
    """A backlog in one chat must not spend another chat's waiting capacity."""
    store = _store(tmp_path)
    for index in range(20):
        _queued(store, f"fx-a{index}", CHAT_A)
        store.transition(
            f"fx-a{index}",
            expected="queued",
            target="blocked",
            now_ms=T0 + index,
            evidence={"kind": "policy", "detail": "budget_exhausted"},
        )
    _queued(store, "fx-b0", CHAT_B)

    assert store.count_waiting_effects(channel="whatsapp", chat_id=CHAT_A) == 0
    assert store.count_waiting_effects(channel="whatsapp", chat_id=CHAT_B) == 1

    # A genuinely waiting effect of the same chat is counted.
    _queued(store, "fx-a-live", CHAT_A)
    assert store.count_waiting_effects(channel="whatsapp", chat_id=CHAT_A) == 1
    assert store.count_waiting_effects(channel="whatsapp", chat_id=CHAT_B) == 1
    store.close()


def test_f07_effect_meta_is_found_behind_five_hundred_terminal_rows(tmp_path: Path) -> None:
    """The reconciler looked up effects through a 500-row, oldest-first scan."""
    store = _store(tmp_path)
    for index in range(500):
        effect_id = f"old{index}"
        _queued(store, effect_id, CHAT_A, now=T0 + index)
        store.transition(
            effect_id, expected="queued", target="cancelled", now_ms=T0 + index
        )
    _queued(store, "newest", CHAT_A, now=T0 + 10_000)

    meta = _effect_meta(store, "newest")

    assert meta is not None, "a new effect past 500 terminal rows must still be found"
    assert meta.effect_id == "newest"
    assert meta.state == "queued"
    store.close()


def test_f11_registry_lookup_uses_the_real_api(tmp_path: Path) -> None:
    """ChatRegistry exposes ``get_chat``; the gate looked for methods that never existed."""

    class _RealShapeRegistry:
        """Only the production method, so the contract is what is tested."""

        def get_chat(self, channel: str, chat_id: str):
            if chat_id != "gruppe@g.us":
                return None
            return {
                "chat_id": chat_id,
                "metadata": {"participants": [{"id": "alice"}, {"id": "bob"}]},
            }

    registry = _RealShapeRegistry()
    members = registry_members(registry, channel="whatsapp", chat_id="gruppe@g.us")

    assert members == {"alice", "bob"}

    context = build_read_context(
        principal_id="alice",
        channel="whatsapp",
        chat_id="gruppe@g.us",
        chat_registry=registry,
        now_ms=T0,
    )
    assert context.membership_known is True
    assert context.current_members == frozenset({"alice", "bob"})

    # An unknown chat stays unknown rather than optimistically open.
    assert registry_members(registry, channel="whatsapp", chat_id="other@g.us") == set()


def test_f11_accepts_string_participants_too() -> None:
    class _Strings:
        def get_chat(self, channel: str, chat_id: str):
            return {"metadata": {"participants": ["alice", "bob"]}}

    assert registry_members(_Strings(), channel="whatsapp", chat_id="x") == {"alice", "bob"}


def test_f01_an_unopenable_store_stops_startup_instead_of_falling_back(tmp_path: Path) -> None:
    """Review F01: returning None uninstalled every guard and let legacy publish.

    The channel path is what matters, so this asserts the startup contract that keeps the
    channel path intact: with the mode enabled and no usable store, building fails loudly.
    """
    import pytest
    from yeoman_gateway.app.bootstrap import (
        ProcessingStoreUnavailableError,
        build_effect_router,
        build_processing_gate,
        build_processing_store,
    )
    from yeoman_shared.config.schema import Config

    # A file where the database's parent directory should be: opening must fail.
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("x")
    config = Config.model_validate(
        {
            "processing": {
                "enabled": True,
                "chats": ["whatsapp:pilot@g.us"],
                "db_path": str(blocked / "processing.db"),
            },
            "security": {"enabled": False},
        }
    )

    with pytest.raises(ProcessingStoreUnavailableError):
        build_processing_store(config)

    # And the disabled mode still stays inert rather than raising.
    config.processing.enabled = False
    assert build_processing_store(config) is None
    assert build_processing_gate(config, None, None, None) is None
    assert build_effect_router(config, None, None, None) is None
