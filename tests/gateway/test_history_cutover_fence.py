"""Synthetic fence witnesses: the owner's global pause at every enabled boundary.

Synthetic homes and transport doubles only; no live service, store or traffic is touched.
The owner-control acknowledgement is exercised through the real admin middleware and the
real transport guard, never through a caller-supplied boolean.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

PAUSED = "paused_global"


class FakeChannel:
    """Transport double: records every call that actually reaches the channel."""

    def __init__(self) -> None:
        self.sent: list[object] = []
        self.reactions: list[object] = []
        self.typing: list[tuple[str, bool]] = []
        self.recording: list[str] = []

    async def send(self, message: object) -> dict:
        self.sent.append(message)
        return {"provider_message_id": "synthetic"}

    async def send_reaction(self, message: object) -> dict:
        self.reactions.append(message)
        return {"provider_message_id": "synthetic-reaction"}

    async def start_typing(self, chat_id: str) -> None:
        self.typing.append((chat_id, True))

    async def stop_typing(self, chat_id: str) -> None:
        self.typing.append((chat_id, False))

    async def start_recording(self, chat_id: str) -> None:
        self.recording.append(chat_id)


def manager(*, paused: bool, acks=None):
    from yeoman_gateway.bus.queue import MessageBus
    from yeoman_gateway.channels.manager import ChannelManager
    from yeoman_shared.config.schema import Config

    bus = MessageBus()
    channels = ChannelManager(Config(), bus)
    channel = FakeChannel()
    channels.channels["whatsapp"] = channel
    channels.set_global_pause_probe(lambda *_: PAUSED if paused else None)
    channels.set_control_acknowledgements(acks)
    return channels, channel, bus


async def drain_once(channels) -> None:
    """Run the real queued dispatcher until it has consumed everything on the bus."""
    task = asyncio.create_task(channels._dispatch_outbound())
    reaction = asyncio.create_task(channels._dispatch_reactions())
    for _ in range(20):
        await asyncio.sleep(0)
    task.cancel()
    reaction.cancel()
    await asyncio.gather(task, reaction, return_exceptions=True)


def outbound(content: str = "synthetic", **metadata):
    from yeoman_gateway.bus.events import OutboundMessage

    return OutboundMessage(
        channel="whatsapp", chat_id="synthetic-chat", content=content, metadata=metadata
    )


def reaction(**metadata):
    from yeoman_gateway.bus.events import ReactionMessage

    return ReactionMessage(
        channel="whatsapp",
        chat_id="synthetic-chat",
        message_id="synthetic-message",
        emoji="👍",
        metadata=metadata,
    )


@pytest.mark.asyncio
async def test_global_pause_refuses_queued_and_direct_transports():
    from yeoman_gateway.processing.models import GlobalPauseRefused

    channels, channel, bus = manager(paused=True)

    with pytest.raises(GlobalPauseRefused):
        await channels.send_now(outbound())
    with pytest.raises(GlobalPauseRefused):
        await channels.send_reaction_now(reaction())

    await bus.publish_outbound(outbound())
    await bus.publish_reaction(reaction())
    await drain_once(channels)

    await channels.set_typing("whatsapp", "synthetic-chat", True)
    await channels.set_recording("whatsapp", "synthetic-chat")

    assert channel.sent == [] and channel.reactions == []
    assert channel.typing == [] and channel.recording == []
    assert bus.outbound.empty() and bus.reaction.empty()


@pytest.mark.asyncio
async def test_unpaused_transport_still_delivers():
    channels, channel, bus = manager(paused=False)
    await channels.send_now(outbound())
    await bus.publish_outbound(outbound("queued"))
    await bus.publish_reaction(reaction())
    await drain_once(channels)
    assert [m.content for m in channel.sent] == ["synthetic", "queued"]
    assert len(channel.reactions) == 1


@pytest.mark.asyncio
async def test_unreadable_pause_state_refuses_before_delivery():
    channels, channel, _ = manager(paused=False)

    def broken(*_):
        raise RuntimeError("synthetic policy outage")

    channels.set_global_pause_probe(broken)
    from yeoman_gateway.processing.models import GlobalPauseRefused

    with pytest.raises(GlobalPauseRefused):
        await channels.send_now(outbound())
    assert channel.sent == []


@pytest.mark.asyncio
async def test_paused_effect_executor_refuses_every_origin_before_transport():
    from yeoman_gateway.processing.dispatch import BusEffectExecutor
    from yeoman_gateway.processing.models import (
        DeletePayload,
        EffectEnvelope,
        EffectTarget,
        ExternalActionPayload,
        ParticipationPreDispatchDenied,
        TextPayload,
    )

    delivered: list[object] = []
    deleted: list[object] = []
    external: list[object] = []

    async def sender(message):
        delivered.append(message)
        return {"provider_message_id": "synthetic"}

    async def delete_handler(envelope):
        deleted.append(envelope)
        return True

    async def external_handler(envelope):
        external.append(envelope)
        return True

    executor = BusEffectExecutor(
        bus=SimpleNamespace(publish_outbound=None, publish_reaction=None),
        direct_sender=sender,
        delete_handler=delete_handler,
        external_handler=external_handler,
        pause_pre_dispatch=lambda envelope: (False, PAUSED),
    )

    def envelope(payload, origin):
        return EffectEnvelope(
            effect_id="synthetic-effect",
            operation_key="synthetic-operation",
            payload=payload,
            target=EffectTarget(channel="whatsapp", chat_id="synthetic-chat"),
            trace_id="synthetic-trace",
            turn_id="",
            turn_revision=1,
            principal="owner",
            capability="send_text",
            expires_at_ms=1,
            created_ms=0,
            origin=origin,
        )

    for origin in ("legacy", "participation", "service"):
        with pytest.raises(ParticipationPreDispatchDenied):
            await executor.execute(envelope(TextPayload(text="synthetic"), origin))
    with pytest.raises(ParticipationPreDispatchDenied):
        await executor.execute(envelope(DeletePayload(message_id="synthetic"), "legacy"))
    with pytest.raises(ParticipationPreDispatchDenied):
        await executor.execute(
            envelope(ExternalActionPayload(action="synthetic"), "legacy")
        )
    assert delivered == [] and deleted == [] and external == []


@pytest.mark.asyncio
async def test_paused_refusal_is_not_a_sent_receipt(tmp_path):
    """A suppressed delivery is recorded as not executed, never as sent."""
    from yeoman_gateway.processing.dispatch import BusEffectExecutor
    from yeoman_gateway.processing.effects import EffectGateway
    from yeoman_gateway.processing.models import (
        EffectEnvelope,
        EffectTarget,
        TextPayload,
    )
    from yeoman_gateway.processing.store import ProcessingStore

    store = ProcessingStore(tmp_path / "processing.db")
    try:
        delivered: list[object] = []

        async def sender(message):  # pragma: no cover - must never be reached
            delivered.append(message)
            return {"provider_message_id": "synthetic"}

        executor = BusEffectExecutor(
            bus=SimpleNamespace(publish_outbound=None, publish_reaction=None),
            direct_sender=sender,
            mark_provenance=True,
            pause_pre_dispatch=lambda envelope: (False, PAUSED),
        )
        def allow(envelope, turn):
            from yeoman_gateway.processing.models import DecisionRecord

            return DecisionRecord(
                decision_id=f"dec-{envelope.effect_id}",
                trace_id=envelope.trace_id,
                policy_version="synthetic",
                policy_hash="synthetic",
                principal=envelope.principal,
                target=envelope.target.key(),
                capability=envelope.capability,
                turn_revision=envelope.turn_revision,
                outcome="allow",
                reason="synthetic",
                created_ms=0,
            )

        gateway = EffectGateway(
            store, authorizer=SimpleNamespace(check=allow), executor=executor
        )
        envelope = EffectEnvelope(
            effect_id="synthetic-effect",
            operation_key="synthetic-operation",
            payload=TextPayload(text="synthetic"),
            target=EffectTarget(channel="whatsapp", chat_id="synthetic-chat"),
            trace_id="synthetic-trace",
            turn_id="",
            turn_revision=1,
            principal="owner",
            capability="send_text",
            expires_at_ms=9_999_999_999_999,
            created_ms=0,
            origin="legacy",
        )
        submitted = gateway.submit(envelope)
        result = await gateway.execute_ready(submitted.effect_id)
        assert result.state != "sent"
        assert store.effect_transport_receipt(result.effect_id) is None
        assert delivered == []
    finally:
        store.close()


# ── owner-control acknowledgement ─────────────────────────────────────────────


def admin_result(**changes):
    from yeoman_gateway.core.admin_commands import AdminCommandResult

    value = dict(
        status="handled",
        response="⏸️ Responses paused for all chats until /start all.",
        command_name="stop",
        outcome="applied",
        source="dm",
    )
    value.update(changes)
    return AdminCommandResult(**value)


async def run_admin_pipeline(result, acks):
    from yeoman_gateway.core.models import InboundEvent
    from yeoman_gateway.core.pipeline import Pipeline
    from yeoman_gateway.pipeline.admin import AdminCommandMiddleware

    pipeline = Pipeline([AdminCommandMiddleware(handler=lambda _: result, acknowledgements=acks)])
    event = InboundEvent(
        channel="whatsapp",
        chat_id="synthetic-owner@s.whatsapp.net",
        sender_id="synthetic-owner",
        content="/stop all",
        timestamp=datetime(2026, 10, 10, tzinfo=UTC),
    )
    return await pipeline.run(event)


@pytest.mark.asyncio
async def test_applied_owner_control_acknowledgement_survives_the_fence():
    from yeoman_gateway.core.control_ack import CONTROL_ACK_KEY, OwnerControlAcknowledgements

    acks = OwnerControlAcknowledgements()
    intents = await run_admin_pipeline(admin_result(), acks)
    event = next(i.event for i in intents if hasattr(i, "event") and i.event.content)
    token = event.metadata[CONTROL_ACK_KEY]

    channels, channel, _ = manager(paused=True, acks=acks)

    def ack_message(token_value):
        from yeoman_gateway.bus.events import OutboundMessage

        return OutboundMessage(
            channel=event.channel,
            chat_id=event.chat_id,
            content=event.content,
            metadata={CONTROL_ACK_KEY: token_value},
        )

    await channels.send_now(ack_message(token))
    # The token is single-use: a replay of the same content is fenced like anything else.
    from yeoman_gateway.processing.models import GlobalPauseRefused

    with pytest.raises(GlobalPauseRefused):
        await channels.send_now(ack_message(token))
    assert [m.content for m in channel.sent] == [event.content]


@pytest.mark.asyncio
async def test_model_or_caller_supplied_exemption_never_passes_the_fence():
    from yeoman_gateway.core.control_ack import CONTROL_ACK_KEY, OwnerControlAcknowledgements
    from yeoman_gateway.processing.models import GlobalPauseRefused

    acks = OwnerControlAcknowledgements()
    intents = await run_admin_pipeline(admin_result(), acks)
    real = next(i.event for i in intents if hasattr(i, "event") and i.event.content)

    channels, channel, _ = manager(paused=True, acks=acks)

    def message(token_value=None):
        from yeoman_gateway.bus.events import OutboundMessage

        metadata = {} if token_value is None else {CONTROL_ACK_KEY: token_value}
        return OutboundMessage(
            channel=real.channel,
            chat_id=real.chat_id,
            content=real.content,
            metadata=metadata,
        )

    for forged in (
        "0" * 64,
        real.metadata[CONTROL_ACK_KEY][:-1] + "0",
        "",
        None,
    ):
        with pytest.raises(GlobalPauseRefused):
            await channels.send_now(message(forged))
    assert channel.sent == []


@pytest.mark.asyncio
async def test_non_applied_admin_control_is_not_an_acknowledgement():
    from yeoman_gateway.core.control_ack import CONTROL_ACK_KEY, OwnerControlAcknowledgements
    from yeoman_gateway.processing.models import GlobalPauseRefused

    acks = OwnerControlAcknowledgements()
    intents = await run_admin_pipeline(admin_result(outcome="invalid"), acks)
    event = next(i.event for i in intents if hasattr(i, "event") and i.event.content)
    assert CONTROL_ACK_KEY not in event.metadata

    channels, channel, _ = manager(paused=True, acks=acks)
    from yeoman_gateway.bus.events import OutboundMessage

    with pytest.raises(GlobalPauseRefused):
        await channels.send_now(
            OutboundMessage(
                channel=event.channel, chat_id=event.chat_id, content=event.content
            )
        )
    assert channel.sent == []


@pytest.mark.asyncio
async def test_control_acknowledgement_keeps_its_token_through_bus_and_transport():
    """The whole path: admin intent -> dispatch -> managed guard -> transport claim."""
    from yeoman_gateway.app.bootstrap import OrchestratorService
    from yeoman_gateway.core.control_ack import OwnerControlAcknowledgements
    from yeoman_gateway.processing.dispatch import managed_outbound_guard

    acks = OwnerControlAcknowledgements()
    intents = await run_admin_pipeline(admin_result(), acks)
    ack_intent = next(i for i in intents if hasattr(i, "event") and i.event.content)

    channels, channel, bus = manager(paused=True, acks=acks)

    class Router:
        def manages(self, channel, chat_id):
            return True

        def effect_covers(self, effect_id, channel, chat_id):
            return True

    bus.set_managed_outbound_guard(managed_outbound_guard(Router(), acknowledgements=acks))
    service = OrchestratorService(
        bus=bus,
        orchestrator=SimpleNamespace(),
        typing_adapter=None,
        telemetry=None,
        memory=None,
        effect_router=None,
        control_acknowledgements=acks,
    )
    await service._dispatch_intents([ack_intent])
    await drain_once(channels)
    assert [m.content for m in channel.sent] == [ack_intent.event.content]

    # A model reply to the same chat is refused by the managed guard before the queue.
    from yeoman_gateway.bus.events import OutboundMessage

    await bus.publish_outbound(
        OutboundMessage(channel="whatsapp", chat_id="synthetic-chat", content="model reply")
    )
    assert bus.outbound.empty()


@pytest.mark.asyncio
async def test_owner_control_still_processed_while_paused():
    """The control itself is executed by the deterministic router, not by the fence."""
    from yeoman_gateway.core.control_ack import OwnerControlAcknowledgements

    acks = OwnerControlAcknowledgements()
    seen: list[str] = []

    def handler(event):
        seen.append(event.content)
        return admin_result()

    from yeoman_gateway.core.models import InboundEvent
    from yeoman_gateway.core.pipeline import Pipeline
    from yeoman_gateway.pipeline.admin import AdminCommandMiddleware

    pipeline = Pipeline(
        [AdminCommandMiddleware(handler=handler, acknowledgements=acks)]
    )
    event = InboundEvent(
        channel="whatsapp",
        chat_id="synthetic-owner@s.whatsapp.net",
        sender_id="synthetic-owner",
        content="/stop all",
        timestamp=datetime(2026, 10, 10, tzinfo=UTC),
    )
    intents = await pipeline.run(event)
    assert seen == ["/stop all"]
    assert any(getattr(i, "event", None) is not None for i in intents)


# ── administrative notification state ─────────────────────────────────────────


def seen_path() -> Path:
    from yeoman_shared.utils.helpers import get_operational_store_path

    return get_operational_store_path("seen_chats")


@pytest.mark.asyncio
async def test_first_contact_notification_deferred_while_paused(tmp_path, monkeypatch):
    from yeoman_gateway.core.intents import SendOutboundIntent
    from yeoman_gateway.core.models import InboundEvent, PolicyDecision
    from yeoman_gateway.core.pipeline import Pipeline
    from yeoman_gateway.pipeline.access import AccessControlMiddleware, NoReplyFilterMiddleware
    from yeoman_gateway.pipeline.new_chat import NewChatNotifyMiddleware
    from yeoman_gateway.pipeline.policy import PolicyMiddleware

    decision = PolicyDecision(
        accept_message=True,
        should_respond=False,
        allowed_tools=frozenset(),
        reason="paused_global",
    )
    paused = {"value": True}
    notify = NewChatNotifyMiddleware(
        owner_alert_resolver=lambda _: ["synthetic-owner@s.whatsapp.net"],
        global_pause=lambda *_: PAUSED if paused["value"] else None,
    )
    pipeline = Pipeline([
        PolicyMiddleware(policy=SimpleNamespace(evaluate=lambda _: decision)),
        AccessControlMiddleware(),
        notify,
        NoReplyFilterMiddleware(),
    ])
    event = InboundEvent(
        channel="whatsapp",
        chat_id="synthetic-fence-new@g.us",
        sender_id="synthetic-sender",
        content="synthetic",
        timestamp=datetime(2026, 10, 10, tzinfo=UTC),
    )

    before = seen_path().read_bytes() if seen_path().exists() else None
    intents = await pipeline.run(event)
    assert not any(isinstance(i, SendOutboundIntent) for i in intents)
    assert notify._notified == set()
    # Neither the in-memory set nor the persisted store advanced for this chat.
    after = seen_path().read_bytes() if seen_path().exists() else None
    assert after == before and b"synthetic-fence-new@g.us" not in (after or b"")

    # The chat is still unseen, so the next legitimate unpaused invocation alerts once.
    paused["value"] = False
    intents = await pipeline.run(event)
    delivered = [i for i in intents if isinstance(i, SendOutboundIntent)]
    assert len(delivered) == 1
    assert "whatsapp:synthetic-fence-new@g.us" in json.loads(seen_path().read_text())["chats"]

    # And it does not alert twice.
    intents = await pipeline.run(event)
    assert not any(isinstance(i, SendOutboundIntent) for i in intents)


@pytest.mark.asyncio
async def test_first_contact_notification_without_probe_unchanged(monkeypatch):
    """A composition that installs no probe keeps its previous behaviour (documented)."""
    from yeoman_gateway.core.intents import SendOutboundIntent
    from yeoman_gateway.core.models import InboundEvent, PolicyDecision
    from yeoman_gateway.core.pipeline import Pipeline
    from yeoman_gateway.pipeline.access import AccessControlMiddleware, NoReplyFilterMiddleware
    from yeoman_gateway.pipeline.new_chat import NewChatNotifyMiddleware
    from yeoman_gateway.pipeline.policy import PolicyMiddleware

    decision = PolicyDecision(
        accept_message=True,
        should_respond=False,
        allowed_tools=frozenset(),
        reason="paused_global",
    )
    pipeline = Pipeline([
        PolicyMiddleware(policy=SimpleNamespace(evaluate=lambda _: decision)),
        AccessControlMiddleware(),
        NewChatNotifyMiddleware(owner_alert_resolver=lambda _: ["synthetic-owner@s.whatsapp.net"]),
        NoReplyFilterMiddleware(),
    ])
    event = InboundEvent(
        channel="whatsapp",
        chat_id="synthetic-probeless-fence@g.us",
        sender_id="synthetic-sender",
        content="synthetic",
        timestamp=datetime(2026, 10, 10, tzinfo=UTC),
    )
    intents = await pipeline.run(event)
    assert any(isinstance(i, SendOutboundIntent) for i in intents)


def test_persona_review_notifications_deferred_while_paused(tmp_path):
    from yeoman_gateway.persona_evolution import PersonaEvolutionLedger

    ledger = PersonaEvolutionLedger(tmp_path / "persona-evolution.db")
    try:
        now = datetime(2026, 10, 10, tzinfo=UTC)
        ledger.record_proposal(
            proposal_id="synthetic-proposal",
            persona_file="synthetic.md",
            proposal_path=tmp_path / "synthetic-proposal.md",
            created_at=now,
            evidence_from=now,
            evidence_to=now,
            total_message_count=1,
            signal_score=1.0,
            base_hash="a" * 64,
        )
        paused = {"value": True}
        assert ledger.pending_review_notifications(
            global_pause_active=lambda: paused["value"]
        ) == []
        rows = ledger.pending_proposals()
        assert [row["proposal_id"] for row in rows] == ["synthetic-proposal"]
        assert rows[0]["notified_at"] is None

        # The next legitimate unpaused invocation announces the same row exactly once.
        paused["value"] = False
        selected = ledger.pending_review_notifications(global_pause_active=lambda: False)
        assert [row["proposal_id"] for row in selected] == ["synthetic-proposal"]
        ledger.mark_notified("synthetic-proposal", channel="telegram", chat_id="synthetic")
        assert ledger.pending_review_notifications(global_pause_active=lambda: False) == []
        assert ledger.pending_proposals()[0]["notified_at"] is not None
    finally:
        ledger.close()


# ── persistent owner-stop fence: acquisition, restore, smoke, timers ──────────


def live_case(tmp_path):
    """A live host control over injected transports; no unit is ever touched."""
    import os

    from tests.gateway.test_history_cutover_host import (
        Clock,
        Runner,
        host_module,
        inventory,
        payload,
    )

    runtime = tmp_path / "systemd/user"
    runtime.mkdir(parents=True)
    (tmp_path / "proc").mkdir()
    inv = inventory() | dict(
        systemd_runtime_dir=str(runtime),
        owner_uid=os.getuid(),
        pause_path=str(tmp_path / "data/ops/response-pauses.json"),
    )
    runner = Runner()
    runner.runtime = runtime
    control = host_module().live_host_controls(
        inventory=inv, runner=runner, clock=Clock(), proc_root=tmp_path / "proc"
    )
    return control, runner, inv, payload(tmp_path)


def canonical_pause(tmp_path, *, global_until=-1, chats=None):
    path = tmp_path / "data/ops/response-pauses.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(dict(version=1, global_until_ms=global_until, chat_until_ms=chats or {}))
    )
    return path


def test_fence_refuses_missing_inactive_and_foreign_owner_pause(tmp_path):
    control, _, _inv, p = live_case(tmp_path)
    pause = tmp_path / "data/ops/response-pauses.json"

    with pytest.raises(ValueError, match="pause_store_unreadable"):
        control("fence-effects", p)

    canonical_pause(tmp_path, global_until=0)
    with pytest.raises(ValueError, match="pause_store_not_indefinite"):
        control("fence-effects", p)

    canonical_pause(tmp_path, global_until=9_999_999_999_999)
    with pytest.raises(ValueError, match="pause_store_not_indefinite"):
        control("fence-effects", p)

    canonical_pause(tmp_path, chats={"whatsapp:synthetic@g.us": -1})
    with pytest.raises(ValueError, match="pause_store_chat_drift"):
        control("fence-effects", p)

    canonical_pause(tmp_path)
    result = control("fence-effects", p)
    assert result["ok"] and result["pause_global_until_ms"] == -1
    assert result["pause_chat_keys"] == []
    assert result["pause_baseline_sha256"] == hashlib.sha256(pause.read_bytes()).hexdigest()
    assert result["prior_pauses_preserved"] is True


def test_release_names_the_authenticated_fence_digest(tmp_path):
    control, runner, _, p = live_case(tmp_path)
    pause = canonical_pause(tmp_path)
    fenced = control("fence-effects", p)
    assert fenced["ok"] and fenced["pause_baseline_sha256"]
    # The sequence starts Bridge and Gateway again before it releases the fence.
    for unit in ("yeoman-bridge.service", "yeoman-gateway.service"):
        runner(["systemctl", "--user", "show", unit])
        runner.states[unit]["ActiveState"] = "active"
    released = control("release-fence", p)
    assert released["ok"] and released["prior_pauses_preserved"] is True
    assert released["prior_pauses_sha256"] == fenced["pause_baseline_sha256"]
    assert released["prior_pauses_sha256"] == hashlib.sha256(pause.read_bytes()).hexdigest()


def test_timer_inactive_while_associated_oneshot_activating_refuses(tmp_path):
    control, runner, _inv, p = live_case(tmp_path)
    canonical_pause(tmp_path)
    # The timer is stopped but the oneshot it already triggered is still activating,
    # so stopping the timer must not be mistaken for a quiescent fence.
    runner.ignore_stop = True
    runner(["systemctl", "--user", "show", "watch.timer"])
    runner.states["watch.timer"]["ActiveState"] = "inactive"
    runner.states["watch.service"] = {
        "ActiveState": "activating",
        "Result": "success",
        "UnitFileState": "static",
        "Restart": "no",
        "ActiveEnterTimestampMonotonic": "0",
        "ExecMainStartTimestampMonotonic": "0",
        "ExecMainExitTimestampMonotonic": "0",
    }
    result = control("stop-timers-and-manual-routes", p)
    assert result["ok"] is False and result["strictly_inactive"] is False
    assert result["timer_services"] == ["watch.service"]
    # The associated service is stopped explicitly next to its timer.
    assert ["systemctl", "--user", "stop", "watch.service"] in runner.calls
    with pytest.raises(ValueError, match="stop_fence_unproven"):
        control("fence-effects", p)


def test_missing_timer_service_association_refuses(tmp_path):
    from scripts.history_cutover import validate_host_inventory
    from tests.gateway.test_history_cutover import record
    from tests.gateway.test_history_cutover_host import host_module

    _, _home, value = record(tmp_path)
    inv = value["inventory"]
    validate_host_inventory(inv, mode="live")
    incomplete = {key: item for key, item in inv.items() if key != "timer_services"}
    with pytest.raises(ValueError, match="missing_inventory_key:timer_services"):
        validate_host_inventory(incomplete, mode="live")
    with pytest.raises(ValueError, match="timer_service_association_required"):
        validate_host_inventory(inv | {"timer_services": {}}, mode="live")
    with pytest.raises(ValueError, match="timer_service_association_required"):
        host_module().live_host_controls(
            inventory=incomplete, runner=object(), clock=object(), proc_root=tmp_path
        )


def restore_case(tmp_path):
    """A snapshot whose pause member is the canonical owner-stop record."""
    from tests.gateway.test_history_cutover import (
        authenticate_pause,
        procedure,
        record,
    )

    m = procedure()
    path, home, value = record(tmp_path)
    pause = Path(value["inventory"]["pause_path"])
    m.acquire_cutover_snapshot(
        home=home, output=Path(value["output"]), inventory=value["inventory"]
    )
    authenticate_pause(value)
    return m, home, value, pause, hashlib.sha256(pause.read_bytes()).hexdigest()


def test_restore_refuses_foreign_pause_drift_before_overwriting(tmp_path):
    m, home, value, pause, digest = restore_case(tmp_path)
    canonical = pause.read_bytes()
    pause.write_text(json.dumps(dict(version=1, global_until_ms=9_999_999_999_999, chat_until_ms={})))
    drifted = pause.read_bytes()
    with pytest.raises(ValueError, match="foreign_pause_drift"):
        m._restore_files(value, home)
    assert pause.read_bytes() == drifted
    pause.write_bytes(canonical)
    assert m._restore_files(value, home)["complete"]
    assert pause.read_bytes() == canonical


def test_restore_accepts_cleared_state_and_leaves_prior_startup_paused(tmp_path):
    m, home, value, pause, digest = restore_case(tmp_path)
    pause.write_text(json.dumps(dict(version=1, global_until_ms=0, chat_until_ms={})))
    result = m._restore_files(value, home)
    assert result["complete"]
    # The prior startup comes back under the authenticated indefinite pause.
    facts = m.pause_facts(pause)
    assert facts["global_until_ms"] == -1 and facts["chat_keys"] == []
    assert result["pause_restored_sha256"] == digest
    assert result["pause_before_sha256"] != digest
    assert any(entry["disposition"] == "retained" for entry in result["preserved"])
    assert any(entry["disposition"] == "restored" for entry in result["restored"])


def test_restore_requires_the_canonical_pause_member(tmp_path):
    m, home, value, pause, digest = restore_case(tmp_path)
    m.require_pause_restore_member(value["inventory"], home)
    without = dict(
        value["inventory"],
        members=[
            entry
            for entry in value["inventory"]["members"]
            if entry["path"] != "data/ops/response-pauses.json"
        ],
    )
    with pytest.raises(ValueError, match="pause_restore_member_required"):
        m.require_pause_restore_member(without, home)
    # A member that is not restored (or is external) does not satisfy the requirement.
    for broken in (
        [
            dict(entry, restore=False) if entry["path"] == "data/ops/response-pauses.json" else entry
            for entry in value["inventory"]["members"]
        ],
        [
            dict(entry, source=str(pause))
            if entry["path"] == "data/ops/response-pauses.json"
            else entry
            for entry in value["inventory"]["members"]
        ],
    ):
        with pytest.raises(ValueError, match="pause_restore_member_required"):
            m.require_pause_restore_member(dict(value["inventory"], members=broken), home)


def test_smoke_refuses_while_paused_or_foreign_and_only_accepts_proven_release(tmp_path):
    from scripts.history_cutover_smoke import _release, publish_ack
    from tests.gateway.test_history_cutover import procedure
    from tests.gateway.test_history_cutover_smoke import (
        FENCED_PAUSE,
        RELEASED_PAUSE,
        FakeClock,
        smoke_case,
    )

    path, value, inputs, runner, _ = smoke_case(tmp_path)
    pause = Path(value["inventory"]["pause_path"])

    # Still fenced: the owner never released the persistent control.
    pause.write_text(FENCED_PAUSE)
    with pytest.raises(ValueError, match="pause_store_not_cleared"):
        publish_ack(record=path, inputs=inputs, owner_confirmed_arrival=True, runner=runner, clock=FakeClock())
    # Foreign drift: neither the recorded fence nor a verified release.
    pause.write_text(json.dumps(dict(version=1, global_until_ms=9_999_999_999_999, chat_until_ms={})))
    with pytest.raises(ValueError, match="pause_store_not_cleared"):
        publish_ack(record=path, inputs=inputs, owner_confirmed_arrival=True, runner=runner, clock=FakeClock())
    # A release permission that does not name the authenticated acquired fence refuses.
    released = _release(value)
    assert isinstance(released, int)
    pause.write_text(RELEASED_PAUSE)
    actions = procedure()._sequence(value)
    release_path = Path(value["receipts"]) / f'cutover-{actions.index("release-fence")+1:02}.json'
    original = json.loads(release_path.read_text())
    release_path.write_text(
        json.dumps(original | {"receipt": original["receipt"] | {"prior_pauses_sha256": "0" * 64}})
    )
    with pytest.raises(ValueError, match="smoke_release_unproven"):
        publish_ack(record=path, inputs=inputs, owner_confirmed_arrival=True, runner=runner, clock=FakeClock())
    release_path.write_text(json.dumps(original))
    # An unauthenticated acquisition (no durable pause receipt) refuses as well.
    acquire_path = Path(value["receipts"]) / f'cutover-{actions.index("acquire")+1:02}.json'
    acquire_original = json.loads(acquire_path.read_text())
    acquire_path.unlink()
    with pytest.raises(ValueError, match="smoke_acquisition_fence_unauthenticated"):
        publish_ack(record=path, inputs=inputs, owner_confirmed_arrival=True, runner=runner, clock=FakeClock())
    acquire_path.write_text(json.dumps(acquire_original))
    assert publish_ack(record=path, inputs=inputs, owner_confirmed_arrival=True, runner=runner, clock=FakeClock()) == dict(ok=True)
