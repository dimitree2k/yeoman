"""Application bootstrap and runtime wiring for the vNext orchestrator."""

from __future__ import annotations

import asyncio
import os
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, assert_never
from uuid import uuid4 as _uuid4

from loguru import logger
from yeoman_shared.telemetry import InMemoryTelemetry, tracing

from yeoman_gateway.a2a.registry import A2AWorkerRegistry
from yeoman_gateway.adapters.policy_engine import EnginePolicyAdapter
from yeoman_gateway.adapters.reply_archive_sqlite import SqliteReplyArchiveAdapter
from yeoman_gateway.adapters.responder_llm import LLMResponder
from yeoman_gateway.adapters.typing_channel_manager import ChannelManagerTypingAdapter
from yeoman_gateway.agent.tools.file_access import build_file_access_resolver
from yeoman_gateway.bus.events import InboundMessage, OutboundMessage, ReactionMessage
from yeoman_gateway.bus.queue import MessageBus
from yeoman_gateway.channels.manager import ChannelManager
from yeoman_gateway.contacts.service import ContactsService
from yeoman_gateway.core.intents import (
    OrchestratorIntent,
    PersistSessionIntent,
    QueueMemoryNotesCaptureIntent,
    RecordManualMemoryIntent,
    RecordMetricIntent,
    SendOutboundIntent,
    SendReactionIntent,
    SetTypingIntent,
)
from yeoman_gateway.core.models import InboundEvent
from yeoman_gateway.core.orchestrator import Orchestrator
from yeoman_gateway.cron.service import CronJobDeferredError, CronJobSkippedError, CronService
from yeoman_gateway.cron.types import CronJob
from yeoman_gateway.cron.voice import evaluate_voice_quiet_gate
from yeoman_gateway.heartbeat.service import HeartbeatService
from yeoman_gateway.media.document_cache import DocumentCache
from yeoman_gateway.media.document_processing import DocumentProcessor
from yeoman_gateway.media.lazy_resolver import LazyMediaResolver
from yeoman_gateway.media.router import ModelRouter
from yeoman_gateway.media.storage import MediaStorage
from yeoman_gateway.media.tts import TTSSynthesizer
from yeoman_gateway.media.vision import VisionDescriber
from yeoman_gateway.memory import MemoryService
from yeoman_gateway.persona_evolution import (
    PersonaEvolutionLedger,
    build_persona_evolution_approval_message,
    persona_evolution_result_needs_notification,
    run_persona_evolution_cron,
)
from yeoman_gateway.policy.persona import load_persona_text
from yeoman_gateway.processing.dispatch import (
    ServiceEffectProducer,
    disable_non_migrated_tools,
)
from yeoman_gateway.processing.models import canonical_hash
from yeoman_gateway.processing.signals import SignalJournalSink
from yeoman_gateway.providers.factory import ProviderFactory
from yeoman_gateway.providers.openai_compatible import resolve_openai_compatible_credentials
from yeoman_gateway.security import NoopSecurity, SecurityEngine
from yeoman_gateway.session.manager import SessionManager
from yeoman_gateway.storage.inbound_archive import InboundArchive
from yeoman_gateway.storage.private_handoff import PrivateHandoffStore

if TYPE_CHECKING:
    from pathlib import Path

    from yeoman_shared.config.schema import Config, ExecToolConfig

    from yeoman_gateway.ipc.gateway_socket import GatewaySocket
    from yeoman_gateway.policy.engine import PolicyEngine
    from yeoman_gateway.processing.dispatch import IntentEffectRouter
    from yeoman_gateway.processing.store import ProcessingStore
    from yeoman_gateway.providers.base import LLMProvider


def _normalize_timestamp(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC)


def _resolve_security_tool_settings(config: "Config") -> tuple[bool, "ExecToolConfig"]:
    """Apply strict-profile hardening overrides for tool runtime settings."""
    restrict_to_workspace = bool(config.tools.restrict_to_workspace)
    exec_config = config.tools.exec.model_copy(deep=True)
    if config.security.strict_profile:
        restrict_to_workspace = True
        exec_config.isolation.enabled = True
        exec_config.isolation.fail_closed = True
        exec_config.allow_host_execution = False
    return restrict_to_workspace, exec_config


def _inbound_message_to_event(msg: InboundMessage) -> InboundEvent:
    meta = msg.metadata
    # Thread assignment from the fast gate travels with the event so the pipeline can
    # keep the own thread out of the ambient block.
    return InboundEvent(
        channel=msg.channel,
        chat_id=msg.chat_id,
        sender_id=msg.sender_id,
        content=msg.content,
        message_id=str(meta.get("message_id") or "").strip() or None,
        timestamp=_normalize_timestamp(msg.timestamp),
        participant=str(meta.get("participant") or "").strip() or None,
        is_group=bool(meta.get("is_group", False)),
        mentioned_bot=bool(meta.get("mentioned_bot", False)),
        reply_to_bot=bool(meta.get("reply_to_bot", False)),
        reply_to_message_id=str(meta.get("reply_to_message_id") or "").strip() or None,
        reply_to_participant=str(meta.get("reply_to_participant") or "").strip() or None,
        reply_to_text=str(meta.get("reply_to_text") or "").strip() or None,
        media=tuple(msg.media),
        raw_metadata=dict(meta),
    )


