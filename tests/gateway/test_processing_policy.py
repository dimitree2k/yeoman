"""Plan 02 / R02, R05, R08: policy snapshots, fast gate and final effect authorization."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest
from yeoman_gateway.adapters.policy_engine import EnginePolicyAdapter
from yeoman_gateway.bus.queue import MessageBus
from yeoman_gateway.channels.whatsapp import InboundEvent as WhatsAppInboundEvent
from yeoman_gateway.channels.whatsapp import WhatsAppChannel
from yeoman_gateway.core.models import InboundEvent, PolicyDecision
from yeoman_gateway.policy.engine import PolicyEngine
from yeoman_gateway.policy.loader import save_policy
from yeoman_gateway.policy.schema import PolicyConfig
from yeoman_gateway.processing.ambient_judge import AmbientVerdict
from yeoman_gateway.processing.effects import EffectGateway
from yeoman_gateway.processing.models import (
    EffectEnvelope,
    EffectTarget,
    PolicySnapshot,
    TextPayload,
    TurnRef,
)
from yeoman_gateway.processing.policy import (
    AdapterSnapshotProvider,
    FastGateOutcome,
    IngestGate,
    IngestRequest,
    PolicyCapabilityResolver,
    SnapshotEffectAuthorizer,
    effect_guard,
)
from yeoman_gateway.processing.store import ProcessingStore
from yeoman_shared.config.schema import ProcessingConfig, WhatsAppConfig

TARGET = EffectTarget(channel="whatsapp", chat_id="chat@g.us")


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------


def _policy(*, when_to_reply: str = "all", who_can_talk: str = "everyone") -> PolicyConfig:
    return PolicyConfig.model_validate(
        {
            "defaults": {"allowedTools": {"mode": "allowlist", "tools": ["message"]}},
            "owners": {"whatsapp": ["owner@s.whatsapp.net"]},
            "channels": {
                "whatsapp": {
                    "default": {
                        "whoCanTalk": {"mode": who_can_talk},
                        "whenToReply": {"mode": when_to_reply},
                    }
                }
            },
        }
    )


def _event(*, mentioned_bot: bool = True) -> InboundEvent:
    return InboundEvent(
        channel="whatsapp",
        chat_id="chat@g.us",
        sender_id="sender@s.whatsapp.net",
        content="hello",
        message_id="m1",
        is_group=True,
        mentioned_bot=mentioned_bot,
    )


def _adapter(tmp_path: Path, *, interval: float = 60.0) -> tuple[EnginePolicyAdapter, Path]:
    path = tmp_path / "policy.json"
    policy = _policy()
    save_policy(policy, path)
    engine = PolicyEngine(policy, workspace=tmp_path, apply_channels={"whatsapp"})
    adapter = EnginePolicyAdapter(
        engine=engine,
        known_tools={"message", "send_voice", "delete_message"},
        policy_path=path,
        reload_on_change=True,
        reload_check_interval_seconds=interval,
        workspace=tmp_path,
    )
    return adapter, path


def _replace_json(path: Path, data: dict[str, Any]) -> None:
    previous = path.stat()
    path.write_text(json.dumps(data), encoding="utf-8")
    mtime_ns = max(time.time_ns(), previous.st_mtime_ns + 1)
    os.utime(path, ns=(previous.st_atime_ns, mtime_ns))


def _force_reload_check(adapter: EnginePolicyAdapter) -> None:
    adapter._last_reload_check = 0.0


def _no_debounce_config() -> WhatsAppConfig:
    """Transport debounce stays untouched; the test simply does not wait for it."""
    return WhatsAppConfig(debounce_ms=0, debounce_media_ms=0)


def _codec_config(chats: tuple[str, ...] = ("whatsapp:chat@g.us",)) -> ProcessingConfig:
    return ProcessingConfig.model_validate({"enabled": True, "chats": list(chats)})


class _StaticSnapshots:
    def __init__(self, snapshot: PolicySnapshot | None = None) -> None:
        self._snapshot = snapshot or PolicySnapshot(
            version="policy-v1", policy_hash="hash-v1", loaded_ms=0
        )

    def snapshot(self) -> PolicySnapshot:
        return self._snapshot


class _Clock:
    def __init__(self, value: int = 0) -> None:
        self.value = value

    def __call__(self) -> int:
        return self.value


class _Executor:
    def __init__(self, result: str = "sent") -> None:
        self.result = result
        self.calls: list[str] = []

    async def execute(self, envelope):
        from yeoman_gateway.processing.models import EffectReceipt

        self.calls.append(envelope.effect_id)
        return EffectReceipt(effect_id=envelope.effect_id, state=self.result)


class _AllowAll:
    def resolve(self, *, principal: str, target: EffectTarget, capability: str):
        return True, "allow"


def _request(**overrides: Any) -> IngestRequest:
    data: dict[str, Any] = {
        "event_key": "wa:chat@g.us:m1",
        "event_id": "evt-1",
        "trace_id": "tr-wa-1",
        "event": _event(),
    }
    data.update(overrides)
    return IngestRequest(**data)


def _envelope(**overrides: Any) -> EffectEnvelope:
    data: dict[str, Any] = {
        "effect_id": "fx1",
        "operation_key": "turn1:send1",
        "payload": TextPayload(text="hi"),
        "target": TARGET,
        "trace_id": "tr1",
        "turn_id": "",
        "turn_revision": 1,
        "principal": "owner@s.whatsapp.net",
        "capability": "send_text",
    }
    data.update(overrides)
    return EffectEnvelope(**data)


def _wa_event(**overrides: Any) -> WhatsAppInboundEvent:
    data: dict[str, Any] = {
        "message_id": "m1",
        "chat_jid": "chat@g.us",
        "participant_jid": "sender@lid",
        "sender_id": "sender",
        "sender_phone_jid": "4915111@s.whatsapp.net",
        "is_group": True,
        "text": "hi",
        "timestamp": 1_700_000_000,
        "mentioned_jids": [],
        "mentioned_bot": True,
        "reply_to_bot": False,
        "reply_to_message_id": None,
        "reply_to_participant": None,
        "reply_to_text": None,
        "media_kind": "image",
        "media_type": "image/jpeg",
        "media_file_name": "photo.jpg",
        "media_path": "/tmp/photo.jpg",
        "media_bytes": 10,
        "media_description": None,
        "voice_transcript": None,
    }
    data.update(overrides)
    return WhatsAppInboundEvent(**data)


# --------------------------------------------------------------------------------------
# effect_guard
# --------------------------------------------------------------------------------------


def test_stale_turn_cannot_send_with_valid_policy():
    assert effect_guard(policy_healthy=True, permitted=True,
                        revision_matches=False, unexpired=True) == "superseded"


def test_effect_guard_covers_every_blocking_reason():
    assert effect_guard(
        policy_healthy=False, permitted=True, revision_matches=True, unexpired=True
    ) == "policy_unhealthy"
    assert effect_guard(
        policy_healthy=True, permitted=True, revision_matches=True, unexpired=False
    ) == "expired"
    assert effect_guard(
        policy_healthy=True, permitted=False, revision_matches=True, unexpired=True
    ) == "permission_denied"
    assert effect_guard(
        policy_healthy=True, permitted=True, revision_matches=True, unexpired=True
    ) == "allow"


# --------------------------------------------------------------------------------------
# snapshots
# --------------------------------------------------------------------------------------


def test_snapshot_names_the_loaded_engine_not_the_file(tmp_path: Path) -> None:
    adapter, path = _adapter(tmp_path)
    loaded = adapter.policy_snapshot()
    assert loaded.healthy is True
    assert loaded.policy_hash
    assert loaded.policy is not None

    # Z lands on disk while engine X stays in memory: the snapshot must still name X.
    _replace_json(path, _policy(when_to_reply="off").model_dump(by_alias=True, exclude_none=True))
    stale = adapter.policy_snapshot()

    assert stale.version == loaded.version
    assert stale.policy_hash == loaded.policy_hash
    assert stale.policy.model_dump(mode="json") == loaded.policy.model_dump(mode="json")


def test_reloaded_policy_is_the_one_the_decision_names(tmp_path: Path) -> None:
    adapter, path = _adapter(tmp_path)
    before = adapter.policy_snapshot()
    store = ProcessingStore(tmp_path / "p.db")
    gate = IngestGate(
        config=_codec_config(),
        store=store,
        snapshots=AdapterSnapshotProvider(adapter),
        evaluate=lambda request: adapter.evaluate(request.event),
    )

    _replace_json(
        path, _policy(who_can_talk="owner_only").model_dump(by_alias=True, exclude_none=True)
    )
    _force_reload_check(adapter)
    adapter.evaluate(_event())  # the engine now really is Y
    after = adapter.policy_snapshot()
    assert after.version != before.version
    assert after.policy_hash != before.policy_hash

    result = gate.admit(_request())
    assert result is not None
    assert result.outcome is FastGateOutcome.DENY  # Y admits owners only
    assert result.decision is not None
    assert result.decision.policy_version == after.version
    assert result.decision.policy_hash == after.policy_hash
    store.close()


def test_known_reload_failure_blocks_gate_and_authorizer(tmp_path: Path) -> None:
    adapter, path = _adapter(tmp_path)
    invalid = _policy().model_dump(by_alias=True, exclude_none=True)
    invalid["unknownField"] = "reject me"
    _replace_json(path, invalid)
    _force_reload_check(adapter)
    adapter.evaluate(_event())

    snapshot = adapter.policy_snapshot()
    assert snapshot.healthy is False
    assert snapshot.error

    store = ProcessingStore(tmp_path / "p.db")
    gate = IngestGate(
        config=_codec_config(),
        store=store,
        snapshots=AdapterSnapshotProvider(adapter),
        evaluate=lambda request: adapter.evaluate(request.event),
    )
    result = gate.admit(_request())
    assert result is not None
    assert result.outcome is FastGateOutcome.DENY
    assert result.reason == "policy_unhealthy"

    authorizer = SnapshotEffectAuthorizer(
        snapshots=AdapterSnapshotProvider(adapter), capabilities=_AllowAll()
    )
    record = authorizer.check(_envelope(), None)
    assert record.outcome == "deny"
    assert record.reason == "policy_unhealthy"
    store.close()


# --------------------------------------------------------------------------------------
# fast gate
# --------------------------------------------------------------------------------------


def _gate(store: ProcessingStore, decision: PolicyDecision) -> IngestGate:
    return IngestGate(
        config=_codec_config(),
        store=store,
        snapshots=_StaticSnapshots(),
        evaluate=lambda request: decision,
    )


def _deny() -> PolicyDecision:
    return PolicyDecision(
        accept_message=False,
        should_respond=False,
        allowed_tools=frozenset(),
        reason="blocked_sender",
    )


def test_denied_sender_is_journaled_but_never_admitted(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "p.db")
    enrich_calls: list[str] = []

    def _evaluate(request: IngestRequest) -> PolicyDecision:
        enrich_calls.append(request.event_key)
        return _deny()

    gate = IngestGate(
        config=_codec_config(),
        store=store,
        snapshots=_StaticSnapshots(),
        evaluate=_evaluate,
    )
    result = gate.admit(_request())

    assert result is not None
    assert result.outcome is FastGateOutcome.DENY
    assert result.reason == "permission_denied"
    assert result.journaled_event_id == "evt-1"
    assert result.decision is not None
    assert store.get_decision(result.decision.decision_id) is not None
    assert store.count_events() == 1
    # Journaling itself is not enrichment: nothing was transcribed or described.
    assert enrich_calls == ["wa:chat@g.us:m1"]
    store.close()


def test_ambient_is_observed_without_a_reactive_turn(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "p.db")
    gate = _gate(
        store,
        PolicyDecision(
            accept_message=True,
            should_respond=False,
            allowed_tools=frozenset(),
            reason="when_to_reply:mention_only_group",
        ),
    )
    result = gate.admit(_request())

    assert result is not None
    assert result.outcome is FastGateOutcome.OBSERVE
    assert result.proceed is True
    assert result.decision is not None and result.decision.outcome == "allow"
    store.close()


def test_mention_creates_a_reactive_turn(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "p.db")
    gate = _gate(
        store,
        PolicyDecision(
            accept_message=True,
            should_respond=True,
            allowed_tools=frozenset({"message"}),
            reason="allow",
        ),
    )
    result = gate.admit(_request())

    assert result is not None
    assert result.outcome is FastGateOutcome.REACT
    store.close()


def test_unmanaged_chat_is_not_touched_by_the_new_mode(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "p.db")
    gate = IngestGate(
        config=_codec_config(chats=("whatsapp:other@g.us",)),
        store=store,
        snapshots=_StaticSnapshots(),
        evaluate=lambda request: _deny(),
    )
    assert gate.admit(_request()) is None
    assert store.count_events() == 0
    store.close()


@pytest.mark.asyncio
async def test_blocked_sender_never_reaches_enrichment_or_the_bus(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "p.db")
    bus = MessageBus()
    channel = WhatsAppChannel(_no_debounce_config(), bus)
    channel.set_processing_gate(_gate(store, _deny()))

    enriched: list[str] = []
    published: list[str] = []

    async def _fake_enrich(event: WhatsAppInboundEvent) -> WhatsAppInboundEvent:
        enriched.append(event.message_id)
        return event

    async def _fake_publish(event: WhatsAppInboundEvent) -> None:
        published.append(event.message_id)

    channel._enrich_media_event = _fake_enrich  # type: ignore[method-assign]
    channel._publish_event = _fake_publish  # type: ignore[method-assign]

    await channel._ingest_inbound_event(_wa_event())

    assert enriched == []
    assert published == []
    assert store.count_events() == 1
    stored = store.get_event("m1")
    assert stored is not None
    assert stored.chat_id == "chat@g.us"
    assert stored.principal == "4915111"
    store.close()


@pytest.mark.asyncio
async def test_allowed_media_still_reaches_enrichment(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "p.db")
    bus = MessageBus()
    channel = WhatsAppChannel(_no_debounce_config(), bus)
    channel.set_processing_gate(
        _gate(
            store,
            PolicyDecision(
                accept_message=True,
                should_respond=True,
                allowed_tools=frozenset({"message"}),
                reason="allow",
            ),
        )
    )

    enriched: list[str] = []
    published: list[str] = []

    async def _fake_enrich(event: WhatsAppInboundEvent) -> WhatsAppInboundEvent:
        enriched.append(event.message_id)
        return event

    async def _fake_publish(event: WhatsAppInboundEvent) -> None:
        published.append(event.message_id)

    channel._enrich_media_event = _fake_enrich  # type: ignore[method-assign]
    channel._publish_event = _fake_publish  # type: ignore[method-assign]

    await channel._ingest_inbound_event(_wa_event())

    assert enriched == ["m1"]
    assert published == ["m1"]
    assert store.count_events() == 1
    store.close()


# --------------------------------------------------------------------------------------
# the judge's verdict and the configured reply action (owner decision, option E)
# --------------------------------------------------------------------------------------


class _Judge:
    """A judge that always returns the same verdict and records being asked."""

    def __init__(self, verdict: AmbientVerdict) -> None:
        self.verdict = verdict
        self.asked: list[str] = []

    async def decide(self, text: str) -> AmbientVerdict:
        self.asked.append(text)
        return self.verdict


class _Reaction:
    """Records what the channel sent, through both reaction entry points."""

    def __init__(self, chosen: str | None = "🤙") -> None:
        self.chosen = chosen
        self.sent: list[str] = []

    async def send(self, *, emoji: str, **_: Any) -> str | None:
        self.sent.append(emoji)
        return emoji

    async def __call__(self, **_: Any) -> str | None:
        if self.chosen is None:
            return None
        self.sent.append(self.chosen)
        return self.chosen


def _ambient_gate(store: ProcessingStore, *, action: str = "answer") -> IngestGate:
    """One chat released for ambient answers, capped by the configured reply action."""
    from yeoman_gateway.processing.threads import ThreadRegistry

    config = ProcessingConfig.model_validate(
        {
            "enabled": True,
            "chats": ["whatsapp:chat@g.us"],
            "ambient_chats": ["whatsapp:chat@g.us"],
            "reply_actions": {"whatsapp:chat@g.us": action},
            "ambient": {"min_seconds_between_answers": 0, "min_messages_since_answer": 0},
        }
    )
    return IngestGate(
        config=config,
        store=store,
        snapshots=_StaticSnapshots(),
        threads=ThreadRegistry(store=store, config=config),
        evaluate=lambda request: PolicyDecision(
            accept_message=True,
            should_respond=True,
            allowed_tools=frozenset({"message"}),
            reason="allow",
        ),
    )


def _ambient_channel(
    store: ProcessingStore, verdict: AmbientVerdict, *, action: str = "answer"
) -> tuple[WhatsAppChannel, _Judge, _Reaction, list[WhatsAppInboundEvent]]:
    channel = WhatsAppChannel(_no_debounce_config(), MessageBus())
    channel.set_processing_gate(_ambient_gate(store, action=action))
    judge = _Judge(verdict)
    channel.set_ambient_judge(judge)
    reaction = _Reaction()
    channel.set_reaction_action(reaction)
    published: list[WhatsAppInboundEvent] = []

    async def _fake_enrich(event: WhatsAppInboundEvent) -> WhatsAppInboundEvent:
        return event

    async def _fake_publish(event: WhatsAppInboundEvent) -> None:
        published.append(event)

    channel._enrich_media_event = _fake_enrich  # type: ignore[method-assign]
    channel._publish_event = _fake_publish  # type: ignore[method-assign]
    return channel, judge, reaction, published


@pytest.mark.asyncio
async def test_a_judged_reaction_is_the_whole_reply(tmp_path: Path) -> None:
    """The verdict `react` opens no turn - and the caller must not read a turn from it.

    Returning the reaction's success flag where the caller expected a turn assignment made
    every judged reaction raise right after the emoji had already gone out.
    """
    store = ProcessingStore(tmp_path / "p.db")
    channel, judge, reaction, published = _ambient_channel(
        store, AmbientVerdict(action="react", emoji="🤙", confidence=1.0)
    )

    await channel._ingest_inbound_event(_wa_event(mentioned_bot=False))

    assert judge.asked, "the brake passed, so the judge decides"
    assert reaction.sent == ["🤙"]
    assert len(published) == 1, "the message still reaches the pipeline as context"
    assert published[0].processing_reacted is True
    assert published[0].processing_answer_granted is False
    assert published[0].thread_assignment is None
    assert channel._processing_gate.is_ambient_pending("m1") is True, (
        "a reaction is the reply, so the classic pipeline must not answer as well"
    )
    store.close()


@pytest.mark.asyncio
async def test_an_answer_verdict_is_capped_to_a_reaction_in_a_react_chat(tmp_path: Path) -> None:
    """A chat on `react` never gets text: the judge's answer becomes the allowed reaction."""
    store = ProcessingStore(tmp_path / "p.db")
    channel, judge, reaction, published = _ambient_channel(
        store, AmbientVerdict(action="answer", confidence=1.0), action="react"
    )

    await channel._ingest_inbound_event(_wa_event(mentioned_bot=False))

    assert judge.asked, "the judge decides in a react chat too"
    assert reaction.sent == ["🤙"], "the chooser picks the emoji for the capped delivery"
    assert published[0].processing_reacted is True
    assert published[0].processing_answer_granted is False
    assert published[0].thread_assignment is None, "no text turn in a react chat"
    store.close()


