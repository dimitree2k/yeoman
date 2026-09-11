"""Plan 06 / Aufgabe 2: end-to-end matrix over the real pipeline.

Only the provider and network boundaries are faked (transport, policy file, clock); the
gate, store, registry, effect router and dispatcher are the production objects.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from yeoman_gateway.adapters.policy_engine import EnginePolicyAdapter
from yeoman_gateway.app.bootstrap import (
    build_effect_router,
    build_processing_gate,
    build_processing_store,
    build_thread_registry,
)
from yeoman_gateway.bus.events import OutboundMessage
from yeoman_gateway.bus.queue import MessageBus
from yeoman_gateway.core.models import InboundEvent
from yeoman_gateway.policy.engine import PolicyEngine
from yeoman_gateway.policy.loader import save_policy
from yeoman_gateway.policy.schema import PolicyConfig
from yeoman_gateway.processing.dispatch import EffectNotDeliveredError, ManagedOutboundDispatcher
from yeoman_gateway.processing.policy import IngestRequest
from yeoman_shared.config.schema import Config

CHAT = "pilot@g.us"
OTHER = "second@g.us"
T0 = 1_700_000_000_000


class _Clock:
    def __init__(self, value: int = T0) -> None:
        self.value = value

    def __call__(self) -> int:
        return self.value


@dataclass
class _Transport:
    """Stands in for ChannelManager.send_now()."""

    sent: list[str] = field(default_factory=list)
    fail: bool = False

    async def send_now(self, message) -> None:
        if self.fail:
            raise TimeoutError("bridge timeout")
        self.sent.append(message.content)

    async def send_reaction_now(self, message) -> None:
        self.sent.append(message.emoji)


def _config(
    *,
    chats: tuple[str, ...] = (CHAT,),
    waiting_cap: int = 20,
    soft_enforce: bool = False,
    ambient: tuple[str, ...] = (),
    ambient_brake: tuple[int, int] = (0, 0),
) -> Config:
    # The ambient brake is off in these tests unless a test asks for it: they are about
    # permission and lineage, and the brake has tests of its own.
    seconds, messages = ambient_brake
    return Config.model_validate(
        {
            "processing": {
                "enabled": True,
                "chats": [f"whatsapp:{chat}" for chat in chats],
                "ambient_chats": [f"whatsapp:{chat}" for chat in ambient],
                "ambient": {
                    "min_seconds_between_answers": seconds,
                    "min_messages_since_answer": messages,
                },
                "budgets": {
                    "outbox_waiting_per_chat": waiting_cap,
                    "thread_soft_enforce": soft_enforce,
                },
            },
            "security": {"enabled": False},
        }
    )


def _policy(
    chats: tuple[str, ...] = (CHAT,),
    *,
    mode: str = "everyone",
    when_to_reply: str = "all",
    senders: tuple[str, ...] = (),
) -> PolicyConfig:
    when: dict[str, object] = {"mode": when_to_reply}
    if senders:
        when["senders"] = list(senders)
    default = {"whoCanTalk": {"mode": mode}, "whenToReply": when}
    return PolicyConfig.model_validate(
        {
            "defaults": {"allowedTools": {"mode": "allowlist", "tools": ["message"]}},
            "owners": {"whatsapp": ["owner@s.whatsapp.net"]},
            "runtime": {"reloadOnChange": True},
            "channels": {"whatsapp": {"default": default}},
        }
    )


@dataclass
class _Runtime:
    store: object
    registry: object
    gate: object
    router: object
    transport: _Transport
    adapter: EnginePolicyAdapter
    policy_path: Path
    workspace: Path
    config: Config

    def reload_policy(self, policy: PolicyConfig) -> None:
        """Write and pick up a new policy, bypassing the production check interval."""
        save_policy(policy, self.policy_path)
        self.adapter._last_reload_check = 0.0  # test-only: skip the throttle window
        self.adapter._maybe_reload()


@pytest.fixture()
def runtime(tmp_path: Path):
    _rt = _make_runtime(tmp_path)
    yield _rt
    _rt.store.close()


def _make_runtime(
    tmp_path: Path,
    *,
    chats: tuple[str, ...] = (CHAT,),
    waiting_cap: int = 20,
    soft_enforce: bool = False,
    ambient: tuple[str, ...] = (),
    ambient_brake: tuple[int, int] = (0, 0),
    **kwargs,
) -> _Runtime:
    policy_path = tmp_path / "policy.json"
    policy = _policy(chats, **kwargs)
    save_policy(policy, policy_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    adapter = EnginePolicyAdapter(
        engine=PolicyEngine(policy, workspace=workspace, apply_channels={"whatsapp"}),
        known_tools={"message"},
        policy_path=policy_path,
        workspace=workspace,
    )
    config = _config(
        chats=chats,
        waiting_cap=waiting_cap,
        soft_enforce=soft_enforce,
        ambient=ambient,
        ambient_brake=ambient_brake,
    )
    # Pin the database: reopening must never fall back to the default (live) path.
    config.processing.db_path = str(tmp_path / "processing.db")
    with patch.dict(os.environ, {"YEOMAN_HOME": str(tmp_path)}):
        store = build_processing_store(config)
        assert store is not None
        registry = build_thread_registry(config, store)
        gate = build_processing_gate(config, adapter, store, registry)
        router = build_effect_router(config, adapter, store, MessageBus(), threads=registry)
    assert registry is not None and gate is not None and router is not None
    transport = _Transport()
    router.set_direct_transport(transport.send_now, transport.send_reaction_now)
    return _Runtime(
        store=store,
        registry=registry,
        gate=gate,
        router=router,
        transport=transport,
        adapter=adapter,
        policy_path=policy_path,
        workspace=workspace,
        config=config,
    )


def _event(
    *,
    message_id: str,
    content: str = "hi",
    chat: str = CHAT,
    mentioned: bool = True,
    sender: str = "orderer@s.whatsapp.net",
) -> InboundEvent:
    return InboundEvent(
        channel="whatsapp",
        chat_id=chat,
        sender_id=sender,
        content=content,
        message_id=message_id,
        is_group=True,
        mentioned_bot=mentioned,
        timestamp=datetime(2023, 11, 14, tzinfo=UTC),
    )


def _admit(
    runtime: _Runtime,
    *,
    message_id: str,
    chat: str = CHAT,
    content: str = "hi",
    mentioned: bool = True,
    sender: str = "orderer@s.whatsapp.net",
):
    return runtime.gate.admit(
        IngestRequest(
            event_key=f"whatsapp:{chat}:{message_id}",
            event_id=message_id,
            trace_id=f"tr-{message_id}",
            event=_event(
                message_id=message_id,
                chat=chat,
                content=content,
                mentioned=mentioned,
                sender=sender,
            ),
        )
    )


@pytest.mark.asyncio
async def test_implicit_reply_reconciles_an_ambient_event_into_a_turn(runtime) -> None:
    # An ambient event only becomes a turn in a chat the owner released for ambient
    # answers; the shared fixture is deliberately neutral, so this test opts in.
    runtime.registry._ambient_chats = frozenset({f"whatsapp:{CHAT}"})
    runtime.reload_policy(_policy(when_to_reply="mention_only"))
    event = _event(message_id="ambient-1", content="Arvid, das ist wichtig", mentioned=False)
    verdict = runtime.gate.admit(
        IngestRequest(
            event_key=f"whatsapp:{CHAT}:ambient-1",
            event_id="ambient-1",
            trace_id="tr-ambient-1",
            event=event,
        )
    )

    assert verdict is not None
    assert verdict.outcome.value == "observe"
    assert verdict.assignment is not None
    assert verdict.assignment.turn_id is None

    promoted = _event(
        message_id="ambient-1",
        content=event.content,
        mentioned=True,
    )
    assignment = runtime.gate.reconcile_reply(promoted)

    assert assignment is not None
    assert assignment.thread_id is not None
    assert assignment.turn_id is not None
    assert runtime.store.event_assignment("ambient-1") == (
        assignment.thread_id,
        assignment.turn_id,
    )
    await _dispatch(runtime, message_id="ambient-1")
    assert runtime.transport.sent == ["answer"]


async def _dispatch(
    runtime: _Runtime,
    *,
    chat: str = CHAT,
    content: str = "answer",
    message_id: str = "m1",
    principal: str | None = None,
):
    """Dispatch one managed reply. ``principal`` comes from the runtime, never metadata."""
    dispatcher = ManagedOutboundDispatcher(
        router=runtime.router,
        bus=MessageBus(),
        principal=(lambda: principal) if principal else None,
    )
    await dispatcher(
        OutboundMessage(
            channel="whatsapp", chat_id=chat, content=content, metadata={"message_id": message_id}
        )
    )


@pytest.mark.asyncio
async def test_an_unmarked_follow_up_needs_a_positive_signal(tmp_path: Path) -> None:
    """Routing spec: no proven continuity, no automatic attachment - both halves.

    A missing topic break is not proof of continuity, so a new subject opens its own
    thread; an explicit call-back to the subject continues the existing one.
    """
    # Half 1: a new subject is not attached to the open thread.
    fresh = _make_runtime(tmp_path / "new-subject")
    try:
        first = _admit(fresh, message_id="m1", content="Fasse den Mietvertrag zusammen.")
        other = _admit(fresh, message_id="m2", content="Wie wird morgen das Wetter?")
        assert first.assignment is not None and other.assignment is not None
        assert other.assignment.thread_id != first.assignment.thread_id
    finally:
        fresh.store.close()

    # Half 2: an explicit call-back to the subject continues the thread.
    runtime = _make_runtime(tmp_path / "call-back")
    try:
        started = _admit(runtime, message_id="m1", content="Fasse den Mietvertrag zusammen.")
        continued = _admit(
            runtime,
            message_id="m2",
            content="Zum Mietvertrag: ergänze bitte die Kündigungsfrist.",
        )
        assert continued.assignment is not None
        assert continued.assignment.thread_id == started.assignment.thread_id
        await _dispatch(runtime, message_id="m1")
        await _dispatch(runtime, message_id="m2")

        effects = runtime.store.list_effects()
        assert len(effects) == 2
        assert {effect.state for effect in effects} == {"sent"}
        assert len({effect.turn_id for effect in effects}) == 1  # one bundled turn
    finally:
        runtime.store.close()


@pytest.mark.asyncio
async def test_two_chats_keep_their_own_budget_and_effects(tmp_path: Path) -> None:
    runtime = _make_runtime(tmp_path, chats=(CHAT, OTHER))
    try:
        _admit(runtime, message_id="m1", chat=CHAT)
        _admit(runtime, message_id="m2", chat=OTHER)
        await _dispatch(runtime, chat=CHAT, message_id="m1")
        await _dispatch(runtime, chat=OTHER, message_id="m2")

        effects = runtime.store.list_effects()
        assert len(effects) == 2
        assert len({effect.target_hash for effect in effects}) == 2
        assert runtime.store.count_send_budget_reservations() == 2
    finally:
        runtime.store.close()


@pytest.mark.asyncio
async def test_duplicate_admission_does_not_send_twice(runtime) -> None:
    first = _admit(runtime, message_id="m1")
    duplicate = _admit(runtime, message_id="m1")

    assert first.assignment is not None
    # A replay lands in the same thread and turn: no second assignment is invented.
    assert duplicate.assignment is not None
    assert duplicate.assignment.thread_id == first.assignment.thread_id
    assert duplicate.assignment.turn_id == first.assignment.turn_id
    await _dispatch(runtime, message_id="m1")
    await _dispatch(runtime, message_id="m1")

    effects = runtime.store.list_effects()
    assert len(effects) == 1
    assert runtime.transport.sent == ["answer"]


@pytest.mark.asyncio
async def test_correction_during_the_provider_await_cancels_the_stale_effect(runtime) -> None:
    result = _admit(runtime, message_id="m1")
    turn_id = str(result.assignment.turn_id)
    turn = runtime.store.get_turn(turn_id)
    runtime.store.enqueue_effect(
        effect_id="fx-stale",
        operation_key=f"send_text:{CHAT}:{turn_id}:1",
        payload={"text": "old"},
        target={"channel": "whatsapp", "chat_id": CHAT},
        turn_id=turn_id,
        turn_revision=1,
        now_ms=T0,
    )

    runtime.store.bump_turn_revision(
        turn_id, expected_revision=turn.revision, now_ms=T0 + 1_000, reason="correction"
    )
    cancelled = runtime.store.cancel_stale_effects(
        turn_id, current_revision=2, now_ms=T0 + 1_000, reason="correction"
    )

    assert cancelled == ("fx-stale",)
    assert runtime.store.effect_state("fx-stale") == "cancelled"
    assert runtime.transport.sent == []
    assert runtime.registry.turn_lookup(turn_id).revision == 2


@pytest.mark.asyncio
async def test_policy_change_before_the_effect_blocks_the_send(runtime) -> None:
    _admit(runtime, message_id="m1")
    runtime.reload_policy(_policy(mode="owner_only"))

    # The reply is produced for the sender, so the new policy decides its egress.
    with pytest.raises(EffectNotDeliveredError) as denied:
        await _dispatch(runtime, message_id="m1", principal="orderer@s.whatsapp.net")

    assert "blocked" in str(denied.value)
    effects = runtime.store.list_effects()
    assert len(effects) == 1
    assert effects[0].state == "blocked"
    assert runtime.transport.sent == []


@pytest.mark.asyncio
async def test_queue_overflow_blocks_with_a_reason(tmp_path: Path) -> None:
    runtime = _make_runtime(tmp_path, waiting_cap=0)
    try:
        result = _admit(runtime, message_id="m1")
        turn_id = str(result.assignment.turn_id)
        runtime.store.enqueue_effect(
            effect_id="fx-waiting",
            operation_key="waiting:1",
            payload={"text": "queued"},
            target={"channel": "whatsapp", "chat_id": CHAT},
            turn_id=turn_id,
            turn_revision=1,
            now_ms=T0,
        )

        with pytest.raises(EffectNotDeliveredError) as blocked:
            await _dispatch(runtime, message_id="m1")
        assert "blocked" in str(blocked.value)
        assert "queue_capacity" in str(blocked.value)

        states = sorted(effect.state for effect in runtime.store.list_effects())
        assert states == ["blocked", "queued"]
        assert runtime.transport.sent == []
    finally:
        runtime.store.close()


@pytest.mark.asyncio
async def test_unknown_send_status_is_resolved_by_a_late_receipt(runtime) -> None:
    from yeoman_gateway.processing.reconcile import (
        LocalEvidenceProbe,
        ProbeOutcome,
        reconcile_effect,
    )

    _admit(runtime, message_id="m1")
    runtime.transport.fail = True
    with pytest.raises(EffectNotDeliveredError) as unproven:
        await _dispatch(runtime, message_id="m1")
    assert "unknown" in str(unproven.value)

    effect = runtime.store.list_effects()[0]
    assert effect.state == "unknown"

    runtime.store.record_transport_receipt(
        effect.effect_id,
        channel="whatsapp",
        chat_id=CHAT,
        provider_message_id="3EB0",
        now_ms=T0 + 5_000,
    )
    result = await reconcile_effect(
        runtime.store, effect.effect_id, probe=LocalEvidenceProbe(runtime.store), now_ms=T0 + 6_000
    )

    assert result.outcome is ProbeOutcome.CONFIRMED
    assert runtime.store.effect_state(effect.effect_id) == "sent"


@pytest.mark.asyncio
async def test_restart_never_resends_a_proven_send(tmp_path: Path) -> None:
    """Crash matrix: what was proven sent before the restart is not sent a second time."""
    runtime = _make_runtime(tmp_path)
    _admit(runtime, message_id="m1")
    await _dispatch(runtime, message_id="m1")
    effect = runtime.store.list_effects()[0]
    assert effect.state == "sent"
    runtime.store.close()

    reopened = build_processing_store(runtime.config)
    assert reopened is not None
    try:
        assert reopened.recover_executing(now_ms=T0 + 10_000) == ()
        assert reopened.effect_state(effect.effect_id) == "sent"
        assert len(reopened.list_effects()) == 1
    finally:
        reopened.close()


def test_every_effect_has_a_parent_turn(runtime) -> None:
    """No orphan effects: each one links to the turn and revision that produced it."""
    _admit(runtime, message_id="m1")

    effects = runtime.store.list_effects()
    for effect in effects:
        assert effect.turn_id, "effect without a parent turn"
        assert effect.turn_revision >= 1
        assert runtime.store.get_turn(effect.turn_id) is not None


@pytest.mark.asyncio
async def test_soft_thread_limit_is_measured_by_default(tmp_path: Path) -> None:
    """A chatty thread keeps working: the limit is measured, not silently enforced."""
    runtime = _make_runtime(tmp_path, chats=(CHAT,), ambient=(CHAT,))
    try:
        _admit(runtime, message_id="m1")
        for index in range(3):
            await _dispatch(runtime, message_id="m1", content=f"answer {index}")

        sent = [effect for effect in runtime.store.list_effects() if effect.state == "sent"]
        assert len(sent) == 3
        assert len({effect.turn_id for effect in sent}) == 1  # one chatty thread
    finally:
        runtime.store.close()


@pytest.mark.asyncio
async def test_soft_thread_limit_refuses_only_when_explicitly_enabled(tmp_path: Path) -> None:
    runtime = _make_runtime(tmp_path / "strict", chats=(CHAT,), soft_enforce=True)
    try:
        _admit(runtime, message_id="m1")
        await _dispatch(runtime, message_id="m1", content="one")
        await _dispatch(runtime, message_id="m1", content="two")

        with pytest.raises(EffectNotDeliveredError) as limited:
            await _dispatch(runtime, message_id="m1", content="three")

        assert "thread soft limit" in str(limited.value)
        assert runtime.transport.sent == ["one", "two"]
    finally:
        runtime.store.close()


def test_the_session_guard_keeps_stores_away_from_the_runtime_tree() -> None:
    """The harness bug that motivated tests/conftest.py cannot come back silently."""
    from pathlib import Path

    from yeoman_shared.utils.helpers import get_data_path

    resolved = get_data_path().resolve()
    assert "yeoman-home" in str(resolved), resolved
    assert resolved != (Path.home() / ".yeoman" / "data").resolve()


@pytest.mark.asyncio
async def test_f02_a_final_reply_keeps_its_frozen_turn(runtime, monkeypatch) -> None:
    """Review F02: the reply must not be adopted by a thread that started meanwhile.

    The generation scope closes before the orchestrator dispatches the final reply, so the
    router used to fall back to the chat's *active* turn - which may already be thread B.
    """
    from yeoman_gateway.processing.models import TurnBinding

    first = _admit(runtime, message_id="m1")
    turn_a = runtime.store.get_turn(str(first.assignment.turn_id))
    assert turn_a is not None

    # A second thread opens in the same chat while m1 is still being answered.
    thread_b = runtime.store.open_thread(
        channel="whatsapp",
        chat_id=CHAT,
        root_principal="orderer@s.whatsapp.net",
        kind="dm",
        trigger_event_id="m2",
        now_ms=T0 + 1,
    )
    turn_b_id = runtime.store.open_turn(
        thread_id=thread_b,
        principal="orderer@s.whatsapp.net",
        trigger_event_id="m2",
        now_ms=T0 + 1,
    )
    turn_b = runtime.store.get_turn(turn_b_id)
    assert turn_b is not None and turn_b.turn_id != turn_a.turn_id

    # The chat's active turn is now B, which is exactly what the heuristic would pick.
    # The router captured its provider at construction, so patch the provider itself.
    monkeypatch.setattr(
        runtime.router,
        "_turn_provider",
        lambda channel, chat_id: turn_b.to_ref(channel="whatsapp", chat_id=CHAT),
    )

    # What the responder wrapper records while the generation for m1 is frozen.
    runtime.router.remember_turn_for_source(
        "m1", TurnBinding(turn=turn_a, trace_id=turn_a.turn_id, generation_id="gen-a")
    )
    await _dispatch(runtime, message_id="m1")

    effects = [effect for effect in runtime.store.list_effects() if effect.turn_id]
    assert len(effects) == 1
    assert effects[0].turn_id == turn_a.turn_id, (
        "the answer was attributed to the newer turn instead of its own"
    )
    assert effects[0].turn_revision == turn_a.revision


@pytest.mark.asyncio
async def test_reply_action_silence_withdraws_the_answer(tmp_path: Path) -> None:
    """Plan 07 / Aufgabe 4: silence produces no turn, no effect and no typing indicator."""
    runtime = _make_runtime(tmp_path / "silence", chats=(CHAT,))
    try:
        runtime.config.processing.reply_actions = {f"whatsapp:{CHAT}": "silence"}

        result = _admit(runtime, message_id="m1")

        assert result.outcome.value == "observe", "the answer must be withdrawn"
        assert result.assignment is not None
        assert result.assignment.turn_id is None, "silence must not open a turn"
        assert runtime.store.list_effects() == ()
        assert runtime.transport.sent == []
    finally:
        runtime.store.close()


@pytest.mark.asyncio
async def test_reply_action_react_answers_with_one_reaction(tmp_path: Path) -> None:
    """Plan 07 / Aufgabe 4: `react` replaces the answer - no turn, no typing, one emoji.

    The emoji is chosen by the small chooser call, validated against the owner's
    vocabulary, and sent as an effect with the message as its own lineage.
    """
    from yeoman_gateway.processing.reaction_action import ReactionAction

    runtime = _make_runtime(tmp_path / "react", chats=(CHAT,))
    try:
        runtime.config.processing.reply_actions = {f"whatsapp:{CHAT}": "react"}
        runtime.config.processing.reaction_emojis = ["🤙", "🥱"]
        content = "Arvid, was hältst du davon?"

        result = _admit(runtime, message_id="m1", content=content)
        assert result.outcome.value == "observe", "react must not open an answer turn"
        assert result.reply_action == "react"
        assert result.react is True, "an answerable message becomes a reaction"
        assert result.assignment is not None and result.assignment.turn_id is None
        assert runtime.gate.admit_reply(_event(message_id="m1", content=content)) is False

        class _Chooser:
            async def choose(self, text: str, *, allowed: Sequence[str]) -> str | None:
                assert list(allowed) == ["🤙", "🥱"], "the configured vocabulary is used"
                assert text == content, "the chooser sees the message it reacts to"
                return "🤙"

        action = ReactionAction(
            chooser=_Chooser(), router=runtime.router, allowed_emojis=("🤙", "🥱")
        )
        emoji = await action(
            channel="whatsapp",
            chat_id=CHAT,
            message_id="m1",
            text=content,
            principal="orderer@s.whatsapp.net",
        )

        assert emoji == "🤙"
        assert runtime.transport.sent == ["🤙"], "the reaction reaches the transport"
        effects = runtime.store.list_effects()
        assert len(effects) == 1
        assert effects[0].operation_key.startswith("reaction:whatsapp:")
        assert "m1" in effects[0].operation_key, "the reaction carries its source"
    finally:
        runtime.store.close()


@pytest.mark.asyncio
async def test_reply_action_react_never_acknowledges_an_observed_message(tmp_path: Path) -> None:
    """A reaction replaces an *answer*, not every message (spec: no acknowledgement emoji)."""
    runtime = _make_runtime(tmp_path / "react-observed", chats=(CHAT,), ambient=(CHAT,))
    try:
        runtime.config.processing.reply_actions = {f"whatsapp:{CHAT}": "react"}
        runtime.reload_policy(_policy(when_to_reply="mention_only"))

        observed = _admit(runtime, message_id="m1", content="nur so ein Gedanke", mentioned=False)

        assert observed.outcome.value == "observe"
        assert observed.react is False, "an unaddressed observation must not be acknowledged"
    finally:
        runtime.store.close()


@pytest.mark.asyncio
async def test_reply_action_react_sends_nothing_when_no_emoji_fits(tmp_path: Path) -> None:
    from yeoman_gateway.processing.reaction_action import ReactionAction

    runtime = _make_runtime(tmp_path / "react-silent", chats=(CHAT,))
    try:

        class _Chooser:
            async def choose(self, text: str, *, allowed: Sequence[str]) -> str | None:
                return None

        action = ReactionAction(
            chooser=_Chooser(), router=runtime.router, allowed_emojis=("🤙",)
        )
        emoji = await action(
            channel="whatsapp",
            chat_id=CHAT,
            message_id="m1",
            text="irgendwas",
            principal="orderer@s.whatsapp.net",
        )

        assert emoji is None
        assert runtime.transport.sent == []
        assert runtime.store.list_effects() == ()
    finally:
        runtime.store.close()


@pytest.mark.asyncio
async def test_a_withdrawn_answer_survives_the_classic_admission(tmp_path: Path) -> None:
    """The veto must hold where the typing indicator and the generation actually start.

    The fast gate is not the last word: the classic pipeline asks `admit_reply` before it
    shows typing or calls the provider. A withdrawn answer that still passes admission
    would be answered anyway - and `react` needs the same guarantee.
    """
    runtime = _make_runtime(tmp_path / "silence-admission", chats=(CHAT,))
    try:
        runtime.config.processing.reply_actions = {f"whatsapp:{CHAT}": "silence"}
        event = _event(message_id="m1", mentioned=True, content="Arvid, fasse das zusammen")

        result = _admit(runtime, message_id="m1", content="Arvid, fasse das zusammen")

        assert result.outcome.value == "observe"
        assert runtime.gate.admit_reply(event) is False, (
            "the classic pipeline would start typing and generate an answer"
        )
    finally:
        runtime.store.close()


@pytest.mark.asyncio
async def test_an_ambient_answer_closes_its_turn_after_sending(tmp_path: Path) -> None:
    """Spec: an ambient answer is short-lived - it must not leave open durable work.

    The brake and the judge run first (see the ambient brake tests); this one starts after
    the verdict, where the channel opens the turn with `reconcile_reply`.
    """
    runtime = _make_runtime(tmp_path / "ambient", chats=(CHAT,), ambient=(CHAT,))
    try:
        event = _event(message_id="m1", content="nur so ein Gedanke", mentioned=False)
        result = _admit(
            runtime, message_id="m1", content="nur so ein Gedanke", mentioned=False
        )
        assert result.ambient_candidate is True, "the brake let this one through"
        assert result.assignment is not None and result.assignment.turn_id is None, (
            "the turn is opened only after the judge's yes"
        )

        assignment = runtime.gate.reconcile_reply(event)
        runtime.gate.note_ambient_answer("m1")
        assert assignment is not None and assignment.turn_id, "ambient needs its own lineage"
        thread = runtime.store.get_thread(str(assignment.thread_id))
        assert thread is not None and thread.kind == "ambient"

        await _dispatch(runtime, message_id="m1")

        turn = runtime.store.get_turn(str(assignment.turn_id))
        assert turn is not None and turn.state == "closed", (
            "the ambient turn stayed open after its answer was sent"
        )
        assert runtime.transport.sent == ["answer"]
    finally:
        runtime.store.close()


def test_the_observation_line_shows_the_reasoning_without_content(runtime) -> None:
    """Criterion 12: classification, candidates, signal with evidence, action, lineage.

    The line exists so an operator can see *why* a message was attached or not; it must
    never carry message text.
    """
    from loguru import logger

    secret = "GEHEIMER-INHALT-4711"
    records: list[str] = []
    sink = logger.add(lambda message: records.append(message.record["message"]), level="INFO")
    try:
        _admit(runtime, message_id="m1", content=f"Fasse den Mietvertrag zusammen. {secret}")
        _admit(
            runtime,
            message_id="m2",
            content="Zum Mietvertrag: ergänze bitte die Kündigungsfrist.",
        )
    finally:
        logger.remove(sink)

    lines = [record for record in records if "routing_decision" in record]
    assert len(lines) >= 2, f"expected one observation line per message: {records}"
    continuation = [line for line in lines if "continuity=explicit_callback" in line]
    assert continuation, f"the call-back signal is not visible: {lines}"
    assert "evidence=" in continuation[0] and "evidence=-" not in continuation[0], (
        "the proving source ids must be visible"
    )
    assert all(secret not in line for line in lines), "the observation line leaked content"


def test_criterion_16_permitted_senders_answer_ambient_and_others_never_do(tmp_path: Path) -> None:
    """Routing spec, criterion 16.

    Under ``allowed_senders`` and ``owner_only`` a permitted sender's unmarked message in
    a group may be answered like under ``all`` - no address needed. A sender who is not
    permitted is answered neither because of a mention nor because of a continuity signal.

    "May be answered" is still a two-step decision: the gate marks it as an ambient
    candidate (brake passed, permission granted) and the judge decides whether it speaks.
    An unpermitted sender's message never even becomes a candidate.
    """
    permitted = "orderer@s.whatsapp.net"
    stranger = "stranger@s.whatsapp.net"

    # allowed_senders: the permitted sender's unmarked message is answerable.
    runtime = _make_runtime(tmp_path / "allowed", chats=(CHAT,), ambient=(CHAT,))
    try:
        runtime.reload_policy(
            _policy(when_to_reply="allowed_senders", senders=(permitted,))
        )
        allowed = _admit(runtime, message_id="m1", content="nur so ein Gedanke", mentioned=False)
        assert allowed.ambient_candidate is True, "a permitted sender may be answered"
        assert allowed.outcome.value == "observe", "the judge still has to agree"

        # The same message from someone else is admitted as context at most - never
        # answered. Admission (whoCanTalk) and reply permission (whenToReply) are
        # separate dimensions in the spec, so this one asserts the reply side.
        denied = _admit(
            runtime,
            message_id="m2",
            content="@Arvid hilf mir",
            mentioned=True,
            sender=stranger,
        )
        assert denied.ambient_candidate is False, "an unpermitted sender is never answered"
        assert denied.outcome.value == "observe"

        # A continuity signal does not make them answerable either: the gate journaled m2
        # above, so this message would be a textbook continuation - and still no answer.
        continued = _admit(
            runtime,
            message_id="m3",
            content="Zum Mietvertrag: ergänze bitte die Frist.",
            mentioned=False,
            sender=stranger,
        )
        assert continued.outcome.value == "observe", "continuity grants no permission"
    finally:
        runtime.store.close()

    # whoCanTalk refuses the stranger's message entirely, mention or not: that is the
    # admission side, and it stays a hard gate.
    strict = _make_runtime(tmp_path / "strict", chats=(CHAT,), ambient=(CHAT,))
    try:
        strict.reload_policy(_policy(mode="allowlist", when_to_reply="all"))
        blocked = _admit(
            strict, message_id="m1", content="@Arvid hilf", mentioned=True, sender=stranger
        )
        assert blocked.outcome.value == "deny", "an unlisted sender may not talk at all"
    finally:
        strict.store.close()

    # owner_only: the owner is treated exactly like the permitted sender above.
    owner_runtime = _make_runtime(tmp_path / "owner", chats=(CHAT,), ambient=(CHAT,))
    try:
        owner_runtime.reload_policy(_policy(when_to_reply="owner_only"))
        owner = _admit(
            owner_runtime,
            message_id="m1",
            content="nur so",
            mentioned=False,
            sender="owner@s.whatsapp.net",
        )
        assert owner.ambient_candidate is True, "the owner may be answered without a mention"
    finally:
        owner_runtime.store.close()

    # mention_only keeps its DM exception: an unmarked DM is answerable, a group is not.
    dm_runtime = _make_runtime(tmp_path / "dm", chats=(CHAT,), ambient=(CHAT,))
    try:
        dm_runtime.reload_policy(_policy(when_to_reply="mention_only"))
        group = _admit(dm_runtime, message_id="m1", content="nur so", mentioned=False)
        assert group.outcome.value == "observe", "an unmarked group message stays unanswered"
    finally:
        dm_runtime.store.close()


@pytest.mark.asyncio
async def test_criterion_8_a_reaction_has_its_own_lineage(runtime) -> None:
    """Routing spec, criterion 8.

    A reaction belongs to the message it reacts to: it carries that message's turn and
    never adopts a different, newer order from the same chat.
    """
    from yeoman_gateway.core.intents import SendReactionIntent

    first = _admit(runtime, message_id="m1")
    second = _admit(runtime, message_id="m2", content="Wie wird morgen das Wetter?")
    assert first.assignment is not None and second.assignment is not None
    assert second.assignment.turn_id != first.assignment.turn_id, "two turns exist"

    submitted = await runtime.router.submit_reaction(
        SendReactionIntent(channel="whatsapp", chat_id=CHAT, message_id="m1", emoji="👍"),
        principal="orderer@s.whatsapp.net",
    )
    assert submitted is True

    effects = runtime.store.list_effects()
    assert len(effects) == 1
    effect = effects[0]
    assert effect.turn_id == first.assignment.turn_id, (
        f"the reaction took the wrong order's turn: {effect.turn_id!r}"
    )
    assert effect.operation_key.startswith("reaction:whatsapp:"), effect.operation_key
    assert "m1" in effect.operation_key, "the triggering message is the provenance"


@pytest.mark.asyncio
async def test_an_unapproved_emoji_never_becomes_an_effect(runtime) -> None:
    """One decision point for every reaction, whoever produced it.

    The owner approves the vocabulary; a model-chosen emoji outside it is dropped here,
    and the caller's direct-publish fallback must not smuggle it onto the wire either.
    """
    from yeoman_gateway.core.intents import SendReactionIntent

    runtime.config.processing.reaction_emojis = ["👍", "🥱"]
    approved = SendReactionIntent(
        channel="whatsapp", chat_id=CHAT, message_id="m1", emoji="👍"
    )

    assert await runtime.router.submit_reaction(approved, principal="orderer@s.whatsapp.net") is True
    assert runtime.transport.sent == ["👍"], "the approved emoji reaches the transport"

    unapproved = SendReactionIntent(
        channel="whatsapp", chat_id=CHAT, message_id="m1", emoji="🤖"
    )
    # True = "handled": nothing was queued, and nothing may be published directly instead.
    assert await runtime.router.submit_reaction(
        unapproved, principal="orderer@s.whatsapp.net"
    ) is True
    assert runtime.transport.sent == ["👍"], "an unapproved emoji must send nothing"
    assert len(runtime.store.list_effects()) == 1, "and must leave no effect behind"


@pytest.mark.asyncio
async def test_a_gateway_decision_is_not_the_models_taste(runtime) -> None:
    """Internal confirmations keep working when the owner narrows the list."""
    from yeoman_gateway.core.intents import SendReactionIntent
    from yeoman_shared.reactions import SYSTEM_ORIGIN

    runtime.config.processing.reaction_emojis = []
    blocked = SendReactionIntent(
        channel="whatsapp",
        chat_id=CHAT,
        message_id="m1",
        emoji="🚫",
        origin=SYSTEM_ORIGIN,
    )

    assert await runtime.router.submit_reaction(blocked, principal="orderer@s.whatsapp.net") is True
    assert runtime.transport.sent == ["🚫"]


# the ambient brake ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_ambient_brake_needs_both_time_and_messages(tmp_path: Path) -> None:
    """A precondition, not a schedule: nothing is judged before both thresholds are met."""
    runtime = _make_runtime(
        tmp_path / "brake", chats=(CHAT,), ambient=(CHAT,), ambient_brake=(300, 6)
    )
    try:
        first = _admit(runtime, message_id="m1", content="nur so ein Gedanke", mentioned=False)
        assert first.ambient_candidate is False, "one quiet message is not a conversation"
        assert first.outcome.value == "observe"
        assert runtime.store.list_effects() == ()

        for index in range(2, 7):
            admitted = _admit(
                runtime, message_id=f"m{index}", content=f"Gedanke {index}", mentioned=False
            )
        # The sixth message in the window is the first one the judge may look at.
        assert admitted.ambient_candidate is True, "six messages are a conversation"
        assert admitted.outcome.value == "observe", "the judge still decides"
    finally:
        runtime.store.close()


@pytest.mark.asyncio
async def test_the_ambient_brake_restarts_its_window_after_every_verdict(tmp_path: Path) -> None:
    """An answer - and a decline - both restart the window, so the judge stays rare."""
    runtime = _make_runtime(
        tmp_path / "brake-window", chats=(CHAT,), ambient=(CHAT,), ambient_brake=(300, 1)
    )
    try:
        granted = _admit(runtime, message_id="m1", content="Frage an die Runde", mentioned=False)
        assert granted.ambient_candidate is True
        runtime.gate.note_ambient_answer("m1")

        after_answer = _admit(runtime, message_id="m2", content="noch ein Gedanke", mentioned=False)
        assert after_answer.ambient_candidate is False, "the answered window starts over"

        runtime.gate.note_ambient_declined("m2")
        declined = _admit(runtime, message_id="m3", content="und noch einer", mentioned=False)
        assert declined.ambient_candidate is False, "a decline starts the window over too"
    finally:
        runtime.store.close()


@pytest.mark.asyncio
async def test_a_declined_ambient_message_can_never_be_answered(tmp_path: Path) -> None:
    """The judge's no is final: the classic pipeline must not answer it anyway."""
    runtime = _make_runtime(tmp_path / "ambient-declined", chats=(CHAT,), ambient=(CHAT,))
    try:
        event = _event(message_id="m1", content="nur so ein Gedanke", mentioned=False)
        verdict = _admit(runtime, message_id="m1", content="nur so ein Gedanke", mentioned=False)
        assert verdict.ambient_candidate is True

        runtime.gate.note_ambient_declined("m1")

        assert runtime.gate.admit_reply(event) is False, (
            "a declined ambient message must not reach typing or the provider"
        )
        assert runtime.gate.reconcile_reply(event) is not None, (
            "a granted message may still be opened by the channel"
        )
    finally:
        runtime.store.close()


@pytest.mark.asyncio
async def test_an_addressed_message_ignores_the_ambient_brake(tmp_path: Path) -> None:
    """The brake is about unaddressed traffic; an order is answered immediately."""
    runtime = _make_runtime(
        tmp_path / "brake-addressed", chats=(CHAT,), ambient=(CHAT,), ambient_brake=(600, 99)
    )
    try:
        result = _admit(runtime, message_id="m1", content="Arvid, hilf mir", mentioned=True)

        assert result.outcome.value == "react", "an addressed order needs no brake"
        assert result.ambient_candidate is False
        assert result.assignment is not None and result.assignment.turn_id, "it gets its turn"
    finally:
        runtime.store.close()
