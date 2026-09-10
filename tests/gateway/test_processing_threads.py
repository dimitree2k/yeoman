"""Plan 03 / R03: durable threads, the six join rules and thread lifetime."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from yeoman_gateway.processing.models import (
    THREAD_STATES,
    TURN_STATES,
    CanonicalEvent,
    ProcessingError,
)
from yeoman_gateway.processing.store import SCHEMA_VERSION, ProcessingStore
from yeoman_gateway.processing.threads import (
    JOIN_RULES,
    JoinDecision,
    JoinRule,
    ThreadRegistry,
)

DAY_MS = 86_400_000
T0 = 1_700_000_000_000


class _Config:
    """The v1 thread defaults, read-only (no new config keys in Plan 03)."""

    class Threads:
        followup_window_seconds = 15
        idle_seconds = 1800
        reopen_window_seconds = 604800
        pending_inputs_per_thread = 32


def _event(
    *,
    event_id: str,
    key: str | None = None,
    principal: str = "owner@s.whatsapp.net",
    chat_id: str = "chat@g.us",
    kind: str = "message",
    occurred_ms: int = T0,
    source_message_id: str | None = None,
    **payload: object,
) -> CanonicalEvent:
    body = {"kind": kind, "text": "hello", **payload}
    return CanonicalEvent(
        event_id=event_id,
        event_key=key or f"wa:{event_id}",
        trace_id=f"tr-{event_id}",
        kind=kind,
        origin="whatsapp",
        principal=principal,
        channel="whatsapp",
        chat_id=chat_id,
        occurred_ms=occurred_ms,
        source_message_id=source_message_id or event_id,
        payload=body,
    )


def _registry(store: ProcessingStore) -> ThreadRegistry:
    return ThreadRegistry(store=store, config=_Config())


# --------------------------------------------------------------------------------------
# rule order and classification
# --------------------------------------------------------------------------------------


def test_join_rules_are_the_spec_order() -> None:
    assert JOIN_RULES == (
        JoinRule.REPLY_KNOWN,
        JoinRule.EXPLICIT_CORRECTION,
        JoinRule.FOLLOWUP_SINGLE_ACTIVE,
        JoinRule.MENTION_NO_REFERENCE,
        JoinRule.DM_LAST_ACTIVE,
        JoinRule.AMBIENT,
    )


def test_thread_and_turn_state_constants_exist() -> None:
    assert THREAD_STATES == ("open", "idle", "closed")
    assert TURN_STATES == ("open", "awaiting", "closed", "superseded")


def test_new_thread_is_deterministic_and_idempotent(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "p.db")
    registry = _registry(store)
    event = _event(event_id="m1", mentioned_bot=True)

    first = registry.assign(event, now_ms=T0)
    second = registry.assign(event, now_ms=T0 + 5)

    assert isinstance(first, JoinDecision)
    assert first.rule is JoinRule.MENTION_NO_REFERENCE
    assert first.thread_id == second.thread_id
    assert first.turn_id == second.turn_id
    assert store.list_threads(state="open") and len(store.list_threads(state="open")) == 1
    store.close()


# --------------------------------------------------------------------------------------
# the six rules, one case each
# --------------------------------------------------------------------------------------


def test_rule_1_reply_to_known_thread_message_joins_that_thread(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "p.db")
    registry = _registry(store)
    first = registry.assign(_event(event_id="m1", mentioned_bot=True), now_ms=T0)
    # The bot's own outgoing message becomes a confirmed anchor for that thread.
    store.register_thread_message(
        thread_id=first.thread_id, turn_id=first.turn_id, direction="out",
        effect_id="fx1", now_ms=T0,
    )
    assert store.attach_confirmed_message_id("fx1", "prov-1", T0) is True

    reply = registry.assign(
        _event(event_id="m2", reply_to_message_id="prov-1", mentioned_bot=False),
        now_ms=T0 + 60_000,
    )

    assert reply.rule is JoinRule.REPLY_KNOWN
    assert reply.thread_id == first.thread_id
    assert reply.turn_id != first.turn_id  # a new turn in the same thread
    store.close()


def test_rule_1_planned_effect_without_provider_id_is_not_an_anchor(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "p.db")
    registry = _registry(store)
    first = registry.assign(_event(event_id="m1", mentioned_bot=True), now_ms=T0)
    store.register_thread_message(
        thread_id=first.thread_id, turn_id=first.turn_id, direction="out",
        effect_id="fx1", now_ms=T0,
    )

    # The unconfirmed id resolves nothing, so rule 1 cannot fire (the follow-up rule may
    # still join the active thread - that is the spec's priority order, not an anchor).
    assert store.thread_for_message("prov-1") is None
    assert store.resolve_reference("prov-1") is None

    reply = registry.assign(
        _event(event_id="m2", reply_to_message_id="prov-1", mentioned_bot=True), now_ms=T0 + 1
    )

    assert reply.rule is not JoinRule.REPLY_KNOWN
    store.close()


def test_rule_2_explicit_correction_of_the_orderer_selects_that_turn(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "p.db")
    registry = _registry(store)
    first = registry.assign(_event(event_id="m1", mentioned_bot=True), now_ms=T0)

    correction = registry.assign(
        _event(
            event_id="m2",
            kind="delete",
            explicit_correction=True,
            target_message_id=first.turn_id,
        ),
        now_ms=T0 + 5_000,
    )

    assert correction.rule is JoinRule.EXPLICIT_CORRECTION
    assert correction.thread_id == first.thread_id
    assert correction.supersedes is True
    store.close()


def test_rule_3_followup_within_window_extends_the_single_active_thread(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "p.db")
    registry = _registry(store)
    first = registry.assign(_event(event_id="m1", mentioned_bot=True), now_ms=T0)

    followup = registry.assign(_event(event_id="m2", mentioned_bot=False), now_ms=T0 + 5_000)

    assert followup.rule is JoinRule.FOLLOWUP_SINGLE_ACTIVE
    assert followup.thread_id == first.thread_id
    assert followup.turn_id == first.turn_id  # same turn, additional context
    store.close()


def test_rule_3_does_not_apply_outside_the_window_or_with_a_topic_break(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "p.db")
    registry = _registry(store)
    first = registry.assign(_event(event_id="m1", mentioned_bot=True), now_ms=T0)

    late = registry.assign(_event(event_id="m2"), now_ms=T0 + 60_000)
    assert late.rule is not JoinRule.FOLLOWUP_SINGLE_ACTIVE
    assert late.thread_id != first.thread_id

    store2 = ProcessingStore(tmp_path / "q.db")
    registry2 = ThreadRegistry(store=store2, config=_Config())
    base = registry2.assign(_event(event_id="n1", mentioned_bot=True), now_ms=T0)
    switched = registry2.assign(_event(event_id="n2", topic_break=True), now_ms=T0 + 1_000)
    assert switched.rule is not JoinRule.FOLLOWUP_SINGLE_ACTIVE
    assert switched.thread_id != base.thread_id
    store.close()
    store2.close()


def test_rule_3_needs_exactly_one_active_thread(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "p.db")
    registry = _registry(store)
    # A topic break keeps the second thread separate; otherwise rule 3 would (correctly)
    # extend the single active thread instead of opening a new one.
    first = registry.assign(_event(event_id="m1", mentioned_bot=True), now_ms=T0)
    second = registry.assign(
        _event(event_id="m2", mentioned_bot=True, topic_break=True), now_ms=T0 + 1_000
    )
    assert second.thread_id != first.thread_id

    followup = registry.assign(_event(event_id="m3"), now_ms=T0 + 2_000)

    assert followup.rule is not JoinRule.FOLLOWUP_SINGLE_ACTIVE
    assert followup.thread_id not in {first.thread_id, second.thread_id}
    store.close()


def test_rule_4_mention_without_reference_starts_a_thread(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "p.db")
    registry = _registry(store)
    decision = registry.assign(_event(event_id="m1", mentioned_bot=True), now_ms=T0)
    assert decision.rule is JoinRule.MENTION_NO_REFERENCE
    assert decision.turn_id is not None
    store.close()


def test_rule_5_dm_without_reply_uses_the_last_active_dm_thread(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "p.db")
    registry = _registry(store)
    first = registry.assign(
        _event(event_id="m1", chat_id="owner@s.whatsapp.net", mentioned_bot=True), now_ms=T0
    )

    followup = registry.assign(
        _event(event_id="m2", chat_id="owner@s.whatsapp.net"), now_ms=T0 + 120_000
    )

    assert followup.rule is JoinRule.DM_LAST_ACTIVE
    assert followup.thread_id == first.thread_id
    store.close()


def test_rule_6_ambient_creates_no_turn_and_no_mailbox(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "p.db")
    registry = _registry(store)
    decision = registry.assign(
        _event(event_id="m1", principal="other@s.whatsapp.net", mentioned_bot=False), now_ms=T0
    )

    assert decision.rule is JoinRule.AMBIENT
    assert decision.turn_id is None
    assert store.count_pending(thread_id=None) == 0
    store.close()


def test_reaction_observes_and_never_opens_a_turn(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "p.db")
    registry = _registry(store)
    first = registry.assign(_event(event_id="m1", mentioned_bot=True), now_ms=T0)
    store.register_thread_message(
        thread_id=first.thread_id, turn_id=first.turn_id, direction="out",
        effect_id="fx1", now_ms=T0,
    )
    store.attach_confirmed_message_id("fx1", "prov-1", T0)

    decision = registry.assign(
        _event(event_id="m2", kind="reaction", reply_to_message_id="prov-1"), now_ms=T0 + 1_000
    )

    assert decision.observes_only is True
    assert decision.turn_id is None
    store.close()


# --------------------------------------------------------------------------------------
# lifetime
# --------------------------------------------------------------------------------------


def test_idle_threads_close_but_reopen_within_the_window(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "p.db")
    registry = _registry(store)
    first = registry.assign(_event(event_id="m1", mentioned_bot=True), now_ms=T0)

    store.register_thread_message(
        thread_id=first.thread_id, turn_id=first.turn_id, direction="out",
        effect_id="fx1", now_ms=T0,
    )
    assert store.attach_confirmed_message_id("fx1", "prov-1", T0) is True

    closed = store.close_idle_threads(T0 + 31 * 60_000, 1800 * 1000)
    assert closed == (first.thread_id,)
    assert store.get_thread(first.thread_id).state == "closed"

    # Spec R03.1: a reply reopens a sleeping thread inside the reopen window.
    reopened = registry.assign(
        _event(event_id="m2", reply_to_message_id="prov-1"), now_ms=T0 + 40 * 60_000
    )
    assert reopened.rule is JoinRule.REPLY_KNOWN
    assert reopened.thread_id == first.thread_id
    assert reopened.new_turn is True
    assert store.get_thread(first.thread_id).state == "open"

    # A plain mention without a reference does not revive a closed thread.
    fresh = registry.assign(_event(event_id="m3", mentioned_bot=True), now_ms=T0 + 41 * 60_000)
    assert fresh.thread_id != first.thread_id
    store.close()


def test_a_thread_with_open_work_is_not_closed(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "p.db")
    registry = _registry(store)
    first = registry.assign(_event(event_id="m1", mentioned_bot=True), now_ms=T0)
    store.enqueue_effect(
        effect_id="fx1",
        operation_key="k1",
        payload={"text": "hi"},
        target={"channel": "whatsapp", "chat_id": "chat@g.us"},
        turn_id=first.turn_id,
        now_ms=T0,
    )

    assert store.close_idle_threads(T0 + 31 * 60_000, 1800 * 1000) == ()
    store.close()


def test_thread_without_sources_is_not_reopened_after_retention(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "p.db")
    registry = _registry(store)
    first = registry.assign(_event(event_id="m1", mentioned_bot=True), now_ms=T0)
    store.close_idle_threads(T0 + 31 * 60_000, 1800 * 1000)
    # Retention removed the payload: the thread must not be revived from deleted content.
    store.purge(now_ms=T0 + 8 * DAY_MS)

    reopened = registry.assign(
        _event(event_id="m2", mentioned_bot=True), now_ms=T0 + 8 * DAY_MS
    )

    assert reopened.thread_id != first.thread_id
    store.close()


# --------------------------------------------------------------------------------------
# schema migration
# --------------------------------------------------------------------------------------

_V1_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (
  event_id TEXT PRIMARY KEY, event_key TEXT NOT NULL UNIQUE, trace_id TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'message', origin TEXT NOT NULL DEFAULT 'unknown',
  channel TEXT NOT NULL DEFAULT '', chat_id TEXT NOT NULL DEFAULT '',
  principal TEXT NOT NULL DEFAULT '', source_message_id TEXT, target_message_id TEXT,
  thread_id TEXT, turn_id TEXT, occurred_ms INTEGER, payload_hash TEXT NOT NULL,
  payload_json TEXT, payload_purged_ms INTEGER, created_ms INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS effects (
  effect_id TEXT PRIMARY KEY, operation_key TEXT NOT NULL UNIQUE,
  trace_id TEXT NOT NULL DEFAULT '', turn_id TEXT NOT NULL DEFAULT '',
  turn_revision INTEGER NOT NULL DEFAULT 1, principal TEXT NOT NULL DEFAULT '',
  capability TEXT NOT NULL DEFAULT '', target_json TEXT NOT NULL DEFAULT '{}',
  target_hash TEXT NOT NULL DEFAULT '', payload_kind TEXT NOT NULL,
  payload_hash TEXT NOT NULL, payload_json TEXT, payload_purged_ms INTEGER,
  state TEXT NOT NULL, expires_at_ms INTEGER, policy_version TEXT, policy_hash TEXT,
  lease_owner TEXT, lease_until_ms INTEGER, created_ms INTEGER NOT NULL,
  updated_ms INTEGER NOT NULL);
"""