@pytest.mark.asyncio
async def test_an_answer_verdict_still_opens_a_turn_when_text_is_allowed(tmp_path: Path) -> None:
    """The cap must not turn a normal chat's granted answer into a reaction."""
    store = ProcessingStore(tmp_path / "p.db")
    channel, judge, reaction, published = _ambient_channel(
        store, AmbientVerdict(action="answer", confidence=1.0)
    )

    await channel._ingest_inbound_event(_wa_event(mentioned_bot=False))

    assert judge.asked
    assert reaction.sent == []
    assert published[0].processing_answer_granted is True
    assert published[0].thread_assignment is not None
    assert published[0].thread_assignment["turn_id"]
    store.close()


@pytest.mark.asyncio
async def test_a_silence_chat_never_spends_a_judge_call(tmp_path: Path) -> None:
    """`silence` is a hard veto: no judge call, no reaction, no turn."""
    store = ProcessingStore(tmp_path / "p.db")
    channel, judge, reaction, published = _ambient_channel(
        store, AmbientVerdict(action="answer", confidence=1.0), action="silence"
    )

    await channel._ingest_inbound_event(_wa_event(mentioned_bot=False))

    assert judge.asked == []
    assert reaction.sent == []
    assert published[0].processing_answer_granted is False
    assert published[0].thread_assignment is None
    store.close()


