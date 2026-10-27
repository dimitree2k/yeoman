"""Synthetic coverage of the read-only thread lifecycle shadow."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from loguru import logger
from yeoman_gateway.core.models import InboundEvent, PolicyDecision
from yeoman_gateway.processing.lifecycle import lifecycle_verdicts
from yeoman_gateway.processing.models import CanonicalEvent, PolicySnapshot
from yeoman_gateway.processing.policy import IngestGate, IngestRequest
from yeoman_gateway.processing.store import ProcessingStore
from yeoman_gateway.processing.threads import ThreadPolicy, ThreadRegistry
from yeoman_shared.config.schema import ProcessingConfig, ProcessingThreadsConfig


def thread(name="a", last=0):
    return SimpleNamespace(thread_id=name, last_activity_ms=last, kind="group")


@pytest.mark.parametrize(
    "minutes,messages,state", [(59, 5, "active"), (60, 4, "active"), (60, 5, "dormant")]
)
def test_boundaries(minutes, messages, state):
    verdict = lifecycle_verdicts(
        [thread()], {"a": messages}, now_ms=minutes * 60000, is_group=True, policy=ThreadPolicy()
    )["a"]
    assert verdict.state == state
    assert verdict.reason == ("active" if state == "active" else "idle_and_quiet")


def test_dm_and_cap():
    assert (
        lifecycle_verdicts([thread()], {}, now_ms=3600000, is_group=False, policy=ThreadPolicy())[
            "a"
        ].state
        == "dormant"
    )
    threads = [thread(name, 1 if name == "a" else 2) for name in "edcba"]
    policy = replace(ThreadPolicy(), max_active_per_chat=3)
    verdicts = lifecycle_verdicts(threads, {}, now_ms=100, is_group=True, policy=policy)
    assert [key for key, v in verdicts.items() if v.reason == "over_cap"] == ["a", "b"]
    assert verdicts == lifecycle_verdicts(
        list(reversed(threads)), {}, now_ms=100, is_group=True, policy=policy
    )


def test_config():
    config = ProcessingThreadsConfig.model_validate(
        {
            "dormantAfterSeconds": 9,
            "dormantAfterMessages": 8,
            "dormantAfterMessagesDm": 7,
            "maxActivePerChat": 6,
            "lifecycleShadow": False,
        }
    )
    policy = ThreadPolicy.from_config(SimpleNamespace(threads=config))
    assert (
        policy.dormant_after_seconds,
        policy.dormant_after_messages,
        policy.dormant_after_messages_dm,
        policy.max_active_per_chat,
        policy.lifecycle_shadow,
    ) == (9, 8, 7, 6, False)


def test_counting():
    with ProcessingStore(":memory:") as store:
        tid = store.open_thread(
            channel="test",
            chat_id="chat",
            root_principal="human",
            kind="group",
            trigger_event_id="root",
            now_ms=100,
        )

        def add(key, source, **kwargs):
            fields = dict(
                event_id=key,
                event_key=key,
                trace_id=key,
                channel="test",
                chat_id="chat",
                principal="human",
                origin="test",
                occurred_ms=101,
                source_message_id=source,
            )
            fields.update(kwargs)
            store.append_event(
                event_key=key,
                event_id=key,
                trace_id=key,
                payload=CanonicalEvent(**fields),
                now_ms=101,
            )

        add("own", "own", thread_id=tid, occurred_ms=100)
        add("own-duplicate", "own")
        add("other", "other")
        add("duplicate", "other")
        add("bot", "bot", direction="out")
        add("system", "system", origin="system")
        add("bot-principal", "bot-principal", principal="bot")
        add("service-principal", "service-principal", principal="service:cron")
        add("before", "before", occurred_ms=100)
        add("future", "future", occurred_ms=201)
        add("different", "different", chat_id="elsewhere")
        add("different-channel", "different-channel", channel="elsewhere")
        add("edit", "edit", kind="edit")
        add("canonical", "canonical", channel="whatsapp", origin="whatsapp_canonical")
        threads = store.list_threads(channel="test", chat_id="chat", state="open")
        before = store._conn.total_changes
        assert store.lifecycle_other_message_counts(
            channel="test", chat_id="chat", threads=threads, now_ms=200, cap=5
        ) == {tid: 1}
        assert store._conn.total_changes == before
        assert store.lifecycle_other_message_counts(
            channel="whatsapp", chat_id="chat", threads=threads, now_ms=200, cap=5
        ) == {tid: 1}


@pytest.mark.parametrize("mode", ["on", "off", "error"])
def test_hook_preserves_routing(mode, monkeypatch):
    config = ProcessingConfig(enabled=True, chats=["test:chat"])
    config.threads.lifecycle_shadow = mode != "off"
    logs = []
    sink = logger.add(lambda msg: logs.append(str(msg)), format="{message}")
    try:
        with ProcessingStore(":memory:") as store:
            gate = IngestGate(
                config=config,
                store=store,
                snapshots=SimpleNamespace(snapshot=lambda: PolicySnapshot("v", "hash")),
                evaluate=lambda request: PolicyDecision(
                    accept_message=True,
                    should_respond=True,
                    allowed_tools=frozenset(),
                    reason="allow",
                ),
                threads=ThreadRegistry(store=store, config=config),
                clock=lambda: 3600000,
            )
            request = IngestRequest(
                "key",
                "event",
                "trace",
                InboundEvent(
                    channel="test",
                    chat_id="chat",
                    sender_id="human",
                    content="synthetic",
                    message_id="message",
                    is_group=True,
                    mentioned_bot=True,
                ),
            )
            if mode == "error":

                def fail(**kwargs):
                    raise RuntimeError("synthetic secret must not be logged")

                monkeypatch.setattr(store, "lifecycle_other_message_counts", fail)
            result = gate.admit(request)
            assert result.outcome.value == "react"
            assert result.assignment.new_thread
            assert store.get_thread(result.assignment.thread_id).state == "open"
            assert store.count_events() == 1
            assert store.count_effects() == 0
        shadow = [line for line in logs if "thread_lifecycle_shadow" in line]
        assert len(shadow) == (0 if mode == "off" else 1)
        if mode == "on":
            assert "open=1 active=1 dormant=0 over_cap=0" in shadow[0]
            assert "current_state=active" in shadow[0]
        if mode == "error":
            assert "failed error_type=RuntimeError" in shadow[0]
        assert all("synthetic" not in line for line in logs)
    finally:
        logger.remove(sink)