def test_v1_database_migrates_additively(tmp_path: Path) -> None:
    path = tmp_path / "p.db"
    legacy = sqlite3.connect(path)
    legacy.executescript(_V1_SCHEMA)
    legacy.execute("INSERT INTO meta (key, value) VALUES ('schema_version', '1')")
    legacy.execute(
        "INSERT INTO events (event_id, event_key, trace_id, kind, payload_hash, payload_json,"
        " created_ms) VALUES ('e1','k1','tr1','message','h','{}',1)"
    )
    legacy.execute(
        "INSERT INTO effects (effect_id, operation_key, payload_kind, payload_hash, state,"
        " created_ms, updated_ms) VALUES ('fx1','op1','text','h','sent',1,1)"
    )
    legacy.commit()
    legacy.close()

    store = ProcessingStore(path)

    assert SCHEMA_VERSION == 3
    assert store.schema_version == 3
    assert store.count_events() == 1
    assert store.count_effects() == 1
    assert store.list_threads() == ()
    assert store.transport_receipts("fx1") == ()  # Plan 04 table exists after migration
    assert store.quick_check() == "ok"
    store.close()


def test_schema_newer_than_code_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "p.db"
    store = ProcessingStore(path)
    store.close()
    con = sqlite3.connect(path)
    con.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
    con.commit()
    con.close()

    with pytest.raises(ProcessingError):
        ProcessingStore(path)