# --------------------------------------------------------------------------------------
# final effect authorization
# --------------------------------------------------------------------------------------


def test_missing_permission_binding_denies(tmp_path: Path) -> None:
    authorizer = SnapshotEffectAuthorizer(snapshots=_StaticSnapshots())
    record = authorizer.check(_envelope(), None)

    assert record.outcome == "deny"
    assert record.reason.startswith("permission_denied")
    assert record.policy_version == "policy-v1"
    assert record.policy_hash == "hash-v1"
    assert record.stage == "final"
    assert record.effect_id == "fx1"


def test_expired_and_stale_turns_are_refused(tmp_path: Path) -> None:
    authorizer = SnapshotEffectAuthorizer(snapshots=_StaticSnapshots(), capabilities=_AllowAll())

    expired = authorizer.check(_envelope(expires_at_ms=1), None)
    assert expired.outcome == "deny" and expired.reason.startswith("expired")

    no_turn_state = authorizer.check(_envelope(turn_id="turn1"), None)
    assert no_turn_state.outcome == "deny"
    assert no_turn_state.reason.startswith("superseded")

    stale = SnapshotEffectAuthorizer(
        snapshots=_StaticSnapshots(),
        capabilities=_AllowAll(),
        turn_lookup=lambda turn_id: TurnRef(
            turn_id="turn1", thread_id="t", chat_id="chat@g.us", channel="whatsapp",
            principal="owner@s.whatsapp.net", revision=2,
        ),
    ).check(_envelope(turn_id="turn1", turn_revision=1), None)
    assert stale.reason.startswith("superseded")

    current = SnapshotEffectAuthorizer(
        snapshots=_StaticSnapshots(),
        capabilities=_AllowAll(),
        turn_lookup=lambda turn_id: TurnRef(
            turn_id="turn1", thread_id="t", chat_id="chat@g.us", channel="whatsapp",
            principal="owner@s.whatsapp.net", revision=1,
        ),
    ).check(_envelope(turn_id="turn1", turn_revision=1), None)
    assert current.outcome == "allow"


