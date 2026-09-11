"""Regressions for the findings of the 2026-09-11 final review (F06, F07, F11)."""

from __future__ import annotations

from pathlib import Path

import pytest
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


def _store_with_message(tmp_path: Path, *, message_id: str = "m1", principal: str = "author-1"):
    """A store with one journaled message event that carries a turn."""
    store = _store(tmp_path)
    thread_id = store.open_thread(
        channel="whatsapp",
        chat_id=CHAT_A,
        root_principal=principal,
        kind="dm",
        trigger_event_id=message_id,
        now_ms=T0,
    )
    turn_id = store.open_turn(
        thread_id=thread_id, principal=principal, trigger_event_id=message_id, now_ms=T0
    )
    store.append_event(
        event_key=f"whatsapp:{CHAT_A}:message:{message_id}",
        event_id=message_id,
        trace_id=f"tr-{message_id}",
        payload={"text": "Fasse den Mietvertrag zusammen."},
        now_ms=T0,
    )
    # The gate writes exactly these columns when it assigns an admitted message.
    store._conn.execute(
        "UPDATE events SET source_message_id = ?, principal = ?, channel = ?, chat_id = ?,"
        " kind = 'message', thread_id = ?, turn_id = ? WHERE event_id = ?",
        (message_id, principal, "whatsapp", CHAT_A, thread_id, turn_id, message_id),
    )
    store._conn.commit()
    return store, thread_id, turn_id


def test_f05_delete_signal_invalidates_the_turn_and_its_effects(tmp_path: Path) -> None:
    """Review F05: the signal path only journaled. It must reach turn and effects."""
    from yeoman_gateway.processing.invalidation import SignalInvalidator
    from yeoman_gateway.processing.signals import SignalJournalSink

    store, _thread_id, turn_id = _store_with_message(tmp_path)
    _queued(store, "fx-pending", CHAT_A)
    # The queued effect belongs to the turn whose source is about to be deleted.
    store._conn.execute(
        "UPDATE effects SET turn_id = ?, turn_revision = 1 WHERE effect_id = ?",
        (turn_id, "fx-pending"),
    )
    store._conn.commit()

    sink = SignalJournalSink(
        store,
        invalidator=SignalInvalidator(store=store, clock=lambda: T0 + 100),
    )
    sink(
        "delete",
        {"chatJid": CHAT_A, "messageId": "m1", "senderId": "author-1", "isGroup": False},
    )

    turn = store.get_turn(turn_id)
    assert turn is not None and turn.revision == 2, "the turn revision was not raised"
    assert store.effect_state("fx-pending") == "cancelled"
    # The deletion itself is still journaled as evidence.
    kinds = [row[0] for row in store._conn.execute("SELECT kind FROM events").fetchall()]
    assert "delete" in kinds
    store.close()


def test_f05_a_foreign_delete_is_refused_and_changes_nothing(tmp_path: Path) -> None:
    """Not every signal carries authority: someone else's delete invalidates nothing."""
    from yeoman_gateway.processing.invalidation import SignalInvalidator

    store, _thread_id, turn_id = _store_with_message(tmp_path)
    invalidator = SignalInvalidator(store=store, clock=lambda: T0 + 100)

    result = invalidator(
        "delete",
        {"chatJid": CHAT_A, "messageId": "m1", "senderId": "someone-else", "isGroup": False},
    )

    assert result.applied is False
    assert result.refused == "sender_is_not_author"
    turn = store.get_turn(turn_id)
    assert turn is not None and turn.revision == 1
    store.close()


def test_f05_a_reaction_never_invalidates(tmp_path: Path) -> None:
    from yeoman_gateway.processing.invalidation import SignalInvalidator

    store, _thread_id, turn_id = _store_with_message(tmp_path)
    invalidator = SignalInvalidator(store=store, clock=lambda: T0 + 100)

    result = invalidator(
        "reaction",
        {"chatJid": CHAT_A, "messageId": "m1", "senderId": "author-1", "emoji": "👍"},
    )

    assert result.applied is False
    assert result.refused == "not_an_invalidating_signal"
    turn = store.get_turn(turn_id)
    assert turn is not None and turn.revision == 1
    store.close()


