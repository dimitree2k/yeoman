"""Production-composition regressions for autonomous participation."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from yeoman_gateway.app.bootstrap import (
    GatewayRuntime,
    OrchestratorService,
    _build_participation_runtime,
    _has_pending_participation_recovery,
    _offer_participation_trigger,
    build_effect_router,
    build_gateway_runtime,
    build_reconciliation_service,
)
from yeoman_gateway.bus.events import InboundObservedEvent
from yeoman_gateway.bus.queue import MessageBus
from yeoman_gateway.channels.whatsapp import InboundEvent, WhatsAppChannel
from yeoman_gateway.policy.engine import PolicyEngine
from yeoman_gateway.policy.schema import PolicyConfig
from yeoman_gateway.processing.store import ProcessingStore
from yeoman_shared.config.schema import Config, WhatsAppConfig


@pytest.mark.asyncio
async def test_reconciliation_wiring_projects_participation_before_empty_return(
    tmp_path: Path,
) -> None:
    """Receipt projection is recovery, not optional learning or unknown probing."""
    calls = 0

    async def project_participation_receipts() -> None:
        nonlocal calls
        calls += 1

    config = Config.model_validate({"processing": {"enabled": True}})
    store = ProcessingStore(tmp_path / "processing.db")
    try:
        service = build_reconciliation_service(
            config,
            store,
            project_participation_receipts=project_participation_receipts,
        )
        assert service is not None
        assert await service.tick_once() == ()
        assert calls == 1
    finally:
        store.close()


@pytest.mark.asyncio
async def test_startup_projection_failure_is_fatal_but_background_tick_is_tolerant(
    tmp_path: Path,
) -> None:
    async def fail_projection() -> None:
        raise RuntimeError("ledger unavailable")

    config = Config.model_validate({"processing": {"enabled": True}})
    store = ProcessingStore(tmp_path / "processing.db")
    try:
        service = build_reconciliation_service(
            config,
            store,
            project_participation_receipts=fail_projection,
        )
        assert service is not None
        with pytest.raises(RuntimeError, match="ledger unavailable"):
            await service.recover_once()
        assert await service.tick_once() == ()
    finally:
        store.close()


@pytest.mark.asyncio
async def test_failed_startup_recovery_blocks_every_ingress() -> None:
    starts: list[str] = []

    class _Recovery:
        async def tick_once(self) -> None:
            raise RuntimeError("ledger unavailable")

        async def stop(self) -> None:
            return None

    class _Idle:
        tools: dict[str, object] = {}

        async def start(self) -> None:
            starts.append("service")

        async def run(self) -> None:
            starts.append("orchestrator")

        async def start_all(self) -> None:
            starts.append("channels")

        async def stop_all(self) -> None:
            return None

        async def aclose(self) -> None:
            return None

        def stop(self) -> None:
            return None

        def close(self) -> None:
            return None

    class _Socket:
        async def start(self) -> None:
            starts.append("socket")

        async def stop(self) -> None:
            return None

    idle = _Idle()
    runtime = GatewayRuntime(
        orchestrator=idle,  # type: ignore[arg-type]
        channels=idle,  # type: ignore[arg-type]
        cron=idle,  # type: ignore[arg-type]
        heartbeat=idle,  # type: ignore[arg-type]
        consciousness=None,
        inbound_archive=idle,  # type: ignore[arg-type]
        responder=idle,  # type: ignore[arg-type]
        memory=idle,  # type: ignore[arg-type]
        contacts=idle,  # type: ignore[arg-type]
        chat_registry=idle,
        gateway_socket=_Socket(),  # type: ignore[arg-type]
        reconciliation=_Recovery(),
    )

    with pytest.raises(RuntimeError, match="ledger unavailable"):
        await runtime.run()

    assert starts == []


def test_processing_only_pending_data_enables_recovery_without_creating_speakup_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from yeoman_gateway.processing.models import (
        EffectEnvelope,
        EffectTarget,
        TextPayload,
        canonical_hash,
        payload_to_mapping,
    )
    from yeoman_gateway.processing.participation_runtime import ParticipationAdmission

    processing_path = tmp_path / "processing.db"
    speakup_path = tmp_path / "data" / "consciousness" / "speakups.db"
    payload = TextPayload(text="pending")
    store = ProcessingStore(processing_path)
    admission = ParticipationAdmission(
        opportunity_id="processing-only",
        channel="whatsapp",
        chat_id="group@g.us",
        activation_epoch=1,
        lane="production",
        observed_revision=1,
        action="comment",
        intent="initiate",
        admission_id="adm-processing-only",
        source_event_ids=("source-1",),
        source_principals=(("source-1", "participant@s.whatsapp.net"),),
        payload_hash=canonical_hash(payload_to_mapping(payload)),
    )
    store.enqueue_participation_effect(
        EffectEnvelope(
            effect_id="processing-only-effect",
            operation_key="participation:processing-only",
            payload=payload,
            target=EffectTarget(channel="whatsapp", chat_id="group@g.us"),
            origin="participation",
            admission_id=admission.admission_id,
            created_ms=1000,
        ),
        admission,
    )
    store.close()

    assert _has_pending_participation_recovery(
        speakup_path=speakup_path,
        processing_path=processing_path,
    )

    config = Config.model_validate(
        {
            "processing": {
                "enabled": False,
                "db_path": str(processing_path),
                "participation": {"enabled": False},
            },
            "consciousness": {"enabled": False},
            "security": {"enabled": False},
        }
    )
    monkeypatch.setenv("YEOMAN_HOME", str(tmp_path))

    class _NeverProvider:
        async def chat(self, *_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("recovery startup must not call an LLM")

        def get_default_model(self) -> str:
            return "test/provider"

    runtime = build_gateway_runtime(
        config=config,
        provider=_NeverProvider(),  # type: ignore[arg-type]
        policy_engine=None,
        policy_path=None,
        workspace=tmp_path / "workspace",
        bus=MessageBus(),
    )
    try:
        assert runtime.processing is not None
        assert runtime.reconciliation is not None
        assert runtime.speakup_log is None
        assert not speakup_path.exists()
    finally:
        runtime.processing.close()
        runtime.inbound_archive.close()
        runtime.chat_registry.close()
        runtime.contacts.close()
        runtime.memory.close()


def test_pending_recovery_probe_fails_closed_on_corrupt_existing_ledger(
    tmp_path: Path,
) -> None:
    import sqlite3

    processing_path = tmp_path / "processing.db"
    processing_path.write_bytes(b"not sqlite")

    with pytest.raises(sqlite3.DatabaseError):
        _has_pending_participation_recovery(
            speakup_path=tmp_path / "missing-speakups.db",
            processing_path=processing_path,
        )


@pytest.mark.asyncio
async def test_receipts_reconcile_with_learning_and_consciousness_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from yeoman_gateway.consciousness.log import SpeakupLog
    from yeoman_gateway.processing.models import (
        EffectEnvelope,
        EffectEvidence,
        EffectTarget,
        TextPayload,
        canonical_hash,
        payload_to_mapping,
    )
    from yeoman_gateway.processing.participation_runtime import ParticipationAdmission

    processing_path = tmp_path / "processing.db"
    speakup_path = tmp_path / "data" / "consciousness" / "speakups.db"
    config = Config.model_validate(
        {
            "processing": {
                "enabled": False,
                "db_path": str(processing_path),
                "participation": {"enabled": False},
            },
            "consciousness": {"enabled": False},
            "security": {"enabled": False},
        }
    )
    log = SpeakupLog(speakup_path)
    store = ProcessingStore(processing_path)
    await log.reserve_delivery(
        proposal_id="recovery-opportunity",
        effect_id="recovery-effect",
        channel="whatsapp",
        chat_id="group@g.us",
        now_ms=1000,
        limits=(("comment", 1, 60_000),),
        origin="participation",
        lane="production",
    )
    await log.record_send_attempt(
        "recovery-opportunity", effect_id="recovery-effect", now_ms=1001
    )
    payload = TextPayload(text="already handed to transport")
    admission = ParticipationAdmission(
        opportunity_id="recovery-opportunity",
        channel="whatsapp",
        chat_id="group@g.us",
        activation_epoch=1,
        lane="production",
        observed_revision=1,
        action="comment",
        intent="initiate",
        admission_id="adm-recovery-effect",
        source_event_ids=("source-1",),
        source_principals=(("source-1", "participant@s.whatsapp.net"),),
        payload_hash=canonical_hash(payload_to_mapping(payload)),
    )
    store.enqueue_participation_effect(
        EffectEnvelope(
            effect_id="recovery-effect",
            operation_key="participation:recovery-opportunity",
            payload=payload,
            target=EffectTarget(channel="whatsapp", chat_id="group@g.us"),
            origin="participation",
            admission_id=admission.admission_id,
            created_ms=1002,
        ),
        admission,
    )
    assert store.claim_effect("recovery-effect", "worker", 1002, 30_000)
    assert store.transition(
        effect_id="recovery-effect",
        expected="executing",
        target="sent",
        now_ms=1002,
        worker_id="worker",
        evidence=EffectEvidence(kind="transport_receipt", detail="accepted"),
    )
    store.record_transport_receipt(
        "recovery-effect",
        channel="whatsapp",
        chat_id="group@g.us",
        provider_message_id="provider-1",
        now_ms=1003,
    )
    store.close()
    log.close()

    assert _has_pending_participation_recovery(
        speakup_path=speakup_path, processing_path=processing_path
    )

    class _NeverProvider:
        async def chat(self, *_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("recovery startup must not call an LLM")

        def get_default_model(self) -> str:
            return "test/provider"

    class _Idle:
        def __init__(self) -> None:
            self.tools: dict[str, object] = {}

        async def start(self) -> None:
            return None

        async def run(self) -> None:
            return None

        async def start_all(self) -> None:
            return None

        async def aclose(self) -> None:
            return None

        async def stop_all(self) -> None:
            return None

        def stop(self) -> None:
            return None

        def close(self) -> None:
            return None

    async def _run_once(expected_state: str) -> None:
        monkeypatch.setenv("YEOMAN_HOME", str(tmp_path))
        runtime = build_gateway_runtime(
            config=config,
            provider=_NeverProvider(),  # type: ignore[arg-type]
            policy_engine=None,
            policy_path=None,
            workspace=tmp_path / "workspace",
            bus=MessageBus(),
        )
        assert runtime.processing is not None
        assert runtime.speakup_log is not None
        assert runtime.reconciliation is not None
        assert runtime.consciousness is None
        assert runtime.opportunity_scheduler is None
        assert runtime.participation_maintenance is None

        class _Channels(_Idle):
            async def start_all(self) -> None:
                assert await runtime.speakup_log.delivery_state(
                    proposal_id="recovery-opportunity",
                    effect_id="recovery-effect",
                ) == expected_state

        class _Socket(_Idle):
            async def start(self) -> None:
                assert await runtime.speakup_log.delivery_state(
                    proposal_id="recovery-opportunity",
                    effect_id="recovery-effect",
                ) == expected_state

            async def stop(self) -> None:
                return None

        idle = _Idle()
        runtime.orchestrator = idle  # type: ignore[assignment]
        runtime.channels = _Channels()  # type: ignore[assignment]
        runtime.cron = idle  # type: ignore[assignment]
        runtime.heartbeat = idle  # type: ignore[assignment]
        runtime.bus = None
        runtime.gateway_socket = _Socket()  # type: ignore[assignment]
        runtime.shared_facts = None
        runtime.startup_hook = None
        await runtime.run()
        assert runtime.reconciliation.running is False

    await _run_once("transport_accepted")

    store = ProcessingStore(processing_path)
    store.append_event(
        event_key="whatsapp:group@g.us:receipt:provider-1",
        event_id="recipient-receipt-1",
        trace_id="recovery-effect",
        payload={
            "kind": "receipt",
            "channel": "whatsapp",
            "chat_id": "group@g.us",
            "principal": "participant@s.whatsapp.net",
            "target_message_id": "provider-1",
            "status": "delivered",
            "occurred_ms": 2000,
        },
        now_ms=2000,
    )
    store.close()
    await _run_once("delivered")

    final_log = SpeakupLog(speakup_path)
    assert await final_log.delivery_state(
        proposal_id="recovery-opportunity", effect_id="recovery-effect"
    ) == "delivered"
    final_log.close()

    log_only_home = tmp_path / "log-only-home"
    log_only_processing = log_only_home / "missing-processing.db"
    log_only_speakups = log_only_home / "data" / "consciousness" / "speakups.db"
    log_only_log = SpeakupLog(log_only_speakups)
    assert await log_only_log.reserve_delivery(
        proposal_id="orphaned-unsubmitted",
        effect_id="orphaned-effect",
        channel="whatsapp",
        chat_id="group@g.us",
        now_ms=1,
        limits=(("comment", 1, 60_000),),
        origin="participation",
        lane="production",
    )
    log_only_log.close()
    log_only_config = config.model_copy(deep=True)
    log_only_config.processing.db_path = str(log_only_processing)
    monkeypatch.setenv("YEOMAN_HOME", str(log_only_home))
    assert _has_pending_participation_recovery(
        speakup_path=log_only_speakups,
        processing_path=log_only_processing,
    )
    log_only_runtime = build_gateway_runtime(
        config=log_only_config,
        provider=_NeverProvider(),  # type: ignore[arg-type]
        policy_engine=None,
        policy_path=None,
        workspace=log_only_home / "workspace",
        bus=MessageBus(),
    )
    assert log_only_runtime.processing is None
    assert log_only_runtime.speakup_log is not None
    assert log_only_runtime.reconciliation is not None
    idle = _Idle()
    log_only_runtime.orchestrator = idle  # type: ignore[assignment]
    log_only_runtime.channels = idle  # type: ignore[assignment]
    log_only_runtime.cron = idle  # type: ignore[assignment]
    log_only_runtime.heartbeat = idle  # type: ignore[assignment]
    log_only_runtime.bus = None
    log_only_runtime.gateway_socket = None
    log_only_runtime.shared_facts = None
    log_only_runtime.startup_hook = None
    await log_only_runtime.run()
    assert not log_only_processing.exists()
    reopened_log_only = SpeakupLog(log_only_speakups)
    assert await reopened_log_only.delivery_state(
        proposal_id="orphaned-unsubmitted", effect_id="orphaned-effect"
    ) == "expired"
    reopened_log_only.close()

    empty_processing = tmp_path / "empty-processing.db"
    empty_home = tmp_path / "empty-home"
    empty_speakups = empty_home / "data" / "consciousness" / "speakups.db"
    empty_config = config.model_copy(deep=True)
    empty_config.processing.db_path = str(empty_processing)
    monkeypatch.setenv("YEOMAN_HOME", str(empty_home))
    assert not _has_pending_participation_recovery(
        speakup_path=empty_speakups, processing_path=empty_processing
    )
    empty_runtime = build_gateway_runtime(
        config=empty_config,
        provider=_NeverProvider(),  # type: ignore[arg-type]
        policy_engine=None,
        policy_path=None,
        workspace=empty_home / "workspace",
        bus=MessageBus(),
    )
    assert empty_runtime.processing is None
    assert empty_runtime.speakup_log is None
    assert empty_runtime.reconciliation is None
    idle = _Idle()
    empty_runtime.orchestrator = idle  # type: ignore[assignment]
    empty_runtime.channels = idle  # type: ignore[assignment]
    empty_runtime.cron = idle  # type: ignore[assignment]
    empty_runtime.heartbeat = idle  # type: ignore[assignment]
    empty_runtime.bus = None
    empty_runtime.gateway_socket = None
    empty_runtime.shared_facts = None
    empty_runtime.startup_hook = None
    await empty_runtime.run()
    assert not empty_processing.exists()
    assert not empty_speakups.exists()


@pytest.mark.asyncio
async def test_admin_terminal_dispatch_finishes_exact_overlapping_direct_bindings(
    tmp_path: Path,
) -> None:
    """A deterministic early-return path releases only after its own dispatch completes."""

    class _TerminalAdmin:
        async def handle(self, _event: object) -> list[object]:
            return []

    store = ProcessingStore(tmp_path / "processing.db")
    released: list[tuple[str, str]] = []
    service = OrchestratorService(
        bus=MessageBus(),
        orchestrator=_TerminalAdmin(),
        typing_adapter=SimpleNamespace(),
        telemetry=SimpleNamespace(),
        memory=SimpleNamespace(),
        processing_store=store,
        release_participation_chat=lambda channel, chat_id: released.append(
            (channel, chat_id)
        ),
    )
    first = SimpleNamespace(
        channel="whatsapp", chat_id="group@g.us", sender_id="owner", message_id="direct-1"
    )
    second = SimpleNamespace(
        channel="whatsapp", chat_id="group@g.us", sender_id="owner", message_id="direct-2"
    )
    store.note_direct_admission("whatsapp", "group@g.us", "direct-1", "turn-1", now_ms=1)
    store.note_direct_admission("whatsapp", "group@g.us", "direct-2", "turn-2", now_ms=2)

    try:
        await service._process_message(first)  # noqa: SLF001
        assert store.direct_work_active("whatsapp", "group@g.us")
        assert released == []

        await service._process_message(second)  # noqa: SLF001
        assert not store.direct_work_active("whatsapp", "group@g.us")
        assert released == [("whatsapp", "group@g.us")]
    finally:
        store.close()


def test_restart_activation_transition_new_offers_use_new_epoch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real builder must offer with the post-restart epoch and live lane."""
    from yeoman_gateway.adapters.policy_engine import EnginePolicyAdapter
    from yeoman_gateway.consciousness.log import SpeakupLog
    from yeoman_gateway.consciousness.participation_runtime import (
        ActivationEpochTracker,
        SourceOwner,
    )
    from yeoman_gateway.storage.inbound_archive import InboundArchive

    class RouteClient:
        def __init__(self, **_: object) -> None:
            pass

    monkeypatch.setattr(
        "yeoman_gateway.processing.model_route.RouteClient", RouteClient
    )
    chat_id = "group@g.us"
    db_path = tmp_path / "speakups.db"
    old_log = SpeakupLog(db_path)
    try:
        old_tracker = ActivationEpochTracker(store=old_log)
        assert old_tracker.observe(
            channel="whatsapp",
            chat_id=chat_id,
            enabled=True,
            shadow=True,
            judge_route="participation.judge",
        ) == 1
    finally:
        old_log.close()

    config = Config.model_validate(
        {
            "processing": {
                "enabled": True,
                "chats": [f"whatsapp:{chat_id}"],
                "participation": {
                    "enabled": True,
                    "shadow": False,
                    "judgeRoute": "participation.judge",
                    "contextWindowMinutes": 5,
                    "contextMaxMessages": 5,
                },
            }
        }
    )
    engine = PolicyEngine(
        PolicyConfig.model_validate(
            {
                "channels": {
                    "whatsapp": {
                        "chats": {
                            chat_id: {
                                "whoCanTalk": {
                                    "mode": "allowlist",
                                    "senders": ["allowed@s.whatsapp.net"],
                                },
                                "whenToReply": {"mode": "mention_only"},
                                "spontaneity": {
                                    "enabled": True,
                                    "dailyCap": 1,
                                    "allowedActions": ["observation"],
                                },
                                "participation": {"enabled": True},
                            }
                        }
                    }
                }
            }
        ),
        workspace=tmp_path,
    )
    log = SpeakupLog(db_path)
    archive = InboundArchive(tmp_path / "inbound.db")
    processing_store = ProcessingStore(tmp_path / "processing.db")
    tracker = ActivationEpochTracker(store=log)
    adapter = EnginePolicyAdapter(
        engine=engine,
        known_tools=set(),
        policy_path=None,
        workspace=tmp_path,
        processing_config=config.processing,
        activation_tracker=tracker,
    )

    class UnprovenReactionResponder:
        async def react_to_participation(self, **kwargs: object) -> object:
            del kwargs
            raise AssertionError("unproven reaction sender must not be composed")

    try:
        assert tracker.refresh_activation_sync() == 2
        runtime, scheduler, decision = _build_participation_runtime(
            config=config,
            source_owner=SourceOwner(store=log),
            log=log,
            policy_engine=engine,
            inbound_archive=archive,
            processing_store=processing_store,
            responder=UnprovenReactionResponder(),
            policy_adapter=adapter,
        )
        assert runtime is not None
        activation = runtime.current_activation("whatsapp", chat_id)
        assert activation is not None
        assert activation.live is True
        assert activation.activation_epoch == log.activation_epoch_sync("participation")
        assert runtime.offer_source(
            channel="whatsapp",
            chat_id=chat_id,
            source_event_ids=("m-live",),
            observed_revision=1,
            trigger="inbound",
        )
        pending = scheduler._pending[("whatsapp", chat_id)]  # noqa: SLF001
        assert pending.activation_epoch == log.activation_epoch_sync("participation")
        assert pending.lane == "production"

        decision_runtime, _ = decision
        assert decision_runtime._reactor is None  # noqa: SLF001
        snapshot = decision_runtime._snapshot_provider(  # noqa: SLF001
            "whatsapp",
            chat_id,
            epoch=pending.activation_epoch,
            opportunity=pending,
        )
        assert snapshot["context_window_minutes"] == 5
        assert snapshot["context_max_messages"] == 5
        assert snapshot["context_revision"] == 1
        assert snapshot["current_source_ids"] == ("m-live",)
        assert snapshot["max_reevaluations"] == 1
        assert snapshot["opportunity_ttl_seconds"] == 120
        assert snapshot["allow_initiation"] is True
        assert snapshot["allow_continuation"] is True
        assert snapshot["allow_reactions"] is True
        assert snapshot["spontaneity_enabled"] is True
        assert snapshot["spontaneity_allowed_actions"] == ("observation",)
        assert snapshot["reply_action"] == "answer"
        assert snapshot["arbitration_revision"] == 0
        assert snapshot["policy_hash"]
        assert not decision_runtime._direct_work_active("whatsapp", chat_id)  # noqa: SLF001
        assert processing_store.note_direct_admission(
            "whatsapp", chat_id, "direct-1", "turn-direct-1", now_ms=1
        ) == 1
        assert decision_runtime._direct_work_active("whatsapp", chat_id)  # noqa: SLF001
        assert decision_runtime._snapshot_provider(  # noqa: SLF001
            "whatsapp", chat_id, epoch=pending.activation_epoch, opportunity=pending
        )["arbitration_revision"] == 1
        source_authorizer = decision_runtime._context_builder._source_authorizer  # noqa: SLF001
        assert source_authorizer is not None
        assert source_authorizer(
            {
                "channel": "whatsapp",
                "chat_id": chat_id,
                "sender_id": "allowed@s.whatsapp.net",
            }
        )
        assert not source_authorizer(
            {
                "channel": "whatsapp",
                "chat_id": chat_id,
                "sender_id": "blocked@s.whatsapp.net",
            }
        )
    finally:
        processing_store.close()
        archive.close()
        log.close()