def test_assignment_exposes_the_thread_sources(tmp_path: Path) -> None:
    """The decision carries the thread's source message ids for ambient dedup."""
    store = ProcessingStore(tmp_path / "p.db")
    registry = _registry(store)
    first = registry.assign(
        _event(event_id="m1", source_message_id="m1", mentioned_bot=True), now_ms=T0
    )
    assert first.source_message_ids == ("m1",)

    followup = registry.assign(
        _event(event_id="m2", source_message_id="m2", reply_to_message_id="m1"), now_ms=T0 + 1
    )
    assert set(followup.source_message_ids) >= {"m1", "m2"}
    store.close()


def test_ambient_window_never_repeats_the_own_thread() -> None:
    """Spec R03: ambient is background for other threads, not a copy of this one."""
    from datetime import UTC, datetime

    from yeoman_gateway.core.models import ArchivedMessage, InboundEvent
    from yeoman_gateway.pipeline.reply_context import ReplyContextMiddleware

    class _Archive:
        def lookup_messages_before(self, channel, chat_id, message_id, limit):
            del channel, chat_id, message_id, limit
            return [
                ArchivedMessage(
                    channel="whatsapp",
                    chat_id="chat@g.us",
                    message_id="own-1",
                    participant="p",
                    sender_id="s",
                    text="own thread message",
                    timestamp=None,
                    created_at="",
                ),
                ArchivedMessage(
                    channel="whatsapp",
                    chat_id="chat@g.us",
                    message_id="other-1",
                    participant="p",
                    sender_id="s",
                    text="other thread message",
                    timestamp=None,
                    created_at="",
                ),
            ]

    middleware = ReplyContextMiddleware(
        archive=_Archive(), reply_context_window_limit=5, reply_context_line_max_chars=200,
        ambient_window_limit=5,
    )
    event = InboundEvent(
        channel="whatsapp",
        chat_id="chat@g.us",
        sender_id="owner@s.whatsapp.net",
        content="hi",
        message_id="m3",
        is_group=True,
        timestamp=datetime(2023, 11, 14, tzinfo=UTC),
        raw_metadata={"thread_source_message_ids": ["own-1"]},
    )

    lines = middleware._build_ambient_window(event)

    assert any("other thread message" in line for line in lines)
    assert not any("own thread message" in line for line in lines)