def test_f05_edit_supersedes_facts_instead_of_revoking_them(tmp_path: Path) -> None:
    """An edit replaces the statement; a delete removes it."""
    from yeoman_gateway.memory.shared_facts import FactSource, SharedFact
    from yeoman_gateway.processing.invalidation import SignalInvalidator

    store, _thread_id, _turn_id = _store_with_message(tmp_path)

    class _Memory:
        def __init__(self) -> None:
            self.calls: list[tuple[list[str], str]] = []

        def invalidate_sources(self, ids, *, now_ms, kind=None):
            self.calls.append((list(ids), str(kind)))

            class _Report:
                revoked = ()
                superseded = ("fact-1",)
                jobs_cancelled = 2

            return _Report()

    memory = _Memory()
    invalidator = SignalInvalidator(store=store, memory=memory, clock=lambda: T0 + 100)

    result = invalidator(
        "edit",
        {"chatJid": CHAT_A, "messageId": "m1", "senderId": "author-1", "text": "neu"},
    )

    assert memory.calls == [(["m1"], "edit")]
    assert result.facts_superseded == ("fact-1",)
    assert result.jobs_cancelled == 2
    # A shared-fact path exists and is reachable from here (sanity, not behaviour).
    assert FactSource and SharedFact
    store.close()


@pytest.mark.asyncio
async def test_f04_a_restart_puts_the_follow_up_into_the_request(tmp_path: Path) -> None:
    """Review F04: pending inputs were consumed but never reached the provider.

    The old restart loop called the inner responder with the *original* event, so the
    provider saw the first order twice while the follow-up disappeared.
    """
    import asyncio
    from datetime import UTC, datetime

    from yeoman_gateway.core.models import InboundEvent
    from yeoman_gateway.processing.actor import ThreadActorRegistry
    from yeoman_gateway.processing.responder import ThreadActorResponder

    store = _store(tmp_path)
    thread_id = store.open_thread(
        channel="whatsapp", chat_id=CHAT_A, root_principal="orderer",
        kind="dm", trigger_event_id="m1", now_ms=T0,
    )
    turn_id = store.open_turn(
        thread_id=thread_id, principal="orderer", trigger_event_id="m1", now_ms=T0
    )
    store.append_event(
        event_key="k1", event_id="m1", trace_id="tr-m1",
        payload={"text": "Book Tuesday"}, now_ms=T0,
    )
    store.append_event(
        event_key="k2", event_id="m2", trace_id="tr-m2",
        payload={"text": "At 15:00 please"}, now_ms=T0 + 1,
    )
    store.add_turn_source(
        turn_id=turn_id, event_id="m1", source_message_id="m1", role="trigger",
        revision_at_join=1, now_ms=T0,
    )
    # The wrapper resolves the thread through the journaled assignment.
    store.attach_event_assignment(
        event_id="m1", thread_id=thread_id, turn_id=turn_id, now_ms=T0
    )

    class _Config:
        threads = type(
            "T", (), {"max_additional_generations": 2, "postbox_max_waiting": 8}
        )()

    actors = ThreadActorRegistry(store=store, config=_Config(), clock=lambda: T0)
    actor = actors.actor_for(thread_id)

    seen: list[str] = []

    class _Barrier:
        """First call parks, a follow-up arrives, then the restart answers."""

        async def generate_reply(self, event, decision, *, session_key=None) -> str | None:
            seen.append(str(getattr(event, "content", "")))
            if len(seen) == 1:
                actor.accept(
                    event_id="m2",
                    principal="orderer",
                    kind="message",
                    explicit_correction=False,
                    authorized=True,
                )
            return "ok"

    event = InboundEvent(
        channel="whatsapp",
        chat_id=CHAT_A,
        sender_id="orderer",
        content="Book Tuesday",
        message_id="m1",
        is_group=False,
        mentioned_bot=True,
        timestamp=datetime(2023, 11, 14, tzinfo=UTC),
    )
    wrapper = ThreadActorResponder(
        inner=_Barrier(), actors=actors, store=store, clock=lambda: T0
    )

    await asyncio.wait_for(wrapper.generate_reply(event, None), timeout=5)

    assert seen, "the provider was never called"
    assert seen[-1].splitlines() == ["Book Tuesday", "At 15:00 please"], (
        f"the follow-up never reached the provider: {seen}"
    )
    store.close()