@pytest.mark.asyncio
async def test_whatsapp_debounce_preserves_all_participation_source_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The production channel must not reduce a debounced batch to its last id."""
    monkeypatch.setenv("YEOMAN_HOME", str(tmp_path))
    bus = MessageBus()
    channel = WhatsAppChannel(
        WhatsAppConfig(debounce_ms=1, debounce_media_ms=1),
        bus,
    )

    def event(message_id: str, text: str) -> InboundEvent:
        return InboundEvent(
            message_id=message_id,
            chat_jid="group@g.us",
            participant_jid="person@s.whatsapp.net",
            sender_id="person",
            sender_phone_jid=None,
            is_group=True,
            text=text,
            timestamp=1,
            mentioned_jids=[],
            mentioned_bot=False,
            reply_to_bot=False,
            reply_to_message_id=None,
            reply_to_participant=None,
            reply_to_text=None,
            media_kind=None,
            media_type=None,
            media_file_name=None,
            media_path=None,
            media_bytes=None,
            media_description=None,
            voice_transcript=None,
        )

    key = "group@g.us:person"
    channel._debounce_buffers[key] = [event("m1", "one"), event("m2", "two")]  # noqa: SLF001
    channel._debounce_delays[key] = 0  # noqa: SLF001
    await channel._flush_debounce_bucket(key, 0)  # noqa: SLF001

    observed = bus._event_queue.get_nowait()  # noqa: SLF001
    assert observed.source_event_ids == ("m1", "m2")


@pytest.mark.asyncio
async def test_bootstrap_reaction_reaches_transport_with_reserved_effect_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real wrapper and managed effect path preserve one reaction identity."""
    from datetime import UTC, datetime

    from yeoman_gateway.adapters.policy_engine import EnginePolicyAdapter
    from yeoman_gateway.adapters.responder_llm import LLMResponder
    from yeoman_gateway.app.bootstrap import (
        build_thread_registry,
        build_thread_responder,
    )
    from yeoman_gateway.consciousness.log import (
        SpeakupLog,
        deterministic_effect_id,
    )
    from yeoman_gateway.consciousness.participation_runtime import SourceOwner
    from yeoman_gateway.processing.dispatch import ServiceEffectProducer
    from yeoman_gateway.processing.models import ReactionPayload
    from yeoman_gateway.processing.participation import ParticipationOpportunity
    from yeoman_gateway.storage.inbound_archive import InboundArchive

    class RouteClient:
        route_key = "participation.judge"

        def __init__(self, **_: object) -> None:
            pass

        async def chat(self, messages: object, *, max_tokens: int = 0) -> str:
            del messages, max_tokens
            return (
                '{"action":"react","intent":"continue","reason":"ack",'
                '"emoji":"👍","target_message_id":"source-1"}'
            )

    class Provider:
        calls = 0

        def get_default_model(self) -> str:
            return "unused"

        async def chat(self, *args: object, **kwargs: object) -> object:
            del args, kwargs
            self.calls += 1
            raise AssertionError("a reaction must not generate text")

    monkeypatch.setattr(
        "yeoman_gateway.processing.model_route.RouteClient", RouteClient
    )
    monkeypatch.setenv("YEOMAN_HOME", str(tmp_path))
    chat_id = "group@g.us"
    sender = "person@s.whatsapp.net"
    config = Config.model_validate(
        {
            "processing": {
                "enabled": True,
                "chats": [f"whatsapp:{chat_id}"],
                "participation": {
                    "enabled": True,
                    "shadow": False,
                    "judgeRoute": "participation.judge",
                },
                "reply_actions": {f"whatsapp:{chat_id}": "react"},
            }
        }
    )
    engine = PolicyEngine(
        PolicyConfig.model_validate(
            {
                "defaults": {
                    "allowedTools": {"mode": "allowlist", "tools": ["message"]}
                },
                "channels": {
                    "whatsapp": {
                        "chats": {
                            chat_id: {
                                "whoCanTalk": {
                                    "mode": "allowlist",
                                    "senders": [sender, "service:speakup"],
                                },
                                "whenToReply": {"mode": "all"},
                                "spontaneity": {
                                    "enabled": True,
                                    "dailyCap": 1,
                                    "allowedActions": ["observation"],
                                },
                                "participation": {"enabled": True},
                            }
                        }
                    }
                },
            }
        ),
        workspace=tmp_path,
    )
    from yeoman_gateway.consciousness.participation_runtime import ActivationEpochTracker

    log = SpeakupLog(tmp_path / "speakups.db")
    archive = InboundArchive(tmp_path / "inbound.db")
    store = ProcessingStore(tmp_path / "processing.db")
    adapter = EnginePolicyAdapter(
        engine=engine,
        known_tools={"message"},
        policy_path=None,
        workspace=tmp_path,
        processing_config=config.processing,
        activation_tracker=ActivationEpochTracker(store=log),
    )
    bus = MessageBus()
    router = build_effect_router(
        config,
        adapter,
        store,
        bus,
        participation_ledger=log,
        inbound_archive=archive,
    )
    assert router is not None
    reaction_calls: list[object] = []

    async def reject_text(message: object) -> None:
        del message
        raise AssertionError("a reaction must not send text")

    async def record_reaction(message: object) -> dict[str, str]:
        reaction_calls.append(message)
        return {"provider_message_id": "provider-reaction-1"}

    router.set_direct_transport(reject_text, record_reaction)
    provider = Provider()
    inner = LLMResponder(
        provider=provider,  # type: ignore[arg-type]
        workspace=tmp_path,
        bus=bus,
        service_effects=ServiceEffectProducer(router=router, bus=bus),
    )
    threads = build_thread_registry(config, store)
    responder = build_thread_responder(
        config, store, threads, inner, adapter, router
    )
    assert responder is not None
    now = datetime.now(UTC)
    archive.record_inbound(
        channel="whatsapp",
        chat_id=chat_id,
        message_id="source-1",
        participant=sender,
        sender_id=sender,
        sender_name="person",
        text="nice",
        timestamp=int(now.timestamp()),
    )
    revision = log.ensure_source_revisions_sync(
        channel="whatsapp", chat_id=chat_id, source_ids=("source-1",)
    )[0][1]
    activation = adapter.current_activation("whatsapp", chat_id)
    assert activation is not None and activation.live
    _runtime, _scheduler, decision = _build_participation_runtime(
        config=config,
        source_owner=SourceOwner(store=log),
        log=log,
        policy_engine=engine,
        inbound_archive=archive,
        processing_store=store,
        responder=responder,
        policy_adapter=adapter,
    )
    decision_runtime, _reconciler = decision
    opportunity = ParticipationOpportunity(
        opportunity_id="opportunity-reaction-1",
        channel="whatsapp",
        chat_id=chat_id,
        trigger="inbound",
        source_event_ids=("source-1",),
        observed_revision=revision,
        activation_epoch=activation.activation_epoch,
        created_at_ms=int(now.timestamp() * 1000),
    )

    try:
        result = await decision_runtime.evaluate_participation(opportunity)

        effect_id = deterministic_effect_id(
            channel="whatsapp",
            chat_id=chat_id,
            operation="reaction",
            proposal_id=opportunity.opportunity_id,
        )
        assert result == {"status": "reaction_submitted", "effect_id": effect_id}
        assert len(reaction_calls) == 1
        sent = reaction_calls[0]
        assert getattr(sent, "message_id") == "source-1"
        assert getattr(sent, "metadata")["processing_effect"] == effect_id
        stored = store.get_effect(effect_id)
        assert stored is not None and stored.effect_id == effect_id
        assert isinstance(stored.payload, ReactionPayload)
        assert stored.payload.message_id == "source-1"
        assert provider.calls == 0
        assert await log.delivery_state(
            proposal_id=opportunity.opportunity_id, effect_id=effect_id
        ) == "transport_accepted"
    finally:
        archive.close()
        log.close()
        store.close()