def test_capability_resolver_uses_the_policy_engine(tmp_path: Path) -> None:
    policy = _policy(who_can_talk="owner_only")
    engine = PolicyEngine(policy, workspace=tmp_path, apply_channels={"whatsapp"})
    resolver = PolicyCapabilityResolver(
        engine_provider=lambda: engine,
        known_tools=lambda: {"message", "send_voice", "delete_message"},
    )

    assert resolver.resolve(
        principal="owner@s.whatsapp.net", target=TARGET, capability="send_text"
    ) == (True, "allow")

    allowed, reason = resolver.resolve(
        principal="stranger@s.whatsapp.net", target=TARGET, capability="send_text"
    )
    assert allowed is False
    assert reason == "permission_denied"

    # An unknown capability must never fall through as permitted.
    allowed, reason = resolver.resolve(
        principal="owner@s.whatsapp.net", target=TARGET, capability="launch_missiles"
    )
    assert allowed is False
    assert reason.startswith("unmapped_capability")


def test_tool_capability_needs_an_explicit_tool_permission(tmp_path: Path) -> None:
    policy = PolicyConfig.model_validate(
        {
            "owners": {"whatsapp": ["owner@s.whatsapp.net"]},
            "channels": {
                "whatsapp": {
                    "default": {
                        "whoCanTalk": {"mode": "everyone"},
                        "whenToReply": {"mode": "all"},
                        "allowedTools": {"mode": "allowlist", "tools": ["message"]},
                    }
                }
            },
        }
    )
    engine = PolicyEngine(policy, workspace=tmp_path, apply_channels={"whatsapp"})
    resolver = PolicyCapabilityResolver(
        engine_provider=lambda: engine,
        known_tools=lambda: {"message", "send_voice"},
    )

    allowed, reason = resolver.resolve(
        principal="owner@s.whatsapp.net", target=TARGET, capability="send_voice"
    )
    assert allowed is False
    assert reason == "capability_denied:send_voice"

    allowed, _ = resolver.resolve(
        principal="owner@s.whatsapp.net", target=TARGET, capability="send_text"
    )
    assert allowed is True


