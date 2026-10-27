"""Synthetic persisted lifecycle and maintenance safety checks."""

import asyncio
import sqlite3
from dataclasses import replace
from types import SimpleNamespace

import pytest
from yeoman_gateway.processing.actor import ThreadActorRegistry
from yeoman_gateway.processing.models import CanonicalEvent
from yeoman_gateway.processing.store import ProcessingStore
from yeoman_gateway.processing.threads import JoinInput, ThreadPolicy, ThreadRegistry
from yeoman_shared.config.schema import ProcessingThreadsConfig


def open_thread(store, name="root", kind="group", at=0):
    return store.open_thread(channel="test", chat_id="chat", root_principal="human",
                             kind=kind, trigger_event_id=name, now_ms=at)


def actors(store, **policy):
    registry = ThreadActorRegistry(store=store)
    registry._policy = replace(ThreadPolicy(), **policy)
    return registry


def test_old_schema_migration_is_idempotent(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE threads (thread_id TEXT PRIMARY KEY, channel TEXT NOT NULL, "
                     "chat_id TEXT NOT NULL, root_principal TEXT NOT NULL, kind TEXT NOT NULL, "
                     "state TEXT NOT NULL, opened_ms INTEGER NOT NULL, "
                     "last_activity_ms INTEGER NOT NULL, closed_ms INTEGER, close_reason TEXT, "
                     "reopen_count INTEGER NOT NULL DEFAULT 0, turn_seq INTEGER NOT NULL DEFAULT 0)")
        conn.execute("INSERT INTO threads VALUES ('old','test','chat','human','group',"
                     "'open',0,0,NULL,NULL,0,0)")
    for _ in range(2):
        with ProcessingStore(path) as store:
            assert store.get_thread("old").conversation_state == "active"
            assert store.get_thread("old").state == "open"
    with ProcessingStore(path) as store:
        assert store.mark_dormant(["old"], 1, "idle_and_quiet") == ("old",)
    with ProcessingStore(path) as store:
        assert store.get_thread("old").conversation_state == "dormant"
        assert store.get_thread("old").dormant_ms == 1


@pytest.mark.parametrize("minutes,messages,expected", [(59, 5, False), (60, 4, False), (60, 5, True)])
def test_tick_thresholds_and_no_turn_mutation(minutes, messages, expected):
    with ProcessingStore(":memory:") as store:
        tid = open_thread(store)
        turn = store.open_turn(thread_id=tid, principal="human", trigger_event_id="root", now_ms=0)
        for index in range(messages):
            key = str(index)
            event = CanonicalEvent(event_id=key, event_key=key, trace_id=key, channel="test",
                                   chat_id="chat", principal="other", origin="test",
                                   source_message_id=key, occurred_ms=1)
            store.append_event(event_key=key, event_id=key, trace_id=key, payload=event, now_ms=1)
        before = store.get_turn(turn)
        registry = actors(store, idle_ms=10**12)  # dormancy alone; turn settling is separate
        assert registry.tick(minutes * 60000) == ((tid,) if expected else ())
        assert store.get_thread(tid).conversation_state == ("dormant" if expected else "active")
        assert store.get_turn(turn) == before
        assert registry.tick(minutes * 60000) == ()


def test_cap_and_dry_run():
    with ProcessingStore(":memory:") as store:
        ids = [open_thread(store, str(i), at=i) for i in range(5)]
        registry = actors(store, lifecycle_enforce=False)
        before = store._conn.total_changes
        assert registry.tick(10) == ()
        assert store._conn.total_changes == before
        registry._policy = replace(registry._policy, lifecycle_enforce=True)
        assert registry.tick(10) == (ids[0],)
        assert store.get_thread(ids[0]).dormant_reason == "over_cap"


@pytest.mark.parametrize("busy", ["generation", "pending", "effect", "actor"])
def test_busy_thread_is_skipped(busy):
    with ProcessingStore(":memory:") as store:
        tid = open_thread(store, kind="dm")
        turn = store.open_turn(thread_id=tid, principal="human", trigger_event_id="root", now_ms=0)
        registry = actors(store)
        actor = registry.actor_for(tid)
        if busy == "generation":
            actor.freeze_snapshot(now_ms=1)
        elif busy == "pending":
            store.enqueue_pending_input(input_id="pending", thread_id=tid, event_id="next",
                                        principal="human", now_ms=1, turn_id=turn)
        elif busy == "effect":
            store._conn.execute("INSERT INTO effects (effect_id,operation_key,turn_id,payload_kind,"
                                "payload_hash,state,created_ms,updated_ms) "
                                "VALUES ('effect','op',?,'text','hash','queued',1,1)", (turn,))
        else:
            actor._generation_in_flight = True
        before = store._conn.total_changes
        assert registry.tick(3600000) == ()
        assert store._conn.total_changes == before
        assert registry.actor_for(tid) is actor
        assert store.get_turn(turn).state == "open"


def test_transition_routing_and_manual_new():
    with ProcessingStore(":memory:") as store:
        tid = open_thread(store, kind="dm")
        store.register_thread_message(message_id="quote", thread_id=tid, now_ms=0)
        assert store.mark_dormant([tid], 1, "idle_and_quiet") == (tid,)
        assert store.mark_dormant([tid], 2, "over_cap") == ()
        registry = ThreadRegistry(store=store)
        data = JoinInput(event_id="next", channel="test", chat_id="chat", principal="human")
        assert registry._active_threads(data, now_ms=2) == ()
        assert registry._last_dm_thread(data) is None
        moment = ThreadPolicy().reopen_window_ms + 100
        event = CanonicalEvent(event_id="next", event_key="next", trace_id="next", channel="test",
                               chat_id="chat", principal="human", origin="test",
                               payload={"reply_to_message_id": "quote"})
        result = registry.assign(event, now_ms=moment)
        assert result.thread_id == tid
        assert store.get_thread(tid).conversation_state == "active"
        assert not store.reactivate(tid, moment + 1)
        store.mark_dormant([tid], moment + 2, "idle_and_quiet")
        store.close_chat_threads(channel="test", chat_id="chat", now_ms=moment + 3)
        assert store.get_thread(tid).close_reason == "manual_new"
        assert not store.reactivate(tid, moment + 4)
        result = registry.assign(replace(event, event_id="third", event_key="third"), now_ms=moment + 4)
        assert result.thread_id != tid


def test_config_defaults_and_aliases():
    assert ProcessingThreadsConfig().lifecycle_enforce is True
    assert ProcessingThreadsConfig().lifecycle_tick_seconds == 60
    config = ProcessingThreadsConfig.model_validate({"lifecycleEnforce": False, "lifecycleTickSeconds": 7})
    assert ThreadPolicy.from_config(SimpleNamespace(threads=config)).lifecycle_enforce is False
    assert config.lifecycle_tick_seconds == 7


@pytest.mark.asyncio
async def test_maintenance_survives_exception_and_cancels(monkeypatch):
    registry = ThreadActorRegistry(store=SimpleNamespace())
    calls = []
    reached = asyncio.Event()

    def tick(moment):
        calls.append(moment)
        if len(calls) == 1:
            raise RuntimeError("synthetic private detail")
        reached.set()

    registry._interval_seconds = 0.001
    monkeypatch.setattr(registry, "tick", tick)
    await registry.start()
    task = registry._task
    try:
        await asyncio.wait_for(reached.wait(), 30)
    finally:
        await registry.stop()
    assert len(calls) >= 2
    assert task.done()
    assert registry._task is None


def test_first_tick_preserves_96_threads_and_208_open_turns():
    with ProcessingStore(":memory:") as store:
        ids = [open_thread(store, str(i), kind="dm") for i in range(96)]
        for i in range(208):
            store.open_turn(thread_id=ids[i % 96], principal="human",
                            trigger_event_id=f"turn-{i}", now_ms=0)
        before = tuple(tuple(row) for row in store._conn.execute("SELECT * FROM turns"))
        assert set(actors(store, idle_ms=10**12).tick(3600000)) == set(ids)
        assert len(store.list_threads(state="open", limit=None)) == 96
        assert tuple(tuple(row) for row in store._conn.execute("SELECT * FROM turns")) == before
        assert all(t.conversation_state == "dormant" for t in store.list_threads(limit=None))


def test_stale_turns_drain_in_bounded_idempotent_ticks():
    with ProcessingStore(":memory:") as store:
        ids = [open_thread(store, str(i), kind="dm") for i in range(96)]
        for i in range(208):
            store.open_turn(thread_id=ids[i % 96], principal="human",
                            trigger_event_id=f"turn-{i}", now_ms=0)
        recent = store.open_turn(thread_id=ids[0], principal="human", trigger_event_id="recent",
                                 now_ms=3_000_000)
        registry = actors(store)

        registry.tick(3_600_000)
        registry.tick(3_600_000)
        changes = store._conn.total_changes
        registry.tick(3_600_000)

        states = dict(store._conn.execute("SELECT state, COUNT(*) FROM turns GROUP BY state"))
        assert states == {"closed": 208, "open": 1}
        assert store.get_turn(recent).state == "open"
        assert len(store.list_threads(state="open", limit=None)) == 96
        assert store._conn.total_changes == changes


def test_turn_settling_waits_for_durable_work_and_respects_dry_run():
    with ProcessingStore(":memory:") as store:
        tid = open_thread(store, kind="dm")
        turn = store.open_turn(thread_id=tid, principal="human", trigger_event_id="root", now_ms=0)
        store.enqueue_pending_input(input_id="pending", thread_id=tid, event_id="next",
                                    principal="human", now_ms=1, turn_id=turn)
        assert store.settle_idle_turns(3_600_000, 1_800_000) == ()
        assert actors(store, lifecycle_enforce=False).tick(3_600_000) == ()
        assert store.get_turn(turn).state == "open"
        assert store.settle_idle_turns(3_600_000, 1_800_000, skip_threads=[tid]) == ()


def test_transition_logs_are_bounded_and_counted():
    from loguru import logger

    logs = []
    sink = logger.add(lambda message: logs.append(str(message)), format="{message}")
    try:
        with ProcessingStore(":memory:") as store:
            tid = open_thread(store, kind="dm")
            registry = actors(store)
            assert registry.tick(3600000) == (tid,)
            store.register_thread_message(message_id="anchor", thread_id=tid, now_ms=0)
            event = CanonicalEvent(event_id="reply", event_key="reply", trace_id="reply",
                                   channel="test", chat_id="chat", principal="human",
                                   origin="test", payload={"reply_to_message_id": "anchor",
                                                           "text": "private synthetic text"})
            ThreadRegistry(store=store).assign(event, now_ms=3600001)
            registry.tick(3600002)
        ticks = [line for line in logs if "thread_lifecycle_tick " in line]
        assert len(ticks) == 2
        assert "dormant_marked=1 over_cap=0 skipped_busy=0 reactivated=0" in ticks[0]
        assert "dormant_marked=0 over_cap=0 skipped_busy=0 reactivated=1" in ticks[1]
        transitions = [line for line in logs if "thread_lifecycle_reactivated " in line]
        assert len(transitions) == 1
        assert f"thread_id={tid} reason=reply_to_known_thread" in transitions[0]
        assert all("private synthetic text" not in line for line in logs)
    finally:
        logger.remove(sink)


@pytest.mark.asyncio
async def test_runtime_starts_lifecycle_independently():
    from unittest.mock import AsyncMock

    from yeoman_gateway.app.bootstrap import GatewayRuntime

    lifecycle = SimpleNamespace(start=AsyncMock())
    runtime = GatewayRuntime(orchestrator=None, channels=None, cron=None, heartbeat=None,
                             responder=None, history_projector=None, thread_lifecycle=lifecycle)
    await runtime._start_processing_services()
    lifecycle.start.assert_awaited_once()