@pytest.mark.asyncio
async def test_participation_approval_rechecks_pause_and_submits_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Approval returns through the production submission and final authorizer."""
    from datetime import UTC, datetime

    from yeoman_gateway.adapters.policy_engine import EnginePolicyAdapter
    from yeoman_gateway.adapters.responder_llm import LLMResponder
    from yeoman_gateway.app.bootstrap import build_thread_registry, build_thread_responder
    from yeoman_gateway.consciousness.approval import SpeakupApprovalStore
    from yeoman_gateway.consciousness.log import SpeakupLog, deterministic_effect_id
    from yeoman_gateway.consciousness.participation_runtime import (
        ActivationEpochTracker,
        SourceOwner,
    )
    from yeoman_gateway.consciousness.tools import ConsciousnessTools
    from yeoman_gateway.core.models import InboundEvent as CoreInboundEvent
    from yeoman_gateway.core.models import PolicyDecision
    from yeoman_gateway.core.pipeline import PipelineContext
    from yeoman_gateway.pipeline.speakup_approval import SpeakupApprovalMiddleware
    from yeoman_gateway.processing.dispatch import ServiceEffectProducer
    from yeoman_gateway.processing.participation import ParticipationOpportunity
    from yeoman_gateway.providers.base import LLMResponse
    from yeoman_gateway.security import NoopSecurity
    from yeoman_gateway.storage.inbound_archive import InboundArchive

    class RouteClient:
        route_key = "participation.judge"
        calls = 0

        def __init__(self, **_: object) -> None:
            pass

        async def chat(self, messages: object, *, max_tokens: int = 0) -> str:
            del max_tokens
            type(self).calls += 1
            source_id = "source-2" if "source-2" in str(messages) else "source-1"
            return (
                '{"action":"comment","intent":"initiate","reason":"useful",'
                f'"evidence_ids":["{source_id}"],"target_message_id":"{source_id}",'
                '"contribution_type":"observation","purpose":"brief answer"}'
            )

    class Provider:
        calls = 0

        def get_default_model(self) -> str:
            return "unused"

        async def chat(self, *args: object, **kwargs: object) -> LLMResponse:
            del args, kwargs
            self.calls += 1
            return LLMResponse(content=f"approved draft {self.calls}")

    monkeypatch.setattr(
        "yeoman_gateway.processing.model_route.RouteClient", RouteClient
    )
    monkeypatch.setenv("YEOMAN_HOME", str(tmp_path))
    chat_id = "group@g.us"
    sender = "person@s.whatsapp.net"
    owner = "owner@s.whatsapp.net"
    config = Config.model_validate(
        {
            "consciousness": {
                "enabled": False,
                "defaultDailyCap": 2,
                "approvalTimeoutSeconds": 3600,
            },
            "processing": {
                "enabled": True,
                "chats": [f"whatsapp:{chat_id}"],
                "participation": {
                    "enabled": True,
                    "shadow": False,
                    "judgeRoute": "participation.judge",
                },
                "reply_actions": {f"whatsapp:{chat_id}": "answer"},
            },
        }
    )
    engine = PolicyEngine(
        PolicyConfig.model_validate(
            {
                "owners": {"whatsapp": [owner]},
                "channels": {
                    "whatsapp": {
                        "chats": {
                            chat_id: {
                                "whoCanTalk": {
                                    "mode": "allowlist",
                                    "senders": [sender, "service:speakup"],
                                },
                                "whenToReply": {"mode": "all"},
                                "spontaneity": {
                                    "enabled": True,
                                    "dailyCap": 2,
                                    "allowedActions": ["observation"],
                                    "preview": "owner_dm",
                                },
                                "participation": {
                                    "enabled": True,
                                    "minUnaddressedJudgeGapSeconds": 0,
                                },
                            }
                        }
                    }
                },
            }
        ),
        workspace=tmp_path,
    )
    log = SpeakupLog(tmp_path / "speakups.db")
    archive = InboundArchive(tmp_path / "inbound.db")
    store = ProcessingStore(tmp_path / "processing.db")
    approval_store = SpeakupApprovalStore(tmp_path / "legacy-approvals.json")
    adapter = EnginePolicyAdapter(
        engine=engine,
        known_tools={"message"},
        policy_path=None,
        workspace=tmp_path,
        processing_config=config.processing,
        activation_tracker=ActivationEpochTracker(store=log),
    )
    bus = MessageBus()
    router = build_effect_router(
        config,
        adapter,
        store,
        bus,
        participation_ledger=log,
        inbound_archive=archive,
    )
    assert router is not None
    target_calls: list[object] = []

    async def record_text(message: object) -> dict[str, str]:
        target_calls.append(message)
        return {"provider_message_id": f"provider-{len(target_calls)}"}

    async def reject_reaction(message: object) -> None:
        del message
        raise AssertionError("approval must submit text")

    router.set_direct_transport(record_text, reject_reaction)
    effects = ServiceEffectProducer(router=router, bus=bus)
    provider = Provider()
    responder = LLMResponder(
        provider=provider,  # type: ignore[arg-type]
        workspace=tmp_path,
        bus=bus,
        service_effects=effects,
    )
    responder = build_thread_responder(
        config,
        store,
        build_thread_registry(config, store),
        responder,
        adapter,
        router,
    )
    assert responder is not None
    tools = ConsciousnessTools(
        config=config,
        policy_engine=engine,
        bus=bus,
        log=log,
        inbound_archive=archive,
        memory=None,
        security=NoopSecurity(),
        approval_store=approval_store,
        service_effects=effects,
        activation_provider=adapter.current_activation,
    )
    runtime, _scheduler, decision = _build_participation_runtime(
        config=config,
        source_owner=SourceOwner(store=log),
        log=log,
        policy_engine=engine,
        inbound_archive=archive,
        processing_store=store,
        responder=responder,
        policy_adapter=adapter,
        approval_tools=tools,
    )
    del runtime, _scheduler
    decision_runtime, _reconciler = decision
    middleware = SpeakupApprovalMiddleware(
        approval_store=approval_store,
        bus=bus,
        log=log,
        security=tools.security,
        tools=tools,
    )

    async def stage(index: int) -> tuple[ParticipationOpportunity, str]:
        timestamp = datetime.now(UTC)
        source_id = f"source-{index}"
        archive.record_inbound(
            channel="whatsapp",
            chat_id=chat_id,
            message_id=source_id,
            participant=sender,
            sender_id=sender,
            sender_name="person",
            text=f"synthetic source {index}",
            timestamp=int(timestamp.timestamp()),
        )
        revision = log.ensure_source_revisions_sync(
            channel="whatsapp", chat_id=chat_id, source_ids=(source_id,)
        )[0][1]
        activation = adapter.current_activation("whatsapp", chat_id)
        assert activation is not None and activation.live
        opportunity = ParticipationOpportunity(
            opportunity_id=f"approval-opportunity-{index}",
            channel="whatsapp",
            chat_id=chat_id,
            trigger="inbound",
            source_event_ids=(source_id,),
            observed_revision=revision,
            activation_epoch=activation.activation_epoch,
            created_at_ms=int(timestamp.timestamp() * 1000),
        )
        result = await decision_runtime.evaluate_participation(opportunity)
        assert result["status"] == "awaiting_approval", result
        preview = await bus.consume_outbound()
        code = preview.content.split("Approve: ", 1)[1].splitlines()[0].strip()
        assert preview.chat_id == owner
        assert await approval_store.list_pending() == []
        return opportunity, code

    def owner_context(code: str, *, identity: str = owner) -> PipelineContext:
        ctx = PipelineContext(
            event=CoreInboundEvent(
                channel="whatsapp",
                sender_id=identity,
                chat_id=identity,
                content=code,
                timestamp=datetime.now(UTC),
                participant=identity,
            )
        )
        ctx.decision = PolicyDecision(
            accept_message=True,
            should_respond=True,
            allowed_tools=frozenset(),
            reason="test",
            is_owner=True,
        )
        return ctx

    async def unused_next(ctx: PipelineContext) -> None:
        del ctx
        raise AssertionError("an authenticated approval code must halt the pipeline")

    try:
        first, first_code = await stage(1)
        await middleware(
            owner_context(first_code, identity="intruder@s.whatsapp.net"),
            unused_next,
        )
        assert await log.approval_claim(first.opportunity_id) is None
        assert target_calls == []
        adapter._set_chat_pause(  # noqa: SLF001
            channel="whatsapp", chat_id=chat_id, until_ms=2**63 - 1
        )
        await middleware(owner_context(first_code), unused_next)
        assert target_calls == []
        first_effect_id = deterministic_effect_id(
            channel="whatsapp",
            chat_id=chat_id,
            operation="comment",
            proposal_id=first.opportunity_id,
        )
        assert await log.delivery_state(
            proposal_id=first.opportunity_id, effect_id=first_effect_id
        ) == "failed"

        adapter._clear_chat_pause(channel="whatsapp", chat_id=chat_id)  # noqa: SLF001
        second, second_code = await stage(2)
        await middleware(owner_context(second_code), unused_next)
        await middleware(owner_context(second_code), unused_next)

        assert len(target_calls) == 1
        second_effect_id = deterministic_effect_id(
            channel="whatsapp",
            chat_id=chat_id,
            operation="comment",
            proposal_id=second.opportunity_id,
        )
        sent = store.get_effect(second_effect_id)
        assert sent is not None and sent.state == "sent"
        assert sent.admission_id
        assert await log.delivery_state(
            proposal_id=second.opportunity_id, effect_id=second_effect_id
        ) == "transport_accepted"
        assert provider.calls == 2
    finally:
        archive.close()
        log.close()
        store.close()


def test_live_empty_or_rejected_offer_never_requests_legacy_fallback() -> None:
    """A failed offer is not an ownership transition back to the old planner."""

    class Policy:
        @staticmethod
        def current_activation(channel: str, chat_id: str) -> object:
            return type("Activation", (), {"live": True, "observing": False})()

    class Runtime:
        calls = 0

        def offer_source(self, **_: object) -> bool:
            self.calls += 1
            return False

    runtime = Runtime()
    assert _offer_participation_trigger(
        channel="whatsapp",
        chat_id="group@g.us",
        trigger="burst",
        runtime=None,
        policy_adapter=Policy(),
        material_provider=lambda channel, chat_id, source_ids: (("m0",), 6),
    ) == {"status": "skipped"}

    assert _offer_participation_trigger(
        channel="whatsapp",
        chat_id="group@g.us",
        trigger="burst",
        runtime=runtime,
        policy_adapter=Policy(),
        material_provider=lambda channel, chat_id, source_ids: ((), 7),
    ) == {"status": "skipped"}
    assert runtime.calls == 0

    assert _offer_participation_trigger(
        channel="whatsapp",
        chat_id="group@g.us",
        trigger="burst",
        runtime=runtime,
        policy_adapter=Policy(),
        material_provider=lambda channel, chat_id, source_ids: (("m1",), 8),
    ) == {"status": "skipped"}
    assert runtime.calls == 1

    class PausedPolicy(Policy):
        @staticmethod
        def participation_pause_reason(channel: str, chat_id: str) -> str:
            return "paused_global"

    material_calls = 0

    def paused_material(
        channel: str, chat_id: str, source_ids: tuple[str, ...] | None
    ) -> tuple[tuple[str, ...], int]:
        nonlocal material_calls
        material_calls += 1
        return ("m2",), 9

    assert _offer_participation_trigger(
        channel="whatsapp",
        chat_id="group@g.us",
        trigger="burst",
        runtime=runtime,
        policy_adapter=PausedPolicy(),
        material_provider=paused_material,
    ) == {"status": "skipped"}
    assert material_calls == 0
    assert runtime.calls == 1


@pytest.mark.asyncio
async def test_participation_burst_observer_runs_without_legacy_consciousness(
    tmp_path: Path,
) -> None:
    from yeoman_gateway.consciousness.burst import BurstObserver

    calls: list[tuple[str, str]] = []
    config = Config.model_validate(
        {
            "consciousness": {
                "enabled": False,
                "burstEnabled": True,
                "burstThresholdMessages": 2,
            },
            "processing": {
                "enabled": True,
                "chats": ["whatsapp:group@g.us"],
                "participation": {
                    "enabled": True,
                    "judgeRoute": "participation.judge",
                },
            },
        }
    )
    observer = BurstObserver(
        config=config,
        state_path=tmp_path / "burst.json",
        on_burst=lambda channel, chat_id: calls.append((channel, chat_id)),
        is_eligible=lambda channel, chat_id: True,
    )
    for timestamp, message_id in ((1.0, "m1"), (2.0, "m2")):
        await observer.handle(
            InboundObservedEvent(
                channel="whatsapp",
                chat_id="group@g.us",
                sender_id="person",
                content=message_id,
                timestamp=timestamp,
                message_id=message_id,
                source_event_ids=(message_id,),
                is_group=True,
            )
        )
    assert calls == [("whatsapp", "group@g.us")]


@pytest.mark.asyncio
async def test_real_effect_transport_denies_participation_after_pause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The production router reloads pause/epoch/source/reservation state at dispatch."""
    from yeoman_gateway.adapters.policy_engine import EnginePolicyAdapter
    from yeoman_gateway.consciousness.log import SpeakupLog
    from yeoman_gateway.consciousness.participation_runtime import ActivationEpochTracker
    from yeoman_gateway.processing.models import (
        EffectEnvelope,
        EffectTarget,
        TextPayload,
        payload_hash,
    )
    from yeoman_gateway.processing.participation_runtime import ParticipationAdmission
    from yeoman_gateway.storage.inbound_archive import InboundArchive

    monkeypatch.setenv("YEOMAN_HOME", str(tmp_path))
    chat_id = "group@g.us"
    sender = "person@s.whatsapp.net"
    config = Config.model_validate(
        {
            "consciousness": {"defaultDailyCap": 1},
            "processing": {
                "enabled": True,
                "chats": [f"whatsapp:{chat_id}"],
                "participation": {
                    "enabled": True,
                    "shadow": False,
                    "judgeRoute": "participation.judge",
                },
                "reply_actions": {f"whatsapp:{chat_id}": "answer"},
            },
        }
    )
    engine = PolicyEngine(
        PolicyConfig.model_validate(
            {
                "channels": {
                    "whatsapp": {
                        "chats": {
                            chat_id: {
                                "whoCanTalk": {
                                    "mode": "allowlist",
                                    "senders": [sender, "service:speakup"],
                                },
                                "whenToReply": {"mode": "all"},
                                "spontaneity": {
                                    "enabled": True,
                                    "dailyCap": 1,
                                    "allowedActions": ["observation"],
                                    "preview": "none",
                                },
                                "participation": {"enabled": True},
                            }
                        }
                    }
                }
            }
        ),
        workspace=tmp_path,
    )
    log = SpeakupLog(tmp_path / "speakups.db")
    archive = InboundArchive(tmp_path / "inbound.db")
    store = ProcessingStore(tmp_path / "processing.db")
    tracker = ActivationEpochTracker(store=log)
    adapter = EnginePolicyAdapter(
        engine=engine,
        known_tools=set(),
        policy_path=None,
        workspace=tmp_path,
        processing_config=config.processing,
        activation_tracker=tracker,
    )
    archive.record_inbound(
        channel="whatsapp",
        chat_id=chat_id,
        message_id="source-1",
        participant=sender,
        sender_id=sender,
        sender_name="person",
        text="What do you think?",
        timestamp=1,
    )
    try:
        activation = adapter.current_activation("whatsapp", chat_id)
        assert activation is not None and activation.live
        source_rows = log.ensure_source_revisions_sync(
            channel="whatsapp", chat_id=chat_id, source_ids=("source-1",)
        )
        observed_revision = source_rows[0][1]
        log.mark_material_considered_sync(
            channel="whatsapp",
            chat_id=chat_id,
            observed_revision=observed_revision,
            lane="production",
        )
        payload = TextPayload(text="A bound answer")
        effect_id = "participation-effect-1"
        admission = ParticipationAdmission(
            opportunity_id="opportunity-1",
            channel="whatsapp",
            chat_id=chat_id,
            activation_epoch=activation.activation_epoch,
            lane="production",
            observed_revision=observed_revision,
            action="comment",
            intent="initiate",
            admission_id="admission-1",
            source_event_ids=("source-1",),
            source_principals=(("source-1", sender),),
            policy_version=activation.policy_version,
            policy_hash=adapter.policy_snapshot().policy_hash,
            arbitration_revision=store.arbitration_revision("whatsapp", chat_id),
            contribution_type="observation",
            payload_hash=payload_hash(payload),
        )
        assert log.reserve_delivery_sync(
            proposal_id=admission.opportunity_id,
            effect_id=effect_id,
            channel="whatsapp",
            chat_id=chat_id,
            now_ms=1,
            limits=(("comment", 1, 60_000),),
            observed_revision=observed_revision,
            activation_epoch=activation.activation_epoch,
        )
        router = build_effect_router(
            config,
            adapter,
            store,
            MessageBus(),
            participation_ledger=log,
            inbound_archive=archive,
        )
        assert router is not None
        sent: list[object] = []

        async def transport(message: object) -> None:
            sent.append(message)

        router.set_direct_transport(transport, transport)
        envelope = EffectEnvelope(
            effect_id=effect_id,
            operation_key="participation:opportunity-1",
            payload=payload,
            target=EffectTarget(channel="whatsapp", chat_id=chat_id),
            principal="service:speakup",
            capability="send_text",
            origin="participation",
            admission_id=admission.admission_id,
            created_ms=1,
        )
        router._gateway.submit_participation(envelope, admission)  # noqa: SLF001
        await log.record_send_attempt(
            admission.opportunity_id, effect_id=effect_id, now_ms=2
        )
        before_pause = router._gateway._authorizer.check(envelope, None)  # noqa: SLF001
        assert before_pause.outcome == "allow", before_pause.reason

        adapter._set_chat_pause(  # noqa: SLF001
            channel="whatsapp", chat_id=chat_id, until_ms=2**63 - 1
        )
        receipt = await router._gateway.execute_ready(effect_id)  # noqa: SLF001
        assert receipt.state == "blocked"
        assert sent == []

        adapter._clear_chat_pause(channel="whatsapp", chat_id=chat_id)  # noqa: SLF001
        refreshed = adapter.current_activation("whatsapp", chat_id)
        assert refreshed is not None
        fresh_effect_id = "participation-effect-2"
        fresh_admission = replace(
            admission,
            opportunity_id="opportunity-2",
            admission_id="admission-2",
            activation_epoch=refreshed.activation_epoch,
            policy_version=refreshed.policy_version,
            policy_hash=adapter.policy_snapshot().policy_hash,
            arbitration_revision=store.arbitration_revision("whatsapp", chat_id),
        )
        assert log.reserve_delivery_sync(
            proposal_id=fresh_admission.opportunity_id,
            effect_id=fresh_effect_id,
            channel="whatsapp",
            chat_id=chat_id,
            now_ms=3,
            limits=(("comment", 2, 60_000),),
            observed_revision=observed_revision,
            activation_epoch=refreshed.activation_epoch,
        )
        fresh_envelope = replace(
            envelope,
            effect_id=fresh_effect_id,
            operation_key="participation:opportunity-2",
            admission_id=fresh_admission.admission_id,
        )
        router._gateway.submit_participation(fresh_envelope, fresh_admission)  # noqa: SLF001
        await log.record_send_attempt(
            fresh_admission.opportunity_id, effect_id=fresh_effect_id, now_ms=4
        )
        assert router._gateway._authorizer.check(fresh_envelope, None).outcome == "allow"  # noqa: SLF001
        store.note_direct_admission(
            "whatsapp", chat_id, "direct-source", "direct-turn", now_ms=5
        )
        stale = router._gateway._authorizer.check(fresh_envelope, None)  # noqa: SLF001
        assert stale.outcome == "deny"
        assert stale.reason == "participation_denied:source_not_authorized"
    finally:
        store.close()
        archive.close()
        log.close()