def test_missing_engine_denies_capabilities(tmp_path: Path) -> None:
    resolver = PolicyCapabilityResolver(engine_provider=lambda: None, known_tools=lambda: set())
    allowed, reason = resolver.resolve(
        principal="owner@s.whatsapp.net", target=TARGET, capability="send_text"
    )
    assert allowed is False
    assert reason == "policy_unavailable"


# --------------------------------------------------------------------------------------
# shadow mode (spec section 5)
# --------------------------------------------------------------------------------------


def _shadow_config(chats: tuple[str, ...] = (f"whatsapp:{'chat@g.us'}",)) -> ProcessingConfig:
    return ProcessingConfig.model_validate(
        {"enabled": True, "chats": [], "shadow_chats": list(chats)}
    )


def test_shadow_chat_decides_and_journals_without_acting(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "p.db")
    gate = IngestGate(
        config=_shadow_config(),
        store=store,
        snapshots=_StaticSnapshots(),
        evaluate=lambda request: _deny(),  # a denial must not stop shadow traffic
    )

    result = gate.admit(_request())

    assert result is not None
    assert result.shadow is True
    assert result.proceed is True  # shadow never blocks
    assert result.journaled_event_id == "evt-1"
    assert store.count_events() == 1
    assert result.decision is not None
    assert store.get_decision(result.decision.decision_id) is not None
    store.close()