class OrchestratorService:
    """Consumes inbound messages and executes typed orchestrator intents."""

    def __init__(
        self,
        *,
        bus: MessageBus,
        orchestrator: Orchestrator,
        typing_adapter: ChannelManagerTypingAdapter,
        telemetry: InMemoryTelemetry,
        memory: MemoryService,
        effect_router: "IntentEffectRouter | None" = None,
    ) -> None:
        self._bus = bus
        self._orchestrator = orchestrator
        self._typing_adapter = typing_adapter
        self._telemetry = telemetry
        self._memory = memory
        self._effect_router = effect_router
        self._running = False

    async def run(self) -> None:
        self._running = True
        while self._running:
            try:
                msg = await asyncio.wait_for(self._bus.consume_inbound(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            event = _inbound_message_to_event(msg)
            try:
                intents = await self._orchestrator.handle(event)
                await self._dispatch_intents(intents, principal=event.sender_id)
            except Exception as e:
                logger.error(
                    "vnext orchestrator failure stage=handle_dispatch channel={} chat={} "
                    "message_id={} error_type={}",
                    event.channel,
                    event.chat_id,
                    event.message_id,
                    type(e).__name__,
                )

    def stop(self) -> None:
        self._running = False

    async def _dispatch_intents(
        self, intents: list[OrchestratorIntent], *, principal: str = ""
    ) -> None:
        for intent in intents:
            match intent:
                case SetTypingIntent():
                    await self._typing_adapter(intent.channel, intent.chat_id, intent.enabled)
                case SendOutboundIntent():
                    if self._effect_router is not None and await self._effect_router.submit_outbound(
                        intent, principal=principal
                    ):
                        continue
                    await self._bus.publish_outbound(
                        OutboundMessage(
                            channel=intent.event.channel,
                            chat_id=intent.event.chat_id,
                            content=intent.event.content,
                            reply_to=intent.event.reply_to,
                            media=list(intent.event.media),
                            metadata=dict(intent.event.metadata or {}),
                        )
                    )
                case SendReactionIntent():
                    if self._effect_router is not None and await self._effect_router.submit_reaction(
                        intent, principal=principal
                    ):
                        continue
                    await self._bus.publish_reaction(
                        ReactionMessage(
                            channel=intent.channel,
                            chat_id=intent.chat_id,
                            message_id=intent.message_id,
                            emoji=intent.emoji,
                            participant_jid=intent.participant_jid,
                        )
                    )
                case PersistSessionIntent():
                    # Sessions are persisted by the responder implementation.
                    continue
                case QueueMemoryNotesCaptureIntent():
                    self._memory.enqueue_background_note(
                        channel=intent.channel,
                        chat_id=intent.chat_id,
                        sender_id=intent.sender_id,
                        message_id=intent.message_id,
                        content=intent.content,
                        is_group=intent.is_group,
                        mode=intent.mode,
                        batch_interval_seconds=intent.batch_interval_seconds,
                        batch_max_messages=intent.batch_max_messages,
                    )
                case RecordManualMemoryIntent():
                    mapped_kind = "decision" if intent.entry_kind == "backlog" else "episodic"
                    salience = 0.9 if intent.entry_kind == "backlog" else 0.8
                    self._memory.record_manual(
                        channel=intent.channel,
                        chat_id=intent.chat_id,
                        sender_id=intent.sender_id,
                        scope_type="chat",
                        kind=mapped_kind,
                        text=intent.content,
                        importance=salience,
                        confidence=1.0,
                    )
                    self._memory.record_idea_backlog_capture(
                        entry_kind=intent.entry_kind,
                        content=intent.content,
                        source="orchestrator_manual_capture",
                    )
                case RecordMetricIntent():
                    self._telemetry.incr(intent.name, intent.value, intent.labels)
                case _:
                    assert_never(intent)


@dataclass(slots=True)
class GatewayRuntime:
    """Lifecycle holder for the composed gateway runtime."""

    orchestrator: OrchestratorService
    channels: ChannelManager
    cron: CronService
    heartbeat: HeartbeatService
    consciousness: object | None
    inbound_archive: InboundArchive
    responder: LLMResponder
    memory: MemoryService
    contacts: ContactsService
    chat_registry: object
    bus: MessageBus | None = None
    gateway_socket: "GatewaySocket | None" = None
    speakup_log: object | None = None
    lull_observer: object | None = None
    processing: "ProcessingStore | None" = None
    reconciliation: object | None = None
    shared_facts: object | None = None
    startup_hook: Callable[[], Awaitable[None]] | None = None

    async def run(self) -> None:
        tracing.init()
        try:
            await self.cron.start()
            await self.heartbeat.start()
            if self.consciousness is not None:
                await self.consciousness.start()
            if self.lull_observer is not None and hasattr(self.lull_observer, "start"):
                await self.lull_observer.start()
            if self.gateway_socket:
                await self.gateway_socket.start()
            if self.shared_facts is not None and hasattr(self.shared_facts, "start"):
                self.shared_facts.start()
            tasks = [
                self.orchestrator.run(),
                self.channels.start_all(),
            ]
            if self.startup_hook is not None:
                async def _run_startup_hook() -> None:
                    await asyncio.sleep(2.0)
                    try:
                        await self.startup_hook()
                    except Exception:
                        logger.exception("Gateway startup hook failed")

                tasks.append(_run_startup_hook())
            if self.bus:
                tasks.append(self.bus.dispatch_events())
            await asyncio.gather(*tasks)
        finally:
            if self.gateway_socket:
                await self.gateway_socket.stop()
            self.heartbeat.stop()
            if self.lull_observer is not None and hasattr(self.lull_observer, "stop"):
                self.lull_observer.stop()
            if self.consciousness is not None:
                self.consciousness.stop()
            self.cron.stop()
            self.orchestrator.stop()
            await self.channels.stop_all()
            await self.responder.aclose()
            self.inbound_archive.close()
            if self.speakup_log is not None and hasattr(self.speakup_log, "close"):
                self.speakup_log.close()
            if hasattr(self.chat_registry, "close"):
                self.chat_registry.close()
            if self.reconciliation is not None:
                await self.reconciliation.stop()
            if self.shared_facts is not None and hasattr(self.shared_facts, "stop"):
                self.shared_facts.stop()
            self.contacts.close()
            self.memory.close()
            if self.processing is not None:
                self.processing.close()
            await tracing.shutdown()


def build_processing_store(config: "Config") -> "ProcessingStore | None":
    """Open the durable processing store, but only when the new mode is enabled.

    Disabled mode stays byte-for-byte inert: no second database appears next to the
    archives. A store that cannot be opened leaves the new mode offline (fail closed)
    instead of degrading into an unaudited path.
    """
    if not config.processing.enabled:
        return None

    from yeoman_shared.utils.helpers import get_data_path

    from yeoman_gateway.processing.models import DAY_MS, RetentionSettings
    from yeoman_gateway.processing.store import ProcessingStore

    retention_cfg = config.processing.retention
    retention = RetentionSettings(
        journal_payload_ms=retention_cfg.journal_payload_days * DAY_MS,
        metadata_ms=retention_cfg.lineage_metadata_days * DAY_MS,
        unresolved_ms=retention_cfg.unresolved_days * DAY_MS,
    )
    path = Path(config.processing.db_path).expanduser()
    if not path.is_absolute():
        path = get_data_path() / path
    try:
        return ProcessingStore(path, retention=retention)
    except Exception:
        logger.exception("processing store unavailable; new processing mode stays offline")
        return None


@dataclass
class SharedFactRuntime:
    """Read gate plus (optionally) the extraction queue for one gateway process."""

    gate: object
    extraction: object | None
    memory: object
    processing: object | None = None
    chat_registry: object | None = None
    policy: object | None = None
    config: object | None = None

    @property
    def extraction_enabled(self) -> bool:
        return self.extraction is not None

    def start(self) -> None:
        if self.extraction is not None:
            self.extraction.start()

    def stop(self) -> None:
        if self.extraction is not None:
            self.extraction.stop()


def build_shared_fact_runtime(
    config: "Config",
    *,
    store: "ProcessingStore | None" = None,
    processing: object | None = None,
    chat_registry: object | None = None,
    policy: object | None = None,
    memory: "MemoryService | None" = None,
) -> SharedFactRuntime | None:
    """Shared-fact runtime (Plan 05), or ``None`` when any switch is off.

    Three switches must agree before anything exists: memory, the shared-fact opt-in and
    the new processing mode. Disabled mode therefore has no worker thread, no job row and
    no gate object - the same fail-closed shape as :func:`build_processing_store`.
    """
    if not getattr(getattr(config, "memory", None), "enabled", False):
        return None
    shared = getattr(config.memory, "shared", None)
    if shared is None or not bool(getattr(shared, "enabled", False)):
        return None
    if not getattr(getattr(config, "processing", None), "enabled", False):
        return None
    if memory is None or store is None:
        return None

    from yeoman_gateway.memory.extraction_jobs import (
        EXTRACTOR_VERSION,
        SharedFactExtractionQueue,
    )
    from yeoman_gateway.memory.read_gate import FactReadGate

    extraction_cfg = getattr(config.processing, "extraction", None)
    retention_cfg = getattr(config.processing, "retention", None)
    fact_ttl_ms = (
        int(retention_cfg.shared_fact_days) * 24 * 3600 * 1000 if retention_cfg else None
    )
    extractor = None
    if bool(getattr(shared, "extraction_enabled", False)):
        from yeoman_gateway.memory.fact_extractor import SharedFactExtractor

        try:
            extractor = SharedFactExtractor(
                config=config,
                route_key=str(
                    getattr(
                        getattr(config.memory, "capture", None),
                        "extract_route",
                        "memory.capture.extract",
                    )
                ),
                member_provider=_shared_fact_members(chat_registry),
            )
        except Exception:
            logger.exception("shared fact extractor unavailable; extraction stays off")
            extractor = None
    queue = SharedFactExtractionQueue(
        store=memory.store,
        journal=store,
        extractor=extractor,
        idle_ms=int(getattr(extraction_cfg, "idle_seconds", 60)) * 1000,
        max_delay_ms=int(getattr(extraction_cfg, "max_delay_seconds", 300)) * 1000,
        max_waiting=int(getattr(shared, "max_jobs_waiting", 64)),
        fact_ttl_ms=fact_ttl_ms,
        extractor_version=str(getattr(shared, "extractor_version", EXTRACTOR_VERSION)),
    )
    runtime = SharedFactRuntime(
        gate=FactReadGate(memory.store),
        extraction=queue if bool(getattr(shared, "extraction_enabled", False)) else None,
        memory=memory,
        processing=processing,
        chat_registry=chat_registry,
        policy=policy,
        config=shared,
    )
    memory.extraction = runtime.extraction
    return runtime


def _shared_fact_members(chat_registry: object | None):
    """Proven chat participants, or ``None`` when nothing is proven.

    The audience of a fact must come from a recorded participant list - never from the
    model and never from a guess. Unknown membership therefore yields no audience, and
    the fact degrades to ``author_only``.
    """
    if chat_registry is None:
        return None

    def _lookup(channel: str, chat_id: str) -> frozenset[str] | None:
        try:
            record = chat_registry.get_chat(channel, chat_id)  # type: ignore[attr-defined]
        except Exception:
            return None
        if not isinstance(record, dict):
            return None
        metadata = record.get("metadata")
        participants = None
        if isinstance(metadata, dict):
            participants = metadata.get("participants")
        if not isinstance(participants, list) or not participants:
            return None
        members: set[str] = set()
        for item in participants:
            if isinstance(item, str):
                members.add(item)
                continue
            if isinstance(item, dict):
                for key in ("id", "jid", "lid", "phoneNumber", "user_id"):
                    value = item.get(key)
                    if value:
                        members.add(str(value))
                        break
        return frozenset(members) if members else None

    return _lookup


def build_reconciliation_service(
    config: "Config",
    store: "ProcessingStore | None",
    *,
    probe: object | None = None,
):
    """Reconciler for the new mode; ``None`` while processing is off or no store is open.

    Disabled mode stays inert: no store, no database and no background task.
    """
    if store is None or not config.processing.enabled:
        return None

    from yeoman_gateway.processing.reconcile import LocalEvidenceProbe, ReconciliationService

    reconciliation = config.processing.reconciliation
    evidence = probe or LocalEvidenceProbe(
        store,
        provider_lookup_enabled=bool(reconciliation.provider_lookup_enabled),
    )
    return ReconciliationService(store, probe=evidence, config=reconciliation)


def build_thread_registry(config: "Config", store: "ProcessingStore | None"):
    """Thread registry for the new mode; ``None`` while processing is off."""
    if store is None or not config.processing.enabled:
        return None

    from yeoman_gateway.processing.threads import ThreadRegistry

    return ThreadRegistry(store=store, config=config.processing)


def build_processing_gate(
    config: "Config",
    policy_adapter: "EnginePolicyAdapter | None",
    store: "ProcessingStore | None",
    threads: object | None = None,
):
    """Fast gate for canonical ingest -> journal -> policy, before expensive work.

    Returns ``None`` when the new mode is off, so the legacy path is untouched.
    """
    if store is None or policy_adapter is None or not config.processing.enabled:
        return None

    from yeoman_gateway.processing.policy import AdapterSnapshotProvider, IngestGate

    return IngestGate(
        config=config.processing,
        store=store,
        snapshots=AdapterSnapshotProvider(policy_adapter),
        evaluate=lambda request: policy_adapter.evaluate(request.event),
        threads=threads if threads is not None else build_thread_registry(config, store),
    )


def build_thread_responder(
    config: "Config",
    store: "ProcessingStore | None",
    threads: object | None,
    responder: object,
    policy_adapter: "EnginePolicyAdapter | None" = None,
):
    """Responder wrapper that drives the thread actor. ``None`` keeps the legacy path."""
    if store is None or threads is None or not config.processing.enabled:
        return None

    from yeoman_gateway.processing.actor import ThreadActorRegistry
    from yeoman_gateway.processing.policy import operator_check
    from yeoman_gateway.processing.responder import ThreadActorResponder
    from yeoman_gateway.processing.threads import TurnAuthority

    actor_registry = ThreadActorRegistry(
        store=store,
        config=config.processing,
        authority=TurnAuthority(
            is_operator=operator_check(
                (lambda: policy_adapter.policy_engine()) if policy_adapter is not None else (lambda: None)
            )
        ),
    )
    return ThreadActorResponder(inner=responder, actors=actor_registry, store=store)


def build_effect_router(
    config: "Config",
    policy_adapter: "EnginePolicyAdapter | None",
    store: "ProcessingStore | None",
    bus: MessageBus,
    security: object | None = None,
    threads: object | None = None,
):
    """Effect gateway plus transport executor for the new mode.

    Returns ``None`` while the new mode is disabled, so every producer keeps its legacy
    path and no second sender exists for the same turn.
    """
    if store is None or policy_adapter is None or not config.processing.enabled:
        return None

    from yeoman_gateway.processing.dispatch import BusEffectExecutor, IntentEffectRouter
    from yeoman_gateway.processing.effects import EffectGateway
    from yeoman_gateway.processing.policy import (
        AdapterSnapshotProvider,
        PolicyCapabilityResolver,
        SnapshotEffectAuthorizer,
    )

    snapshots = AdapterSnapshotProvider(policy_adapter)
    gateway = EffectGateway(
        store,
        authorizer=SnapshotEffectAuthorizer(
            snapshots=snapshots,
            capabilities=PolicyCapabilityResolver(
                engine_provider=policy_adapter.policy_engine,
                known_tools=lambda: set(policy_adapter.known_tools),
            ),
            turn_lookup=getattr(threads, "turn_lookup", None),
        ),
        executor=BusEffectExecutor(
            bus=bus,
            mark_provenance=True,
            security=security,
            security_block_message=config.security.block_user_message,
        ),
    )
    return IntentEffectRouter(
        gateway=gateway,
        config=config,
        turn_provider=getattr(threads, "active_turn", None),
    )


def build_gateway_runtime(
    *,
    config: "Config",
    provider: "LLMProvider",
    policy_engine: "PolicyEngine | None",
    policy_path: "Path | None",
    workspace: "Path",
    bus: MessageBus,
) -> GatewayRuntime:
    """Compose full gateway runtime around vNext orchestrator."""

    from yeoman_shared.utils.helpers import get_operational_data_path

    processing_store = build_processing_store(config)

    session_manager = SessionManager(workspace)
    # The owner wants a complete inbound record: keep every message, purge nothing.
    inbound_archive = InboundArchive(
        db_path=get_operational_data_path() / "inbound" / "reply_context.db",
        retention_days=None,
    )
    model_router = ModelRouter(config.models)
    media_storage = MediaStorage(
        incoming_dir=config.channels.whatsapp.media.incoming_path,
        outgoing_dir=config.channels.whatsapp.media.outgoing_path,
    )
    provider_factory = ProviderFactory(config=config)
    document_cache = DocumentCache(get_operational_data_path() / "media" / "document_cache.db")
    lazy_vision = (
        VisionDescriber(provider_factory)
        if config.channels.whatsapp.media.ocr_images
        else None
    )
    document_processor = DocumentProcessor(
        cache=document_cache,
        model_router=model_router,
        vision_describer=lazy_vision,
        max_document_bytes=config.channels.whatsapp.media.max_document_bytes_mb * 1024 * 1024,
        max_image_bytes=config.channels.whatsapp.media.max_image_bytes_mb * 1024 * 1024,
        max_pdf_pages=config.channels.whatsapp.media.max_document_text_pages,
        max_prompt_chars=config.channels.whatsapp.media.max_document_prompt_chars,
    )
    lazy_media_resolver = LazyMediaResolver(
        cache=document_cache,
        processor=document_processor,
        max_prompt_chars=config.channels.whatsapp.media.max_document_prompt_chars,
    )

    assistant_model = config.agents.defaults.model
    try:
        assistant_profile = model_router.resolve("assistant.reply")
        if assistant_profile.model:
            assistant_model = assistant_profile.model
    except KeyError:
        pass

    telemetry = InMemoryTelemetry()
    restrict_to_workspace, exec_config = _resolve_security_tool_settings(config)
    security = SecurityEngine(config.security) if config.security.enabled else NoopSecurity()

    # Optional LLM-based input classifier (second defence layer).
    security_classifier = None
    if config.security.enabled and config.security.stages.input:
        try:
            from yeoman_gateway.security.classifier import InputClassifier

            security_classifier = InputClassifier(config=config)
        except Exception as exc:
            logger.warning("security classifier disabled: {}", exc)

    memory_service = MemoryService(workspace=workspace, config=config.memory, root_config=config)
    memory_state_dir = config.memory.wal.state_dir
    try:
        imported = memory_service.backfill_from_workspace_files(force=False)
        if imported > 0:
            logger.info("memory backfill imported {} entries", imported)
    except Exception as e:
        logger.warning("memory backfill failed: {}", e)

    contacts_service = ContactsService(
        db_path=get_operational_data_path() / "contacts" / "contacts.db",
    )
    from yeoman_gateway.storage.chat_registry import ChatRegistry

    chat_registry = ChatRegistry(
        db_path=get_operational_data_path() / "inbound" / "chat_registry.db",
    )
    contacts_service.mark_owner_from_policy(
        policy_engine.policy.owners if policy_engine else {},
    )
    memory_service.set_contacts(contacts_service)
    try:
        linked = contacts_service.backfill_memory(memory_service.store)
        if linked > 0:
            logger.info("contacts: backfilled {} memory nodes with contact_id", linked)
    except Exception as e:
        logger.warning("contacts: memory backfill failed: {}", e)

    cron_store_path = get_operational_data_path() / "cron" / "jobs.json"
    cron = CronService(cron_store_path, sessions_dir=get_operational_data_path() / "inbound")
    private_handoffs = PrivateHandoffStore(get_operational_data_path() / "policy" / "private_handoffs.json")

    # Create policy adapter first so we can use it for owner_alert_resolver
    policy_adapter = EnginePolicyAdapter(
        engine=policy_engine,
        known_tools=set(),  # Will be updated after responder is created
        policy_path=policy_path,
        session_manager=session_manager,
        private_handoff_store=private_handoffs,
        workspace=workspace,
        memory_state_dir=memory_state_dir,
    )
    policy_adapter.set_memory_service(memory_service)

    file_access_resolver = build_file_access_resolver(
        workspace=workspace,
        policy=policy_engine.policy if policy_engine is not None else None,
    )
    a2a_registry = A2AWorkerRegistry.from_config(config.tools.a2a)
    if a2a_registry is not None:
        logger.info("A2A worker tools enabled: {}", ", ".join(a2a_registry.names))

    openai_compat = resolve_openai_compatible_credentials(config)
    elevenlabs = config.providers.elevenlabs
    openrouter = config.providers.openrouter
    fish_api_key = os.environ.get("FISH_API_KEY") or ""
    tts = TTSSynthesizer(
        openai_api_key=openai_compat.api_key if openai_compat else None,
        openai_api_base=openai_compat.api_base if openai_compat else None,
        openai_extra_headers=openai_compat.extra_headers if openai_compat else None,
        elevenlabs_api_key=elevenlabs.api_key or None,
        elevenlabs_api_base=elevenlabs.api_base,
        elevenlabs_extra_headers=elevenlabs.extra_headers,
        elevenlabs_default_voice_id=elevenlabs.voice_id,
        elevenlabs_default_model_id=elevenlabs.model_id,
        openrouter_api_key=openrouter.api_key or None,
        openrouter_api_base=openrouter.api_base,
        openrouter_extra_headers=openrouter.extra_headers,
        fish_api_key=fish_api_key or None,
        fish_default_voice_id="71c095ed4c03459fb98500db63b88fbe",
        max_concurrency=config.channels.whatsapp.media.max_tts_concurrency,
    )

    # CalDAV service — enabled when iCloud credentials are in env
    _caldav_service = None
    _caldav_user = os.environ.get("ICLOUD_CALDAV_USERNAME")
    _caldav_pass = os.environ.get("ICLOUD_CALDAV_APP_PASSWORD")
    if _caldav_user and _caldav_pass:
        from yeoman_gateway.caldav.service import CalDAVService

        _caldav_service = CalDAVService(_caldav_user, _caldav_pass)
        logger.info("CalDAV service enabled for {}", _caldav_user)

    thread_registry = build_thread_registry(config, processing_store)
    effect_router = build_effect_router(
        config,
        policy_adapter,
        processing_store,
        bus,
        security=security,
        threads=thread_registry,
    )
    if effect_router is not None:
        from yeoman_gateway.processing.dispatch import managed_outbound_guard

        bus.set_managed_outbound_guard(managed_outbound_guard(effect_router))
    service_effects = (
        ServiceEffectProducer(router=effect_router, bus=bus)
        if effect_router is not None
        else None
    )

    responder = LLMResponder(
        provider=provider,
        workspace=workspace,
        bus=bus,
        model=assistant_model,
        subagent_model=config.agents.defaults.subagent_model,
        max_iterations=config.agents.defaults.max_tool_iterations,
        tavily_api_key=config.tools.web.search.tavily_api_key or None,
        web_config=config.tools.web,
        exec_config=exec_config,
        restrict_to_workspace=restrict_to_workspace,
        session_manager=session_manager,
        effect_router=effect_router,
        memory_service=memory_service,
        telemetry=telemetry,
        security=security,
        cron_service=cron,
        contacts_service=contacts_service,
        chat_registry=chat_registry,
        caldav_service=_caldav_service,
        owner_alert_resolver=policy_adapter.owner_recipients,
        file_access_resolver=file_access_resolver,
        group_resolver=policy_adapter.resolve_whatsapp_group,
        model_router=model_router,
        routed_provider_factory=provider_factory.create_chat_provider,
        tts=tts,
        whatsapp_tts_outgoing_dir=config.channels.whatsapp.media.outgoing_path,
        inbound_archive=inbound_archive,
        private_handoff_store=private_handoffs,
        a2a_registry=a2a_registry,
        lazy_media_resolver=lazy_media_resolver,
        whatsapp_session_history_limit=config.channels.whatsapp.session_history_limit,
        whatsapp_session_history_limit_group=config.channels.whatsapp.session_history_limit_group,
    )
    if policy_engine is not None:
        policy_engine.validate(set(responder.tool_names))

    if effect_router is not None:
        # The new mode must not keep uncontained write capabilities as a bypass.
        disable_non_migrated_tools(responder.tools)

    # Wire /voice command callback: reuses the send_voice tool.
    async def _voice_send_callback(content: str, chat_id: str) -> str:
        return await responder.tools.execute(
            "send_voice",
            {
                "content": content,
                "channel": "whatsapp",
                "chat_id": chat_id,
                "voice": "71c095ed4c03459fb98500db63b88fbe",
                "verbatim": True,
            },
        )

    policy_adapter.set_voice_send_callback(_voice_send_callback)

    # Wire admin notify callback: sends text to a given channel+chat.
    async def _admin_notify(channel: str, chat_id: str, text: str) -> None:
        if service_effects is not None:
            await service_effects.send(
                source="admin",
                operation_ref=f"admin:{channel}:{chat_id}:{canonical_hash(text)[:12]}",
                channel=channel,
                chat_id=chat_id,
                content=text,
                capability="send_text",
            )
            return
        await bus.publish_outbound(OutboundMessage(channel=channel, chat_id=chat_id, content=text))

    policy_adapter.set_admin_notify_callback(_admin_notify)

    # Update policy adapter with actual tool names
    policy_adapter._known_tools = set(responder.tool_names)
    admin_command_handler = getattr(policy_adapter, "route_admin_command", None)
    if admin_command_handler is None:
        admin_command_handler = getattr(policy_adapter, "maybe_handle_admin_command", None)

    channels = ChannelManager(
        config,
        bus,
        session_manager=session_manager,
        inbound_archive=inbound_archive,
        model_router=model_router,
        media_storage=media_storage,
        provider_factory=provider_factory,
        document_cache=document_cache,
        processing_gate=build_processing_gate(config, policy_adapter, processing_store),
        processing_signals=(
            SignalJournalSink(processing_store) if processing_store is not None else None
        ),
    )

    typing_adapter = ChannelManagerTypingAdapter(channels)

    if effect_router is not None:
        # Approved effects go straight to the channel transport, so a successful send is
        # a real receipt instead of a queue guess (spec R07).
        effect_router.set_direct_transport(channels.send_now, channels.send_reaction_now)

    # Wire recording indicator: responder switches presence to mic icon during TTS
    async def _recording_notifier(channel: str, chat_id: str) -> None:
        await channels.set_recording(channel, chat_id)

    responder._recording_notifier = _recording_notifier

    from yeoman_gateway.cron.workflow_chain import build_chained_prompt, is_chain_failure
    from yeoman_gateway.cron.workflow_state import PendingApproval, WorkflowState

    workflow_state = WorkflowState(
        store_path=Path(workspace) / "data" / "cron" / "pending_approvals.json"
    )
    persona_evolution_state_db_path = Path(workspace) / "persona-evolution" / "persona-evolution.db"

    async def _on_approval_expired(approval: PendingApproval) -> None:
        content = (
            f"Workflow approval expired: {approval.approval_id}. "
            "Use /cron workflow_list to review."
        )
        if service_effects is not None:
            await service_effects.send(
                source="cron",
                operation_ref=f"approval-expired:{approval.approval_id}",
                channel=approval.channel,
                chat_id=approval.chat_id,
                content=content,
            )
            return
        await bus.publish_outbound(OutboundMessage(
            channel=approval.channel,
            chat_id=approval.chat_id,
            content=content,
        ))

    cron._workflow_state = workflow_state
    cron._on_approval_expired = _on_approval_expired

    async def _handle_approved_job(approval: PendingApproval) -> None:
        from uuid import uuid4
        next_job = cron.get_job(approval.next_job_id)
        if not next_job:
            logger.warning("Approved job {} not found", approval.next_job_id)
            return
        run_id = uuid4().hex[:8]
        prompt = build_chained_prompt(
            approval.previous_output, next_job.payload.message,
            input_from_previous=next_job.payload.input_from_previous,
        )
        next_job.payload.max_chain_depth = approval.remaining_depth
        response = await responder.process_direct(
            prompt, session_key=f"cron:{next_job.id}:{run_id}",
            channel=next_job.payload.channel or "cli",
            chat_id=next_job.payload.to or "direct",
            model_profile=next_job.payload.model_profile,
        )
        if next_job.payload.deliver and next_job.payload.to:
            delivery_channel = next_job.payload.channel or "cli"
            if service_effects is not None:
                await service_effects.send(
                    source="cron",
                    operation_ref=f"cron:{next_job.id}:{run_id}",
                    channel=delivery_channel,
                    chat_id=next_job.payload.to,
                    content=response or "",
                )
            else:
                await bus.publish_outbound(OutboundMessage(
                    channel=delivery_channel,
                    chat_id=next_job.payload.to, content=response or "",
                ))
        if next_job.payload.next_job_id and response and not is_chain_failure(response):
            await _handle_chain(next_job, response, run_id)

    speakup_log = None
    speakup_approval_store = None
    if config.consciousness.enabled and policy_engine is not None:
        from yeoman_gateway.consciousness.approval import SpeakupApprovalStore
        from yeoman_gateway.consciousness.log import SpeakupLog

        consciousness_data_dir = get_operational_data_path() / "consciousness"
        speakup_log = SpeakupLog(consciousness_data_dir / "speakups.db")
        speakup_approval_store = SpeakupApprovalStore(
            consciousness_data_dir / "pending_approvals.json"
        )

    archive_adapter = SqliteReplyArchiveAdapter(inbound_archive)
    thread_responder = build_thread_responder(
        config, processing_store, thread_registry, responder, policy_adapter
    )

    orchestrator = Orchestrator(
        policy=policy_adapter,
        responder=thread_responder or responder,
        reply_archive=archive_adapter,
        contacts=contacts_service,
        reply_context_window_limit=config.channels.whatsapp.reply_context_window_limit,
        reply_context_line_max_chars=config.channels.whatsapp.reply_context_line_max_chars,
        ambient_window_limit=config.channels.whatsapp.ambient_window_limit,
        typing_notifier=typing_adapter,
        security=security,
        security_classifier=security_classifier,
        security_block_message=config.security.block_user_message,
        policy_admin_handler=admin_command_handler,
        model_router=model_router,
        tts=tts,
        whatsapp_tts_outgoing_dir=config.channels.whatsapp.media.outgoing_path,
        owner_alert_resolver=policy_adapter.owner_recipients,
        workflow_state=workflow_state,
        approval_trigger=_handle_approved_job,
        bus=bus,
        speakup_approval_store=speakup_approval_store,
        speakup_log=speakup_log,
        persona_evolution_workspace=Path(workspace),
        persona_evolution_state_db_path=persona_evolution_state_db_path,
        session_manager=session_manager,
        service_effects=service_effects,
    )

    def _choose_voice_phrase(job: CronJob, phrases: list[str]) -> str:
        recent = [str(v).strip() for v in job.payload.voice_recent_messages if str(v).strip()]
        available = [phrase for phrase in phrases if phrase not in set(recent)]
        pool = available or phrases
        return random.choice(pool) if job.payload.voice_random else pool[0]

    def _remember_voice_phrase(job: CronJob, content: str) -> None:
        text = str(content or "").strip()
        if not text:
            return
        recent = [str(v).strip() for v in job.payload.voice_recent_messages if str(v).strip()]
        recent = [value for value in recent if value != text]
        recent.append(text)
        job.payload.voice_recent_messages = recent[-8:]

    def _clean_generated_voice(text: str) -> str:
        cleaned = " ".join(line.strip() for line in str(text or "").splitlines() if line.strip())
        cleaned = cleaned.strip().strip('"').strip("'").strip()
        for prefix in ("Text:", "Voice:", "Nachricht:", "Sprachnachricht:"):
            if cleaned.lower().startswith(prefix.lower()):
                cleaned = cleaned[len(prefix):].strip()
        if len(cleaned) > 360:
            return ""
        return cleaned

    async def _generate_voice_phrase(
        job: CronJob,
        *,
        channel: str,
        chat_id: str,
        fallback_phrases: list[str],
    ) -> str:
        fallback = _choose_voice_phrase(job, fallback_phrases)
        if not job.payload.voice_generate:
            return fallback

        recent = [str(v).strip() for v in job.payload.voice_recent_messages if str(v).strip()]
        recent_block = "\n".join(f"- {value}" for value in recent[-8:]) or "- none"
        default_prompt = (
            "Write one fresh German WhatsApp voice-note line for Arvid in Finanzgruppe. "
            "It is a quiet weekly morning ritual, not a reply to the current chat. "
            "Make it lightly funny, concise, and natural. Max two short sentences. "
            "Do not ask a question. Do not use a visible label. Return only the line."
        )
        prompt = (
            f"{job.payload.voice_prompt or default_prompt}\n\n"
            "Avoid repeating or closely paraphrasing these previous weekly voice lines:\n"
            f"{recent_block}"
        )
        persona_text = None
        model_profile = job.payload.model_profile
        if policy_engine is not None:
            resolved = policy_engine.resolve_policy(channel, chat_id)
            persona_text = load_persona_text(resolved.persona_file, Path(workspace))
            model_profile = model_profile or resolved.model_profile

        try:
            generated = await responder.process_direct(
                prompt,
                session_key=f"cron:{job.id}:voice-generate:{int(time.time())}",
                channel=channel,
                chat_id=chat_id,
                allowed_tools=set(),
                persona_text=persona_text,
                is_owner=True,
                model_profile=model_profile,
            )
        except Exception as exc:
            logger.warning("Cron voice generation failed for {}: {}", job.id, exc)
            return fallback

        cleaned = _clean_generated_voice(generated)
        if not cleaned or cleaned in set(recent):
            return fallback
        return cleaned

    async def _notify_persona_evolution_review(
        proposal: dict[str, object],
        *,
        approval_channel: str | None = None,
    ) -> None:
        if proposal.get("notified_at"):
            return
        channel = str(approval_channel or "telegram").strip() or "telegram"
        raw_targets = policy_adapter.owner_recipients(channel)
        if not raw_targets:
            logger.warning(
                "Persona evolution proposal {} has no owner recipients for {}",
                proposal.get("proposal_id"),
                channel,
            )
            return
        from yeoman_gateway.pipeline.new_chat import _normalize_owner_target

        targets = sorted(
            {
                target
                for raw in raw_targets
                if (target := _normalize_owner_target(channel, str(raw)))
            }
        )
        if not targets:
            logger.warning(
                "Persona evolution proposal {} had no valid owner targets for {}",
                proposal.get("proposal_id"),
                channel,
            )
            return
        message = build_persona_evolution_approval_message(proposal)
        for target in targets:
            if service_effects is not None:
                await service_effects.send(
                    source="cron",
                    operation_ref=(
                        f"persona-evolution:{proposal.get('proposal_id')}:{target}"
                    ),
                    channel=channel,
                    chat_id=target,
                    content=message,
                )
                continue
            await bus.publish_outbound(
                OutboundMessage(channel=channel, chat_id=target, content=message)
            )
        ledger = PersonaEvolutionLedger(persona_evolution_state_db_path)
        try:
            ledger.mark_notified(
                str(proposal["proposal_id"]),
                channel=channel,
                chat_id=",".join(targets),
            )
        finally:
            ledger.close()

    async def _notify_pending_persona_evolution_reviews() -> None:
        ledger = PersonaEvolutionLedger(persona_evolution_state_db_path)
        try:
            proposals = ledger.pending_proposals()
        finally:
            ledger.close()
        for proposal in proposals:
            await _notify_persona_evolution_review(proposal)

    async def on_cron_job(job: CronJob) -> str | None:
        if job.payload.kind == "persona_evolution":
            if not config.persona_evolution.enabled:
                return "persona_evolution no proposal: disabled"
            persona_file = str(job.payload.persona_file or "").strip()
            if not persona_file:
                raise ValueError("persona_evolution job requires personaFile")
            allowlist = {
                str(item).strip()
                for item in config.persona_evolution.personas_allowlist
                if str(item).strip()
            }
            if allowlist and persona_file not in allowlist:
                return f"persona_evolution no proposal: persona_not_allowed persona_file={persona_file}"
            output_path = None
            if str(job.payload.persona_output or "").strip():
                output_path = Path(str(job.payload.persona_output).strip())
            result = await run_persona_evolution_cron(
                policy=policy_engine.policy,
                workspace=Path(workspace),
                persona_file=persona_file,
                memory=memory_service,
                speakup_log=speakup_log,
                inbound_archive=inbound_archive,
                window_days=max(1, int(job.payload.persona_window_days)),
                limit=max(1, int(job.payload.persona_limit)),
                output_path=output_path,
                min_meaningful_messages=max(
                    0, int(job.payload.persona_min_meaningful_messages)
                ),
                min_signal_score=max(0.0, float(job.payload.persona_min_signal_score)),
                max_accumulation_days=max(1, int(job.payload.persona_max_accumulation_days)),
                proposal_ttl_seconds=max(60, int(config.persona_evolution.proposal_ttl_seconds)),
                proposal_mode=config.persona_evolution.mode,
            )
            if persona_evolution_result_needs_notification(result):
                ledger = PersonaEvolutionLedger(persona_evolution_state_db_path)
                try:
                    proposal = ledger.pending_proposal(persona_file)
                finally:
                    ledger.close()
                if proposal is not None:
                    await _notify_persona_evolution_review(
                        proposal,
                        approval_channel=job.payload.approval_channel,
                    )
            return result

        if job.payload.kind == "voice_broadcast":
            phrases = [str(v).strip() for v in list(job.payload.voice_messages) if str(v).strip()]
            if not phrases and str(job.payload.message or "").strip():
                phrases = [str(job.payload.message).strip()]
            if not phrases:
                raise ValueError("voice_broadcast job has no message candidates")

            voice_channel = str(job.payload.voice_channel or "").strip() or "whatsapp"
            if str(job.payload.voice_group or "").strip():
                if voice_channel != "whatsapp":
                    raise ValueError("voice_broadcast group targets require WhatsApp")
                chat_target, err = policy_adapter.resolve_whatsapp_group(
                    str(job.payload.voice_group).strip()
                )
                if err is not None or not chat_target:
                    raise ValueError(err or "failed to resolve voice_broadcast group")
            else:
                chat_target = (
                    str(job.payload.voice_chat_id or "").strip()
                    or str(job.payload.to or "").strip()
                )
                if not chat_target:
                    raise ValueError("voice_broadcast job has no target chat")

            quiet = evaluate_voice_quiet_gate(
                payload=job.payload,
                inbound_archive=inbound_archive,
                channel=voice_channel,
                chat_id=chat_target,
                now=datetime.now(UTC).astimezone(),
            )
            if quiet.status == "defer":
                retry_at_ms = quiet.retry_at_ms or int((time.time() + 1800) * 1000)
                raise CronJobDeferredError(quiet.reason, retry_at_ms=retry_at_ms)
            if quiet.status == "skip":
                raise CronJobSkippedError(quiet.reason)

            content = await _generate_voice_phrase(
                job,
                channel=voice_channel,
                chat_id=chat_target,
                fallback_phrases=phrases,
            )
            args: dict[str, object] = {"content": content}
            if job.payload.voice_verbatim:
                args["verbatim"] = True
            if job.payload.voice_name:
                args["voice"] = job.payload.voice_name
            if job.payload.voice_tts_route:
                args["tts_route"] = job.payload.voice_tts_route
            if job.payload.voice_max_sentences is not None:
                args["max_sentences"] = int(job.payload.voice_max_sentences)
            if job.payload.voice_max_chars is not None:
                args["max_chars"] = int(job.payload.voice_max_chars)

            args["channel"] = voice_channel
            if str(job.payload.voice_group or "").strip():
                args["group"] = str(job.payload.voice_group).strip()
            else:
                args["chat_id"] = chat_target

            result = await responder.tools.execute("send_voice", args)
            if str(result).startswith("Error:"):
                raise RuntimeError(str(result))
            _remember_voice_phrase(job, content)
            return str(result)

        response = await responder.process_direct(
            job.payload.message,
            session_key=f"cron:{job.id}",
            channel=job.payload.channel or "cli",
            chat_id=job.payload.to or "direct",
            model_profile=job.payload.model_profile,
        )
        if job.payload.deliver and job.payload.to:
            delivery_channel = job.payload.channel or "cli"
            if service_effects is not None:
                await service_effects.send(
                    source="cron",
                    operation_ref=(
                        f"cron:{job.id}:"
                        f"{job.state.last_run_at_ms or job.state.next_run_at_ms or ''}"
                    ),
                    channel=delivery_channel,
                    chat_id=job.payload.to,
                    content=response or "",
                )
            else:
                await bus.publish_outbound(
                    OutboundMessage(
                        channel=delivery_channel,
                        chat_id=job.payload.to,
                        content=response or "",
                    )
                )

        # Workflow chaining
        if job.payload.next_job_id and response is not None:
            if is_chain_failure(response):
                fail_channel = job.payload.approval_channel or job.payload.channel or "cli"
                fail_chat = job.payload.to or "direct"
                wf_name = job.payload.workflow_id or job.id
                fail_content = (
                    f"Workflow '{wf_name}' failed at step {job.payload.workflow_step}: "
                    f"{response[:200]}. Use /cron workflow_list to review."
                )
                if service_effects is not None:
                    await service_effects.send(
                        source="cron",
                        operation_ref=f"workflow:{wf_name}:step{job.payload.workflow_step}:failed",
                        channel=fail_channel,
                        chat_id=fail_chat,
                        content=fail_content,
                    )
                else:
                    await bus.publish_outbound(OutboundMessage(
                        channel=fail_channel, chat_id=fail_chat,
                        content=fail_content,
                    ))
            else:
                from uuid import uuid4
                run_id = uuid4().hex[:8]
                await _handle_chain(job, response, run_id)

        return response

    async def _handle_chain(job: CronJob, output: str, run_id: str = "") -> None:
        next_job = cron.get_job(job.payload.next_job_id) if job.payload.next_job_id else None
        if not next_job:
            logger.warning("Chained job {} not found", job.payload.next_job_id)
            return

        remaining = job.payload.max_chain_depth - 1
        if remaining <= 0:
            wf_name = job.payload.workflow_id or job.id
            stop_channel = job.payload.approval_channel or job.payload.channel or "cli"
            stop_chat = job.payload.to or "direct"
            stop_content = f"Workflow '{wf_name}' stopped: max chain depth reached."
            if service_effects is not None:
                await service_effects.send(
                    source="cron",
                    operation_ref=f"workflow:{wf_name}:max-depth",
                    channel=stop_channel,
                    chat_id=stop_chat,
                    content=stop_content,
                )
            else:
                await bus.publish_outbound(OutboundMessage(
                    channel=stop_channel,
                    chat_id=stop_chat,
                    content=stop_content,
                ))
            return

        if job.payload.requires_approval:
            from uuid import uuid4
            approval_id = f"wf-approve-{job.id}-{uuid4().hex[:8]}"
            approval_channel = job.payload.approval_channel or job.payload.channel or "cli"
            approval_chat = job.payload.to or "direct"

            await workflow_state.add(PendingApproval(
                approval_id=approval_id,
                next_job_id=next_job.id,
                previous_output=output,
                channel=approval_channel,
                chat_id=approval_chat,
                created_at=time.time(),
                expires_at=time.time() + 86400,
                workflow_id=job.payload.workflow_id,
                remaining_depth=remaining,
            ))

            approval_content = (
                f"{output}\n\n---\n"
                f"Workflow step {job.payload.workflow_step} complete.\n"
                f"Next: {next_job.name}\n"
                f"Reply with this code to approve: {approval_id}"
            )
            if service_effects is not None:
                await service_effects.send(
                    source="cron",
                    operation_ref=f"workflow-approval:{approval_id}",
                    channel=approval_channel,
                    chat_id=approval_chat,
                    content=approval_content,
                )
            else:
                await bus.publish_outbound(OutboundMessage(
                    channel=approval_channel, chat_id=approval_chat,
                    content=approval_content,
                ))
        else:
            prompt = build_chained_prompt(output, next_job.payload.message, input_from_previous=next_job.payload.input_from_previous)
            next_job.payload.max_chain_depth = remaining
            chain_response = await responder.process_direct(
                prompt,
                session_key=f"cron:{next_job.id}:{run_id}",
                channel=next_job.payload.channel or "cli",
                chat_id=next_job.payload.to or "direct",
                model_profile=next_job.payload.model_profile,
            )
            if next_job.payload.deliver and next_job.payload.to:
                chain_channel = next_job.payload.channel or "cli"
                if service_effects is not None:
                    await service_effects.send(
                        source="cron",
                        operation_ref=f"cron:{next_job.id}:{run_id}",
                        channel=chain_channel,
                        chat_id=next_job.payload.to,
                        content=chain_response or "",
                    )
                else:
                    await bus.publish_outbound(OutboundMessage(
                        channel=chain_channel,
                        chat_id=next_job.payload.to,
                        content=chain_response or "",
                    ))
            if next_job.payload.next_job_id and chain_response is not None and not is_chain_failure(chain_response):
                await _handle_chain(next_job, chain_response, run_id)

    cron.on_job = on_cron_job

    async def on_heartbeat(prompt: str) -> str:
        return await responder.process_direct(
            prompt,
            session_key="heartbeat",
            channel="heartbeat",
            chat_id="direct",
        )

    heartbeat = HeartbeatService(
        workspace=workspace,
        on_heartbeat=on_heartbeat,
        interval_s=30 * 60,
        enabled=True,
    )

    orchestrator_service = OrchestratorService(
        bus=bus,
        orchestrator=orchestrator,
        typing_adapter=typing_adapter,
        telemetry=telemetry,
        memory=memory_service,
        effect_router=effect_router,
    )

    # IPC socket for overseer commands
    from yeoman_gateway.ipc.gateway_socket import GatewaySocket

    ipc_config = config.ipc
    socket_path = Path(ipc_config.gateway_socket_path).expanduser()

    async def ipc_send_message(channel: str, chat_id: str, content: str) -> dict:
        if service_effects is not None:
            await service_effects.send(
                source="ipc",
                operation_ref=f"ipc:{_uuid4().hex}",
                channel=channel,
                chat_id=chat_id,
                content=content,
            )
            return {"queued": True}
        await bus.publish_outbound(
            OutboundMessage(channel=channel, chat_id=chat_id, content=content)
        )
        return {"delivered": True}

    async def ipc_trigger_agent_turn(
        prompt: str,
        session_key: str,
        channel: str,
        chat_id: str,
        model_profile: str | None = None,
    ) -> dict:
        response = await responder.process_direct(
            prompt,
            session_key=session_key,
            channel=channel,
            chat_id=chat_id,
            model_profile=model_profile,
        )
        return {"response": response}

    async def ipc_owner_turn(
        prompt: str,
        session_key: str | None,
        chat_id: str,
        post_to_whatsapp: bool,
    ) -> dict:
        from yeoman_gateway.ipc.owner_turn import process_owner_turn

        async def _owner_outbound(message: OutboundMessage) -> None:
            from yeoman_gateway.processing.dispatch import CURRENT_PRINCIPAL

            CURRENT_PRINCIPAL.set(str(chat_id))
            try:
                await responder.send_outbound(message)
            finally:
                CURRENT_PRINCIPAL.set("")

        return await process_owner_turn(
            prompt=prompt,
            chat_id=chat_id,
            session_key=session_key,
            post_to_whatsapp=post_to_whatsapp,
            policy_adapter=policy_adapter,
            responder=responder,
            bus=bus,
            outbound_dispatch=_owner_outbound,
        )

    async def ipc_publish_event(kind: str, detail: dict) -> dict:
        from yeoman_gateway.bus.events import SystemEvent

        await bus.publish_event(SystemEvent(kind=kind, detail=detail, timestamp=time.time()))
        return {"published": True}

    gateway_socket = GatewaySocket(
        path=socket_path,
        send_message_handler=ipc_send_message,
        trigger_agent_turn_handler=ipc_trigger_agent_turn,
        owner_turn_handler=ipc_owner_turn,
        publish_event_handler=ipc_publish_event,
        rate_limit=ipc_config.command_rate_limit,
    )

    consciousness_service = None
    lull_observer = None
    if config.consciousness.enabled and policy_engine is not None:
        from yeoman_gateway.consciousness.agent import ConsciousnessAgent
        from yeoman_gateway.consciousness.burst import BurstObserver
        from yeoman_gateway.consciousness.lull import LullObserver
        from yeoman_gateway.consciousness.outcomes import OutcomeEnricher
        from yeoman_gateway.consciousness.service import ConsciousnessService
        from yeoman_gateway.consciousness.taste import TasteDistiller
        from yeoman_gateway.consciousness.tools import ConsciousnessTools

        consciousness_tools = ConsciousnessTools(
            config=config,
            policy_engine=policy_engine,
            bus=bus,
            log=speakup_log,
            inbound_archive=inbound_archive,
            memory=memory_service,
            security=security,
            approval_store=speakup_approval_store,
            service_effects=service_effects,
        )

        async def _consciousness_route_call(route: str, prompt: str) -> str:
            profile = model_router.resolve(route)
            if not profile.model:
                raise RuntimeError(f"Consciousness route {route!r} has no model")
            routed_provider = provider_factory.create_chat_provider(profile.model, profile.provider)
            response = await routed_provider.chat(
                [{"role": "user", "content": prompt}],
                tools=[],
                model=profile.model,
                max_tokens=profile.max_tokens or 700,
                temperature=profile.temperature if profile.temperature is not None else 0.1,
                reasoning=profile.reasoning,
            )
            return response.content or "{}"

        async def _consciousness_planner(prompt: str) -> str:
            return await _consciousness_route_call("consciousness.agent", prompt)

        consciousness_agent = ConsciousnessAgent(
            tools=consciousness_tools,
            planner=_consciousness_planner,
        )
        outcome_enricher = OutcomeEnricher(
            log=speakup_log,
            inbound_archive=inbound_archive,
            classifier=lambda prompt: _consciousness_route_call("consciousness.outcome", prompt),
        )
        taste_distiller = TasteDistiller(
            log=speakup_log,
            memory=memory_service,
            distiller=lambda prompt: _consciousness_route_call("consciousness.taste", prompt),
        )
        consciousness_service = ConsciousnessService(
            config=config,
            agent=consciousness_agent,
            outcome_enricher=outcome_enricher,
            taste_distiller=taste_distiller,
            speakup_log=speakup_log,
        )
        burst_observer = BurstObserver(
            config=config,
            state_path=consciousness_data_dir / "burst_state.json",
            on_burst=lambda channel, chat_id: consciousness_service.tick_once(
                trigger="burst",
                target_channel=channel,
                target_chat_id=chat_id,
            ),
            is_eligible=lambda channel, chat_id: consciousness_tools.is_chat_within_opportunity_budget(
                channel,
                chat_id,
                trigger="burst",
            ),
            session_manager=session_manager,
        )
        bus.subscribe_event("InboundObservedEvent", burst_observer.handle)

        if config.consciousness.lull_enabled:
            lull_observer = LullObserver(
                config=config,
                state_path=consciousness_data_dir / "lull_state.json",
                on_lull=lambda channel, chat_id: consciousness_service.tick_once(
                    trigger="lull",
                    target_channel=channel,
                    target_chat_id=chat_id,
                ),
                is_eligible=lambda channel, chat_id: consciousness_tools.is_chat_within_opportunity_budget(
                    channel,
                    chat_id,
                    trigger="lull",
                ),
                session_manager=session_manager,
            )
            bus.subscribe_event("InboundObservedEvent", lull_observer.handle)

    shared_fact_runtime = build_shared_fact_runtime(
        config,
        store=processing_store,
        processing=processing_store,
        chat_registry=chat_registry,
        policy=policy_adapter,
        memory=memory_service,
    )
    if shared_fact_runtime is not None:
        responder.shared_facts = shared_fact_runtime

    return GatewayRuntime(
        orchestrator=orchestrator_service,
        channels=channels,
        cron=cron,
        heartbeat=heartbeat,
        consciousness=consciousness_service,
        inbound_archive=inbound_archive,
        responder=responder,
        memory=memory_service,
        contacts=contacts_service,
        chat_registry=chat_registry,
        bus=bus,
        gateway_socket=gateway_socket,
        speakup_log=speakup_log,
        lull_observer=lull_observer,
        processing=processing_store,
        reconciliation=build_reconciliation_service(config, processing_store),
        shared_facts=shared_fact_runtime,
        startup_hook=_notify_pending_persona_evolution_reviews,
    )