def test_tool_context_is_per_turn_not_per_instance() -> None:
    """Two overlapping turns must not leak their target into each other's tool call."""
    import asyncio

    from yeoman_gateway.agent.tools.message import MessageTool
    from yeoman_gateway.processing.tool_context import (
        ToolInvocationContext,
        reset_tool_context,
        set_tool_context,
    )

    sent: list[tuple[str, str]] = []

    async def _send(message) -> None:
        sent.append((message.channel, message.chat_id))

    tool = MessageTool(send_callback=_send)
    tool.set_context("whatsapp", "shared-default@g.us")

    async def _turn(chat_id: str) -> str:
        token = set_tool_context(ToolInvocationContext(channel="whatsapp", chat_id=chat_id))
        try:
            await asyncio.sleep(0)  # let the other turn interleave
            return await tool.execute(content="hi")
        finally:
            reset_tool_context(token)

    async def _main() -> list[str]:
        return await asyncio.gather(_turn("chat-a@g.us"), _turn("chat-b@g.us"))

    asyncio.run(_main())

    assert sorted(sent) == [("whatsapp", "chat-a@g.us"), ("whatsapp", "chat-b@g.us")]
    assert ("whatsapp", "shared-default@g.us") not in sent


def test_tool_context_falls_back_to_the_instance_default() -> None:
    import asyncio

    from yeoman_gateway.agent.tools.message import MessageTool

    sent: list[tuple[str, str]] = []

    async def _send(message) -> None:
        sent.append((message.channel, message.chat_id))

    tool = MessageTool(send_callback=_send)
    tool.set_context("whatsapp", "shared-default@g.us")

    asyncio.run(tool.execute(content="hi"))

    assert sent == [("whatsapp", "shared-default@g.us")]