def test_shadow_gate_stays_inert_when_processing_is_disabled(tmp_path: Path) -> None:
    store = ProcessingStore(tmp_path / "p.db")
    config = ProcessingConfig.model_validate(
        {"enabled": False, "shadow_chats": ["whatsapp:chat@g.us"]}
    )
    gate = IngestGate(
        config=config,
        store=store,
        snapshots=_StaticSnapshots(),
        evaluate=lambda request: _deny(),
    )

    assert gate.admit(_request()) is None
    assert store.count_events() == 0
    store.close()


def test_shadow_chat_creates_no_effects(tmp_path: Path) -> None:
    from yeoman_gateway.processing.dispatch import IntentEffectRouter

    store = ProcessingStore(tmp_path / "p.db")
    config = ProcessingConfig.model_validate(
        {"enabled": True, "chats": [], "shadow_chats": ["whatsapp:chat@g.us"]}
    )
    executor = _Executor("sent")
    gateway = EffectGateway(
        store,
        authorizer=SnapshotEffectAuthorizer(
            snapshots=_StaticSnapshots(), capabilities=_AllowAll(), clock=_Clock(0)
        ),
        executor=executor,
        clock=_Clock(0),
    )
    router = IntentEffectRouter(gateway=gateway, config=config, clock=_Clock(0))

    assert router.manages("whatsapp", "chat@g.us") is False
    assert store.count_effects() == 0
    store.close()