def test_first_dm_message_opens_a_thread(tmp_path: Path) -> None:
    """A plain DM mentions nothing and replies to nothing: it must still open a thread."""
    store = ProcessingStore(tmp_path / "p.db")
    registry = _registry(store)

    decision = registry.assign(
        _event(event_id="dm1", chat_id="owner@s.whatsapp.net", mentioned_bot=False), now_ms=T0
    )

    assert decision.rule is JoinRule.DM_LAST_ACTIVE
    assert decision.thread_id is not None
    assert decision.turn_id is not None
    assert store.get_thread(decision.thread_id).state == "open"

    followup = registry.assign(
        _event(event_id="dm2", chat_id="owner@s.whatsapp.net", mentioned_bot=False),
        now_ms=T0 + 120_000,
    )
    assert followup.thread_id == decision.thread_id  # still the last active DM thread
    assert followup.turn_id != decision.turn_id
    store.close()


def test_first_message_of_every_shape_is_decided(tmp_path: Path) -> None:
    """Pins the class of gap live traffic found: the *first* message of each shape.

    A DM without mention or reply used to fall through to ambient and never open a
    thread; every shape below must produce a definite decision on an empty store.
    """
    store = ProcessingStore(tmp_path / "p.db")
    registry = _registry(store)

    cases = {
        "dm_plain": (_event(event_id="dm1", chat_id="owner@s.whatsapp.net"), JoinRule.DM_LAST_ACTIVE),
        "group_mention": (_event(event_id="g1", mentioned_bot=True), JoinRule.MENTION_NO_REFERENCE),
        "group_ambient": (_event(event_id="g2", principal="other@s.whatsapp.net"), JoinRule.AMBIENT),
    }
    for name, (event, expected_rule) in cases.items():
        decision = registry.assign(event, now_ms=T0)
        assert decision.rule is expected_rule, name
        if expected_rule is JoinRule.AMBIENT:
            assert decision.thread_id is None and decision.turn_id is None, name
        else:
            assert decision.thread_id and decision.turn_id, name
    store.close()


def test_observed_message_gets_lineage_but_no_turn(tmp_path: Path) -> None:
    """allow_turn=False (fast gate OBSERVE): lineage only, no turn, no mailbox."""
    store = ProcessingStore(tmp_path / "p.db")
    registry = _registry(store)

    decision = registry.assign(
        _event(event_id="dm1", chat_id="owner@s.whatsapp.net"), now_ms=T0, allow_turn=False
    )

    assert decision.thread_id is not None
    assert decision.turn_id is None
    assert store.active_turn(decision.thread_id) is None
    assert store.count_pending(thread_id=decision.thread_id) == 0
    store.close()
